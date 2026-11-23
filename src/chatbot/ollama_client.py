"""Call a local Ollama model as the intent-extraction LLM.

    llm = make_ollama_llm(metadata, model="qwen2.5:7b")
    handle_question("What is SREQ 123?", llm)

Uses Ollama's chat API (POST {host}/api/chat) with:
  * format = the intent JSON Schema, so the reply is forced into a valid shape
  * temperature = 0, so the same question gives the same intent
  * stream = false, so the whole reply comes back at once
  * num_ctx = the context window in tokens. Ollama's default (2048-4096, depending
    on the version) is smaller than our system prompt, and Ollama silently drops the
    start of a prompt that doesn't fit: that is where the rules are. So it is set
    explicitly, and a reply whose prompt filled the window is rejected.

Only the Python standard library is used. Defaults can be set with the
OLLAMA_HOST, OLLAMA_MODEL and OLLAMA_NUM_CTX environment variables.

Why: Ollama is the local stand-in for the production LLM during development.
It is wrapped in the same (system_prompt, question) -> text function shape as
any other LLM, so replacing it later means writing one new function, not
changing the pipeline.

Note: urllib blocks while waiting for the model. In an async host (asyncio /
FastAPI async def), run the call in a worker thread so other users aren't blocked.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

from .errors import IntentExtractionError
from .intent_extraction import LLMCall
from .intent_schema import build_intent_schema
from .metadata_loader import Metadata

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_NUM_CTX = 16384  # the prompt is ~6k tokens today; leaves room for metadata to grow


def make_ollama_llm(
    metadata: Metadata,
    model: str | None = None,
    host: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    num_ctx: int | None = None,
) -> LLMCall:
    """Return an LLM function (system_prompt, question) -> reply text that calls Ollama.

    model and host fall back to $OLLAMA_MODEL / $OLLAMA_HOST, then the defaults; a
    host without http:// gets it added. num_ctx falls back to $OLLAMA_NUM_CTX, then
    DEFAULT_NUM_CTX. The JSON Schema is built once here, not on
    every question. The returned function carries .model and .host so the CLI can
    show which model answered.
    """
    model = model or os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL
    host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    num_ctx = num_ctx or int(os.environ.get("OLLAMA_NUM_CTX") or DEFAULT_NUM_CTX)
    schema = build_intent_schema(metadata)

    def call(system_prompt: str, question: str) -> str:
        """Send one question to Ollama and return the reply text (the raw JSON intent)."""
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": num_ctx},
        }
        reply = _post_json(f"{host}/api/chat", body, timeout, model, host)
        used = reply.get("prompt_eval_count")
        if isinstance(used, int) and used >= num_ctx:
            # Ollama cut the prompt to fit, so the model answered without some of the rules.
            raise IntentExtractionError(
                f"The prompt filled Ollama's whole context window ({num_ctx} tokens), so part of it was "
                "cut off. Raise OLLAMA_NUM_CTX."
            )
        content = (reply.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise IntentExtractionError("Ollama's reply had no message content.")
        return content

    call.model = model  # type: ignore[attr-defined]  # shown by the CLI
    call.host = host  # type: ignore[attr-defined]
    call.num_ctx = num_ctx  # type: ignore[attr-defined]
    return call


def _post_json(url: str, body: dict[str, Any], timeout: float, model: str, host: str) -> dict[str, Any]:
    """POST `body` as JSON to `url` and return the decoded JSON response.

    Every failure becomes an IntentExtractionError with a fix the developer can act
    on: unknown model ("ollama pull ..."), timeout, Ollama not running ("ollama
    serve"), or a response that isn't a JSON object.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404 and "not found" in detail.lower():
            raise IntentExtractionError(
                f"Ollama has no model '{model}'. Download it with: ollama pull {model} "
                f"(see installed models with: ollama list)."
            ) from exc
        raise IntentExtractionError(f"Ollama returned HTTP {exc.code}: {detail[:300]}") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise IntentExtractionError(f"Ollama did not answer within {timeout:.0f} seconds.") from exc
    except urllib.error.URLError as exc:
        raise IntentExtractionError(
            f"Can't reach Ollama at {host} ({exc.reason}). Is it running? Start it with: ollama serve"
        ) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntentExtractionError("Ollama's response was not valid JSON.") from exc
    if not isinstance(data, dict):
        raise IntentExtractionError("Ollama's response was not a JSON object.")
    return data
