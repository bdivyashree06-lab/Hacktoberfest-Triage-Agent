"""OpenAI-compatible LLM client with one-shot JSON repair.

The backend is an environment-variable choice — no code changes needed::

    # Ollama (default, local, works offline)
    LLM_BASE_URL=http://localhost:11434/v1  LLM_MODEL=qwen2.5:7b-instruct

    # llama.cpp llama-server (local, works offline)
    LLM_BASE_URL=http://localhost:8080/v1   LLM_MODEL=your-model.gguf

    # Hosted open-weight endpoint (needs venue Wi-Fi)
    LLM_BASE_URL=https://api.groq.com/openai/v1
    LLM_API_KEY=...  LLM_MODEL=llama-3.3-70b-versatile

Every backend above speaks the OpenAI chat-completions API and supports
JSON mode via response_format={"type": "json_object"}.
Only open-weight models may be used in this track (Qwen/Llama/DeepSeek/Gemma...).
"""

from __future__ import annotations

import json
import os

import openai

BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
MODEL = os.environ.get("LLM_MODEL", "qwen2.5:7b-instruct")
TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "90"))
# Low temperature: triage should be reproducible — nondeterministic demos lose judges.
TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))

_client: openai.OpenAI | None = None


def client() -> openai.OpenAI:
    """Lazy singleton: importing this module never touches the network."""
    global _client
    if _client is None:
        _client = openai.OpenAI(
            base_url=BASE_URL,
            api_key=os.environ.get("LLM_API_KEY", "local"),  # local servers ignore it
            timeout=TIMEOUT,
            max_retries=1,  # fail fast — the agent degrades instead of hanging the demo
        )
    return _client


def _create(messages: list[dict], json_mode: bool = True) -> str:
    kwargs: dict = {"model": MODEL, "messages": messages, "temperature": TEMPERATURE}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client().chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


def _complete(messages: list[dict]) -> tuple[str | None, str | None]:
    """Returns (text, transport_error). Never raises."""
    try:
        return _create(messages, json_mode=True), None
    except openai.BadRequestError:
        # Backend that doesn't understand response_format — retry once without it.
        try:
            return _create(messages, json_mode=False), None
        except openai.OpenAIError as exc:
            return None, f"{type(exc).__name__}: {exc}"
    except openai.OpenAIError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def chat_json(messages: list[dict]) -> dict | None:
    """Failure path #1 — model output handling:

    * valid JSON            -> dict
    * broken JSON           -> ONE repair retry -> dict, else None
    * backend unreachable   -> {"__transport_error__": "..."} so the orchestrator
      flags needs_human instead of hallucinating a verdict.
    """
    raw, err = _complete(messages)
    if err:
        return {"__transport_error__": err}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass

    # Repair attempt: echo the offending output back with a strict instruction.
    repair = messages + [
        {"role": "assistant", "content": raw or ""},
        {"role": "user", "content": "That was not valid JSON. Output ONLY the corrected JSON object."},
    ]
    raw2, err2 = _complete(repair)
    if err2:
        return {"__transport_error__": err2}
    try:
        return json.loads(raw2)
    except (json.JSONDecodeError, TypeError):
        return None
