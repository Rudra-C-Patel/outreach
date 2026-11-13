"""Local Jarvis model via Ollama (127.0.0.1 only)."""
from __future__ import annotations

import json

import httpx

from . import config


class LLMError(RuntimeError):
    pass


def chat_json(system: str, user: str, *, temperature: float = 0.4, timeout: float = 180) -> dict:
    if not config.OLLAMA_URL.startswith(("http://127.0.0.1", "http://localhost")):
        raise LLMError("refusing non-local model endpoint")
    payload = {
        "model": config.OLLAMA_MODEL,
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"temperature": temperature, "num_ctx": 16384},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    try:
        r = httpx.post(f"{config.OLLAMA_URL}/api/chat", json=payload, timeout=timeout)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMError(f"Ollama not reachable at {config.OLLAMA_URL} ({e}). Run: ollama serve") from e
    content = r.json()["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise LLMError(f"model returned non-JSON: {content[:200]}") from e
