"""MCP-Adapter fuer die Tools-Klasse in trading_sqlite.py.

Die Tool-Quelltexte liegen unveraendert neben diesem Skript. Jede oeffentliche Methode der
Klasse `Tools` wird 1:1 als MCP-Tool bereitgestellt. Sonderargumente mit doppeltem
Unterstrich (`__user__`, ...) werden aus der Tool-Signatur entfernt; `__user__` wird intern
mit einer festen Kennung gefuellt.

Konfiguration ueber Umgebungsvariablen:
  TRADING_DB_PATH       Pfad zur SQLite-Datenbank (Pflicht)
  TRADING_EXCLUDE       Komma-getrennte Tool-Namen, die nicht bereitgestellt werden
  TRADING_USER_ID       Kennung fuer `__user__` (Standard: goose)

Transport: stdio. Der Server laeuft nur, solange der Client ihn gestartet hat.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import sys
import typing
from pathlib import Path

from mcp.server.mcpserver import MCPServer

HERE = Path(__file__).resolve().parent


def _log(msg: str) -> None:
    # stdout gehoert dem MCP-Protokoll, Diagnose nur auf stderr
    print(msg, file=sys.stderr, flush=True)


def _load_tools_class(filename: str, modname: str):
    path = HERE / filename
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module.Tools


# Kontextschutz: Lokale Modelle haben ein kleines Kontextfenster. Das Rohergebnis der Trading-Engine
# umfasst ca. 100.000 Zeichen, die eigentliche Antwort (rendered_summary_de) nur ca. 1.300.
MAX_RESULT_CHARS = int(os.environ.get("TRADING_MAX_RESULT_CHARS", "40000"))
ORCHESTRATOR = "run_trading_orchestrator"
HEAVY_ORCHESTRATOR_KEYS = ("existing_position_results", "entry_candidate_results", "promotion_results")
DETAIL_DOC = (
    "\n\nMCP adapter: detail=false (default) returns the compact status: portfolio, positions, position_engine (SELL/TRIM/HOLD/ADD of "
    "existing positions), promotion, entry_plan (planner: planned and deferred NEW entries), proceeds (gross/tax/net) and capital. "
    "detail=true returns the complete result (about 95,000 characters; only on explicit request)."
)


def _compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _load_orchestrator_compact():
    """The pure projection module lives next to this file; a missing or broken module must never break the tool."""
    path = HERE / "orchestrator_compact.py"
    try:
        spec = importlib.util.spec_from_file_location("orchestrator_compact", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as exc:  # noqa: BLE001
        _log(f"orchestrator_compact unavailable ({type(exc).__name__}); falling back to the legacy slimming")
        return None


_COMPACT = _load_orchestrator_compact()


def _slim_orchestrator(res):
    """Default (detail=false) view: structured compact status; falls back to the legacy list-omitting view on any problem."""
    if _COMPACT is not None:
        try:
            compact = _COMPACT.compact_orchestrator_response(res)
            if compact is not None:
                return compact
        except Exception as exc:  # noqa: BLE001
            _log(f"compact orchestrator view failed ({type(exc).__name__}); falling back to the legacy slimming")
    return _slim_orchestrator_legacy(res)


def _slim_orchestrator_legacy(res):
    if not isinstance(res, dict) or not isinstance(res.get("result"), dict):
        return res
    inner = dict(res["result"])
    for key in HEAVY_ORCHESTRATOR_KEYS:
        value = inner.get(key)
        if value is not None:
            inner[key] = {
                "omitted_for_context": True,
                "count": len(value) if hasattr(value, "__len__") else None,
                "hint": "call run_trading_orchestrator with detail=true for the full list",
            }
    plan = inner.get("portfolio_action_plan")
    if isinstance(plan, dict) and isinstance(plan.get("entry_candidates"), list):
        slim_plan = dict(plan)
        slim_plan["entry_candidates"] = [
            {key: item.get(key) for key in ("rank", "symbol", "entry_status", "entry_score", "reason_codes")}
            for item in plan["entry_candidates"]
        ]
        slim_plan["entry_candidates_note"] = "per-candidate sizing/quality omitted for context; call run_trading_orchestrator with detail=true"
        inner["portfolio_action_plan"] = slim_plan
    return {**res, "result": inner, "detail": False}


def _make_wrapper(instance, name: str, method, user_id: str):
    sig = inspect.signature(method)
    hints = typing.get_type_hints(method)
    public_params = [p for p in sig.parameters.values() if not p.name.startswith("__")]
    needs_user = "__user__" in sig.parameters
    is_async = inspect.iscoroutinefunction(method)
    is_orchestrator = name == ORCHESTRATOR
    if is_orchestrator:
        public_params.append(
            inspect.Parameter("detail", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=False, annotation=bool)
        )

    async def wrapper(**kwargs):
        detail = bool(kwargs.pop("detail", False)) if is_orchestrator else False
        if needs_user:
            kwargs["__user__"] = {"id": user_id}
        if is_async:
            res = await method(**kwargs)
        else:
            res = await asyncio.to_thread(lambda: method(**kwargs))
        if is_orchestrator and not detail:
            res = _slim_orchestrator(res)
        text = _compact(res)
        if not detail and len(text) > MAX_RESULT_CHARS:
            return _compact({
                "ok": False,
                "error": "RESULT_TOO_LARGE",
                "message": (
                    f"Das Ergebnis hat {len(text)} Zeichen (Grenze {MAX_RESULT_CHARS}) und wurde nicht uebergeben, "
                    "um das Kontextfenster zu schuetzen. Frage enger (bestimmte Spalten, WHERE, LIMIT)."
                ),
            })
        return text

    wrapper.__name__ = name
    wrapper.__doc__ = (method.__doc__ or name) + (DETAIL_DOC if is_orchestrator else "")
    wrapper.__signature__ = sig.replace(parameters=public_params)
    annotations = {k: v for k, v in hints.items() if not k.startswith("__") and k != "return"}
    if is_orchestrator:
        annotations["detail"] = bool
    wrapper.__annotations__ = annotations
    return wrapper


def build_server() -> MCPServer:
    db_path = os.environ.get("TRADING_DB_PATH")
    if not db_path:
        raise SystemExit("TRADING_DB_PATH ist nicht gesetzt")
    user_id = os.environ.get("TRADING_USER_ID", "goose")
    excluded = {x.strip() for x in os.environ.get("TRADING_EXCLUDE", "").split(",") if x.strip()}

    server = MCPServer("trading")
    registered: list[str] = []

    sources = [("trading_sqlite.py", "trading_sqlite_tool")]

    for filename, modname in sources:
        tools_cls = _load_tools_class(filename, modname)
        instance = tools_cls()
        if hasattr(instance.valves, "database_path"):
            instance.valves.database_path = db_path
        for name, method in inspect.getmembers(instance, predicate=callable):
            if name.startswith("_") or name in excluded:
                continue
            if not (inspect.ismethod(method) or inspect.isfunction(method)):
                continue
            wrapper = _make_wrapper(instance, name, method, user_id)
            server.tool(name=name, description=inspect.getdoc(wrapper) or name, structured_output=False)(wrapper)
            registered.append(name)

    _log(f"trading-mcp: {len(registered)} Tools registriert: {', '.join(sorted(registered))}")
    return server


if __name__ == "__main__":
    build_server().run("stdio")
