"""Reachability probe of the local Ollama server (diagnostics only; there is no chat in this version).

The GUI never starts or stops Ollama.  Two short GET requests: ``/api/version`` and ``/api/tags`` (model names only).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class OllamaStatus:
    reachable: bool
    version: str = ""
    models: list[str] = field(default_factory=list)
    error: str = ""


def _get(url: str, timeout: float):
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - the URL is a user setting (http/https only, validated)
        return json.load(response)


def check_ollama(base_url: str, timeout: float = 2.0) -> OllamaStatus:
    base = base_url.rstrip("/")
    if not base.lower().startswith(("http://", "https://")):
        return OllamaStatus(False, error="URL muss mit http:// oder https:// beginnen")
    try:
        version = str(_get(f"{base}/api/version", timeout).get("version", ""))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return OllamaStatus(False, error=f"nicht erreichbar ({type(reason).__name__})")
    models: list[str] = []
    try:
        models = sorted(str(m.get("name", "")) for m in _get(f"{base}/api/tags", timeout).get("models", []))
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        pass
    return OllamaStatus(True, version=version, models=models)
