"""Display text of the chat events (pure Python): tool calls, tool results, errors, the final line.

Tool results are never shown in full: name, duration, size and a short note only.
"""

from __future__ import annotations

from typing import Any, Mapping

OLLAMA_ERRORS = {
    "UNREACHABLE": "Ollama ist nicht erreichbar. Läuft Ollama? (Die GUI startet Ollama nicht.)",
    "MODEL_MISSING": "Das Modell ist in Ollama nicht vorhanden. Das Modell wird in den Einstellungen gewählt.",
    "TIMEOUT": "Ollama hat nicht rechtzeitig geantwortet.",
    "STREAM_BROKEN": "Die Verbindung zu Ollama ist während der Antwort abgebrochen.",
    "HTTP_ERROR": "Ollama meldet einen Fehler.",
    "PROTOCOL": "Ollama hat eine unerwartete Antwort geliefert.",
    "MCP_NOT_CONNECTED": "MCP ist nicht verbunden.",
}


def human_chars(n: int) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}".replace(".", ",") + "k Zeichen"
    return f"{n} Zeichen"


def short_value(value: Any, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_tool_call(name: str, arguments: Mapping[str, Any], allowed: bool = True) -> str:
    args = ", ".join(f"{k}={short_value(v)}" for k, v in arguments.items())
    return f"▸ Tool {name}({args})" + ("" if allowed else "  – nicht erlaubt")


def format_tool_result(name: str, data: Mapping[str, Any]) -> tuple[str, str]:
    """``(text, level)``; level is ``ok`` | ``warning`` | ``error``."""
    seconds = float(data.get("seconds") or 0.0)
    chars = int(data.get("chars") or 0)
    note = str(data.get("note") or "")
    when = f"{seconds:.2f} s".replace(".", ",")
    if data.get("ok"):
        return f"✓ {name}: {when}, {human_chars(chars)}" + (f" – {note}" if note else ""), "ok"
    kind = str(data.get("kind") or "FEHLER")
    level = "warning" if kind in ("TOO_LARGE", "CANCELLED") else "error"
    return f"✗ {name}: {kind}" + (f" – {short_value(note, 160)}" if note else "") + (f" ({when})" if seconds else ""), level


def format_error(kind: str, message: str) -> str:
    hint = OLLAMA_ERRORS.get(kind)
    return f"{hint} ({message})" if hint and message and message not in hint else (hint or message)


def format_done(data: Mapping[str, Any]) -> str:
    outcome = data.get("outcome")
    seconds = f"{float(data.get('seconds') or 0):.1f}".replace(".", ",")
    parts = [f"{seconds} s"]
    if data.get("rounds"):
        parts.append(f"{data['rounds']} Tool-Runde" + ("n" if data["rounds"] != 1 else "") + f", {data.get('tool_calls', 0)} Aufruf" + ("e" if data.get("tool_calls", 0) != 1 else ""))
    tokens = int(data.get("prompt_tokens") or 0) + int(data.get("eval_tokens") or 0)
    if tokens:
        parts.append(f"{tokens} Tokens")
    label = {"done": "Fertig", "cancelled": "Abgebrochen", "error": "Fehler", "round_limit": "Tool-Runden-Limit erreicht"}.get(str(outcome), str(outcome))
    return f"{label} · " + " · ".join(parts)
