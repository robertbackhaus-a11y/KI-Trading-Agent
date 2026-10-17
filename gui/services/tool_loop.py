# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

"""The chat tool loop (no Qt, no framework): Ollama <-> MCP tools, read-only.

    user message -> /api/chat (streamed) -> tool_calls? -> check -> MCP call -> result as role=tool -> /api/chat again -> ... -> answer

* The tools offered to the model come from the MCP server's own discovery (name, description and input schema pass through unchanged) and are
  limited to the READ class of ``tool_policy``.  ``approve_swing_promotion``, ``sql_execute`` and every unknown tool are never offered, and a
  call to one of them anyway is answered with ``TOOL_NOT_ALLOWED`` without any request to the MCP server.
* Technical limits only: at most ``MAX_ROUNDS`` tool rounds per request, a size limit per tool result and for all results of one request.
  The parameter ``detail`` is removed from the offered schemas and forced to ``false`` (the compact views fit the context; the 100k character
  detail result never goes to the model).  Nothing here interprets trading data.
* Arguments are validated against the offered schema with a small validator before the call (no extra package).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from . import tool_policy
from .ollama_client import CANCELLED, CancelToken, OllamaClient, OllamaError

if TYPE_CHECKING:  # the loop only reads attributes of the result; it must stay importable without Qt
    from .mcp_client import McpResult

MAX_ROUNDS = 6
MAX_RESULT_CHARS = 40_000  # the same cap the MCP server applies to compact answers
MAX_TOTAL_RESULT_CHARS = 80_000  # all tool results of one request together
WARN_RESULT_CHARS = 20_000
TOOL_TIMEOUT_S = 90.0
FORCED_ARGUMENTS = {"detail": False, "table": False}  # the chat always gets the compact answer; set only where the tool has the parameter

SYSTEM_PROMPT = (
    "Du bist der Assistent von VIPER-AI, einer lokalen Portfolio-Analyse. Der Agent simuliert nur: Pläne und Empfehlungen sind keine Orders, "
    "und du kannst nichts kaufen, verkaufen, ändern oder starten.\n"
    "- Hole alle Fakten über die bereitgestellten Tools (nur lesend). Ergebnisse der Tools haben Vorrang vor deinem Wissen.\n"
    "- Erfinde keine Portfolio-, Kurs- oder Nachrichtendaten. Liefert ein Tool nichts oder einen Fehler, sage das klar.\n"
    "- Beachte simulation_only und die Gründe (reason codes) aus den Ergebnissen; nenne Symbol, Zahl und Grund so, wie das Tool sie liefert.\n"
    "- Rufe nur so viele Tools auf wie nötig. Passende Tools: Depotstatus und Planner: run_trading_orchestrator; Nachrichten: get_market_intelligence; "
    "Kandidaten mit Nachrichten: get_opportunity_view; Watchlist-Bewertung: evaluate_watchlist_candidates oder rank_watchlist; Datenbankzustand: database_status.\n"
    "Antworte knapp auf Deutsch."
)


@dataclass
class LoopEvent:
    """``kind``: status | thinking | content | tool_call | tool_result | warning | error | done (the last event of every request; ``data["outcome"]`` says how it ended)."""

    kind: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoopResult:
    outcome: str  # done | cancelled | error | round_limit
    history: list[dict[str, Any]]
    rounds: int = 0
    tool_calls: int = 0
    error: str = ""


# -- tool catalog -------------------------------------------------------------------------------------------
def offered_tools(catalog: Mapping[str, Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """``(ollama tools, name -> schema)`` of the READ tools in ``catalog`` (the MCP discovery), sorted by name.

    The MCP definition stays the source of truth: name, description and schema are passed through.  The only change is that the
    parameter ``detail`` is removed (it is forced to ``false`` when the tool is called).
    """
    tools: list[dict[str, Any]] = []
    schemas: dict[str, dict[str, Any]] = {}
    for name in sorted(catalog):
        if not tool_policy.is_read(name):
            continue
        schema = json.loads(json.dumps(catalog[name].get("input_schema") or {"type": "object", "properties": {}}))
        props = schema.get("properties")
        if isinstance(props, dict):
            for forced in FORCED_ARGUMENTS:
                props.pop(forced, None)
            if isinstance(schema.get("required"), list):
                schema["required"] = [r for r in schema["required"] if r not in FORCED_ARGUMENTS]
        schemas[name] = schema
        tools.append({"type": "function", "function": {"name": name, "description": str(catalog[name].get("description") or name), "parameters": schema}})
    return tools, schemas


def forced_arguments(catalog: Mapping[str, Mapping[str, Any]], name: str) -> dict[str, Any]:
    """The forced arguments this tool actually has (a tool without ``table`` must not be sent one)."""
    props = (catalog.get(name, {}).get("input_schema") or {}).get("properties") or {}
    return {k: v for k, v in FORCED_ARGUMENTS.items() if k in props}


def schema_hash(catalog: Mapping[str, Mapping[str, Any]]) -> str:
    """Short hash of the whole MCP tool list (names, descriptions, schemas): a changed tool surface shows up in the diagnostics."""
    payload = [(n, catalog[n].get("description") or "", catalog[n].get("input_schema") or {}) for n in sorted(catalog)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


# -- argument validation ----------------------------------------------------------------------------------------
def _types(schema: Mapping[str, Any]) -> list[str]:
    t = schema.get("type")
    return [t] if isinstance(t, str) else list(t) if isinstance(t, list) else []


def _matches(value: Any, schema: Mapping[str, Any], path: str, errors: list[str]) -> Any:
    """Validate ``value`` against the JSON schema subset used by the MCP tools; returns the (possibly coerced) value."""
    for key in ("anyOf", "oneOf"):
        if isinstance(schema.get(key), list):
            for option in schema[key]:
                trial: list[str] = []
                result = _matches(value, option, path, trial)
                if not trial:
                    return result
            errors.append(f"{path}: passt zu keiner erlaubten Variante")
            return value
    types = _types(schema)
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: erlaubt sind {schema['enum']}")
        return value
    if not types:
        return value
    if value is None:
        if "null" not in types:
            errors.append(f"{path}: null ist nicht erlaubt")
        return value
    if isinstance(value, str):  # models often send numbers and booleans as strings: coerce exact matches only
        text = value.strip()
        if "string" not in types:
            if "integer" in types and text.lstrip("-").isdigit():
                return int(text)
            if "number" in types:
                try:
                    return float(text)
                except ValueError:
                    pass
            if "boolean" in types and text.lower() in ("true", "false"):
                return text.lower() == "true"
    checks = {
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool) or isinstance(v, float) and v.is_integer(),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }
    if not any(checks.get(t, lambda v: False)(value) for t in types):
        errors.append(f"{path}: erwartet {'/'.join(types)}, erhalten {type(value).__name__}")
        return value
    if isinstance(value, float) and value.is_integer() and "integer" in types and "number" not in types:
        value = int(value)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [_matches(v, schema["items"], f"{path}[{i}]", errors) for i, v in enumerate(value)]
    if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        return validate_arguments(schema, value, errors, path)
    return value


def validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any], errors: Optional[list[str]] = None, path: str = "") -> dict[str, Any]:
    """Check ``arguments`` against an object schema.  Returns the coerced arguments; problems are appended to ``errors``."""
    errors = errors if errors is not None else []
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    out: dict[str, Any] = {}
    for name in schema.get("required") or []:
        if name not in arguments:
            errors.append(f"{path + '.' if path else ''}{name}: Pflichtargument fehlt")
    for name, value in arguments.items():
        where = f"{path + '.' if path else ''}{name}"
        if name not in props:
            errors.append(f"{where}: unbekanntes Argument")
            continue
        out[name] = _matches(value, props[name], where, errors)
    return out


# -- history -------------------------------------------------------------------------------------------------------
def compact_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Older tool results are replaced by a one-line placeholder, so a follow-up question does not carry 100k characters again."""
    out = []
    for message in history:
        if message.get("role") == "tool":
            size = len(str(message.get("content") or ""))
            out.append({**message, "content": f"[Ergebnis von {message.get('tool_name', 'Tool')} ({size} Zeichen) – nicht mehr im Verlauf]"})
        else:
            out.append(message)
    return out


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _error_text(kind: str, message: str) -> str:
    return _json({"ok": False, "error": kind, "message": message})


# -- the loop ----------------------------------------------------------------------------------------------------
class ToolLoop:
    """Blocking; meant to run in a worker thread.  ``call_tool(name, arguments, token) -> McpResult`` is the only way to the MCP server."""

    def __init__(self, client: OllamaClient, catalog: Mapping[str, Mapping[str, Any]], call_tool: Callable[[str, dict[str, Any], CancelToken], "McpResult"], model: str,
                 think: bool = False, system_prompt: str = SYSTEM_PROMPT, max_rounds: int = MAX_ROUNDS, max_result_chars: int = MAX_RESULT_CHARS,
                 max_total_chars: int = MAX_TOTAL_RESULT_CHARS) -> None:
        self.client, self.catalog, self.call_tool = client, catalog, call_tool
        self.model, self.think, self.system_prompt = model, think, system_prompt
        self.max_rounds, self.max_result_chars, self.max_total_chars = max_rounds, max_result_chars, max_total_chars
        self.tools, self.schemas = offered_tools(catalog)

    def run(self, history: list[dict[str, Any]], user_text: str, emit: Callable[[LoopEvent], None], token: CancelToken) -> LoopResult:
        started = time.perf_counter()
        base = compact_history(history)
        turn: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
        rounds = calls_made = total_chars = prompt_tokens = eval_tokens = 0

        def finish(outcome: str, error: str = "") -> LoopResult:
            emit(LoopEvent("done", data={"outcome": outcome, "rounds": rounds, "tool_calls": calls_made, "seconds": round(time.perf_counter() - started, 2),
                                         "prompt_tokens": prompt_tokens, "eval_tokens": eval_tokens}))
            return LoopResult(outcome, base + turn if outcome == "done" else history, rounds, calls_made, error)

        while True:
            if token.cancelled:
                return finish("cancelled")
            emit(LoopEvent("status", "Ollama antwortet …", {"round": rounds}))
            content, pending = "", []
            try:
                for chunk in self.client.chat_stream(self.model, [{"role": "system", "content": self.system_prompt}, *base, *turn], self.tools, self.think, token):
                    if chunk.kind == "thinking":
                        if self.think:
                            emit(LoopEvent("thinking", chunk.text))
                    elif chunk.kind == "content":
                        content += chunk.text
                        emit(LoopEvent("content", chunk.text))
                    elif chunk.kind == "tool_calls":
                        pending.extend(chunk.tool_calls)
                    elif chunk.kind == "done":
                        prompt_tokens += int(chunk.stats.get("prompt_eval_count") or 0)
                        eval_tokens += int(chunk.stats.get("eval_count") or 0)
            except OllamaError as exc:
                if exc.kind == CANCELLED or token.cancelled:
                    return finish("cancelled")
                emit(LoopEvent("error", str(exc), {"kind": exc.kind, "status": exc.status}))
                return finish("error", f"{exc.kind}: {exc}")
            if not pending:
                turn.append({"role": "assistant", "content": content})
                return finish("done")
            if rounds >= self.max_rounds:
                emit(LoopEvent("warning", f"Abbruch: mehr als {self.max_rounds} Tool-Runden in einer Anfrage. Frage enger oder in Schritten.", {"limit": self.max_rounds}))
                return finish("round_limit", f"mehr als {self.max_rounds} Tool-Runden")
            rounds += 1
            turn.append({"role": "assistant", "content": content, "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in pending]})
            for call in pending:
                if token.cancelled:
                    return finish("cancelled")
                calls_made += 1
                text, total_chars = self._execute(call, emit, token, total_chars)
                turn.append({"role": "tool", "tool_name": call["name"], "content": text})

    def _execute(self, call: dict[str, Any], emit: Callable[[LoopEvent], None], token: CancelToken, total_chars: int) -> tuple[str, int]:
        name, arguments = call["name"], call["arguments"]
        shown = {k: v for k, v in arguments.items() if k not in FORCED_ARGUMENTS}
        started = time.perf_counter()

        def refuse(kind: str, message: str) -> tuple[str, int]:
            emit(LoopEvent("tool_call", name, {"arguments": shown, "allowed": False}))
            emit(LoopEvent("tool_result", name, {"ok": False, "kind": kind, "seconds": 0.0, "chars": 0, "note": message}))
            return _error_text(kind, message), total_chars

        if name not in self.schemas:  # not offered: an action, an unknown tool, or a made-up name
            why = "Aktion" if tool_policy.is_known(name) and not tool_policy.is_read(name) else "unbekanntes Tool"
            return refuse("TOOL_NOT_ALLOWED", f"{name or '(ohne Namen)'}: {why} – in dieser Chat-Version nicht erlaubt")
        errors: list[str] = []
        coerced = validate_arguments(self.schemas[name], {k: v for k, v in arguments.items() if k not in FORCED_ARGUMENTS}, errors)
        if errors:
            return refuse("TOOL_ARGS_INVALID", "; ".join(errors)[:300])
        final_args = dict(coerced)
        final_args.update(forced_arguments(self.catalog, name))
        emit(LoopEvent("tool_call", name, {"arguments": coerced, "allowed": True}))
        emit(LoopEvent("status", f"Tool {name} läuft …"))
        result = self.call_tool(name, final_args, token)
        seconds = round(time.perf_counter() - started, 2)
        if token.cancelled:
            emit(LoopEvent("tool_result", name, {"ok": False, "kind": "CANCELLED", "seconds": seconds, "chars": 0, "note": "abgebrochen"}))
            return _error_text("CANCELLED", "abgebrochen"), total_chars
        if not result.ok:
            emit(LoopEvent("tool_result", name, {"ok": False, "kind": result.kind, "seconds": seconds, "chars": 0, "note": result.error or ""}))
            return _error_text(result.kind, result.error or "Fehler"), total_chars
        text = _json(result.data)
        chars = len(text)
        note = ""
        if chars > self.max_result_chars:
            note = f"Ergebnis zu groß ({chars} Zeichen, Grenze {self.max_result_chars}); nicht an das Modell übergeben"
            text = _error_text("TOOL_RESULT_TOO_LARGE", f"Das Ergebnis hat {chars} Zeichen (Grenze {self.max_result_chars}). Frage enger.")
        elif total_chars + chars > self.max_total_chars:
            note = f"Gesamtgrenze der Tool-Ergebnisse ({self.max_total_chars} Zeichen) erreicht; Ergebnis nicht übergeben"
            text = _error_text("TOOL_RESULT_BUDGET", f"Die Tool-Ergebnisse dieser Anfrage haben die Grenze von {self.max_total_chars} Zeichen erreicht.")
        else:
            total_chars += chars
            if chars > WARN_RESULT_CHARS:
                emit(LoopEvent("warning", f"Sehr großes Tool-Ergebnis von {name}: {chars} Zeichen"))
        emit(LoopEvent("tool_result", name, {"ok": result.tool_ok and not note, "kind": "OK" if not note else "TOO_LARGE", "seconds": seconds, "chars": chars, "note": note or result.message}))
        return text, total_chars
