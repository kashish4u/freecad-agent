"""
engine/openai_client.py - a GENERIC OpenAI-compatible chat client (stdlib only).

WHY this exists (ADR 0004 + the "any OpenAI-compatible API" goal):
  The engine used to be hard-wired to Ollama's local HTTP API. That is just ONE of
  many providers that speak the OpenAI Chat Completions protocol: OpenAI itself,
  Groq, LM Studio, llama.cpp server, vLLM, Ollama (in OpenAI mode), Together,
  DeepSeek, and many more. To make the agent talk to ANY of them we need a client
  that speaks the OpenAI protocol, driven by CONFIG instead of hard-coded values.

  This module keeps the SAME interface the rest of the engine already depends on
  (chat_json / list_models / is_available / has_model / effective_model / model /
  timeout), so brain.py and bridge_server.py barely change. It also lets the old
  Ollama client keep working (see ollama_client.OllamaUnavailable, a subclass of
  the AiUnavailable raised here), so nothing that already works breaks.

STDLIB ONLY: urllib + json. No third-party packages, no venv. Runs on FreeCAD's
bundled Python 3.11 and on headless tests.

PROTOCOL NOTES (principle 9: adapt, don't exclude):
  - OpenAI style: POST {base_url}/chat/completions with
        response_format = {"type": "json_object"}
    and list models via GET  {base_url}/models  (data[].id).
  - Ollama style : POST {base_url}/api/chat with  format = "json"  and
    GET  {base_url}/api/tags  (models[].name). The default local provider still
    uses this style, so behaviour is unchanged for Ollama users.
  The endpoint/JSON shape is chosen per provider `style` (openai | ollama | auto).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

# Per-call timeout defaults to UNLIMITED (None): wait as long as the model needs,
# unless the user opts into a limit (panel) or sets FREECAD_AGENT_OLLAMA_TIMEOUT.
DEFAULT_TIMEOUT: "float | None" = None
# Context window (tokens) requested per chat call. Same rationale as the old
# Ollama default: our planning prompt (~2.8k tokens) plus a document needs more
# than the tiny model default or the reply comes back empty/garbled. 0 disables
# the override (the server uses its own default).
DEFAULT_NUM_CTX: int = 8192
# Reachability probes must stay snappy regardless of the inference timeout.
PROBE_TIMEOUT = 10.0


class AiUnavailable(RuntimeError):
    """Raised when the AI endpoint cannot be reached or the model is missing.

    The message is written to be shown straight to the user (principle 9).
    OllamaClient.OllamaUnavailable subclasses this, so existing callers that catch
    OllamaUnavailable keep working.
    """


def detect_style(base_url: str, style: str = "auto") -> str:
    """Resolve the provider protocol from an explicit style or the base URL.

    "auto" (the default) inspects the URL: Ollama is recognised by its host name
    or its well-known port 11434; everything else is treated as OpenAI-style.
    """
    s = (style or "auto").strip().lower()
    if s in ("openai", "ollama"):
        return s
    b = (base_url or "").lower().rstrip("/")
    if "ollama" in b or b.endswith(":11434"):
        return "ollama"
    # Ollama-style endpoints use a path segment named exactly "api"
    # (e.g. .../api/chat). Match only a real path segment so that the host name
    # "https://api.openai.com" does NOT get misclassified as Ollama.
    try:
        path = urllib.parse.urlparse(b).path
    except Exception:
        path = ""
    if any(seg == "api" for seg in path.split("/")):
        return "ollama"
    return "openai"


def _reply_content(reply: Dict[str, Any]) -> str:
    """Extract the assistant message content from a chat-completion response.

    Handles the standard OpenAI-compatible shape (choices[0].message.content),
    which real endpoints return, as well as the top-level {"message": {...}}
    shape some mock servers/tests emit. Returns "" when no content is present.
    """
    if not isinstance(reply, dict):
        return ""
    choices = reply.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        msg = choices[0].get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
    top = reply.get("message")
    if isinstance(top, dict) and isinstance(top.get("content"), str):
        return top["content"]
    return ""


class AiClient:
    """Tiny HTTP client for ANY OpenAI-compatible server (stdlib only).

    The interface mirrors the old OllamaClient so the rest of the engine needs
    almost no changes:
      chat_json(system, user, temperature) -> parsed JSON dict
      list_models()                       -> [tag, ...]  (also an availability probe)
      is_available()                      -> bool         (never raises)
      has_model(model=None)               -> bool
      effective_model()                   -> str          (installed tag to use)
      .model / .timeout / .base_url / .api_key / .style / .num_ctx / .temperature
    """

    def __init__(self, base_url: str, model: str = "gpt-4o-mini",
                 timeout: "float | None" = DEFAULT_TIMEOUT,
                 num_ctx: int = DEFAULT_NUM_CTX,
                 temperature: float = 0.0,
                 api_key: Optional[str] = None,
                 style: str = "auto",
                 known_models: Optional[List[str]] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.api_key = api_key
        self.style = detect_style(base_url, style)
        # Models we KNOW about (from config) - used as a fallback when the server's
        # /models list is empty or the endpoint is not queryable.
        self._known_models: List[str] = list(known_models or [])
        self._effective_model: Optional[str] = None

    # -- low-level HTTP --------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.style == "openai" and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST", headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AiUnavailable(self._http_error(path, exc)) from exc
        except urllib.error.URLError as exc:
            raise AiUnavailable(self._friendly_error(exc)) from exc
        except (TimeoutError, ConnectionError) as exc:  # pragma: no cover
            raise AiUnavailable(self._friendly_error(exc)) from exc

    def _get(self, path: str) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AiUnavailable(self._http_error(path, exc)) from exc
        except urllib.error.URLError as exc:
            raise AiUnavailable(self._friendly_error(exc)) from exc
        except (TimeoutError, ConnectionError) as exc:  # pragma: no cover
            raise AiUnavailable(self._friendly_error(exc)) from exc

    def _http_error(self, path: str, exc: "urllib.error.HTTPError") -> str:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace").strip()
        except Exception:
            pass
        return (f"The AI server returned HTTP {exc.code} on {path}: "
                f"{body or exc.reason}. (The server is reachable; this is usually a "
                f"missing model, a bad API key, or an unsupported endpoint.)")

    def _friendly_error(self, exc: Exception) -> str:
        return (
            f"Cannot reach the AI at {self.base_url}. "
            f"Check the endpoint and API key in the config, then try again. "
            f"Underlying error: {exc}"
        )

    # -- public API ------------------------------------------------------------

    def list_models(self) -> List[str]:
        """Return the model ids/names. Doubles as a reachability probe."""
        if self.style == "ollama":
            data = self._get("/api/tags")
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        # OpenAI style: GET /models -> data[].id. Fall back to the configured list
        # if the endpoint is unavailable or empty (still a usable availability signal).
        try:
            data = self._get("/models")
            ids = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
            if ids:
                return ids
        except AiUnavailable:
            pass
        return list(self._known_models)

    def is_available(self) -> bool:
        """True if the server answers. Never raises (principle 9)."""
        try:
            self.list_models()
            return True
        except AiUnavailable:
            return False

    def has_model(self, model: Optional[str] = None) -> bool:
        """True if `model` (or the default) is known to the server."""
        target = model or self.model
        try:
            installed = self.list_models()
        except AiUnavailable:
            installed = []
        bare = target.split(":")[0]
        return any(
            name == target or name.split(":")[0] == bare
            for name in installed if name
        )

    def effective_model(self) -> str:
        """Resolve the model tag to actually use.

        OpenAI style: the configured model if known, else the first known model,
        else the configured model (remote model names need no installation check).
        Ollama style: keep the old lenient base-name resolution against installed
        tags. Cached after the first successful resolution.
        """
        if self._effective_model:
            return self._effective_model
        try:
            installed = self.list_models()
        except AiUnavailable:
            installed = []
        candidates = installed or list(self._known_models)
        chosen = None
        if self.model in candidates:
            chosen = self.model
        else:
            bare = self.model.split(":")[0]
            same_base = [m for m in candidates if m.split(":")[0] == bare]
            chosen = same_base[0] if same_base else (candidates[0] if candidates else self.model)
        self._effective_model = chosen
        return chosen

    def chat_json(self, system: str, user: str,
                  temperature: float = 0.0) -> Dict[str, Any]:
        """
        Send a (system, user) pair and return the model's reply PARSED as JSON.

        JSON output is forced (OpenAI: response_format=json_object; Ollama:
        format=json). A low temperature keeps the planner deterministic. Robust
        against a "thinking" preamble: we extract the JSON object from the content
        if needed.
        """
        payload: Dict[str, Any] = {
            "model": self.effective_model(),  # use an actually-available model
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if self.style == "ollama":
            payload["stream"] = False
            payload["format"] = "json"
            options: Dict[str, Any] = {"temperature": temperature}
            if isinstance(self.num_ctx, int) and self.num_ctx > 0:
                options["num_ctx"] = self.num_ctx
            payload["options"] = options
        else:
            payload["response_format"] = {"type": "json_object"}
        reply = self._post("/chat/completions" if self.style == "openai"
                           else "/api/chat", payload)
        # TEMP DEBUG: dump the raw response shape (REMOVE after diagnosis).
        try:
            if isinstance(reply, dict):
                _keys = list(reply.keys())
                _msgkeys: Any = None
                _content: Any = None
                _choices = reply.get("choices")
                if isinstance(_choices, list) and _choices:
                    _c0 = _choices[0]
                    if isinstance(_c0, dict):
                        _m = _c0.get("message")
                        if isinstance(_m, dict):
                            _msgkeys = list(_m.keys())
                            _content = _m.get("content")
                _cstr = repr(_content)[:500]
                print(
                    f"[openai-debug] model={self.model} "
                    f"user={user[:120]!r} reply.keys={_keys} "
                    f"choices0_message.keys={_msgkeys} content={_cstr}",
                    flush=True,
                )
            else:
                print(
                    f"[openai-debug] reply is not a dict: {type(reply).__name__}",
                    flush=True,
                )
        except Exception as _e:  # pragma: no cover - debug only
            print(f"[openai-debug] debug dump failed: {_e}", flush=True)
        content = _reply_content(reply)
        return _parse_json_object(content)


def _strip_reasoning(content: str) -> str:
    """Remove reasoning/thinking blocks that thinking models emit around the answer.

    Reasoning models (DeepSeek-R1, Qwen3-thinking, etc.) commonly wrap a preamble
    of reasoning in <think>…</think> or <thinking>…</thinking> tags. That reasoning
    frequently contains braces, quotes and code, which would corrupt a naive brace
    scan of the whole reply. Stripping the blocks first keeps the JSON extraction
    honest.
    """
    if not content:
        return ""
    return re.sub(
        r"<think>.*?</think>|<thinking>.*?</thinking>",
        "",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _extract_json_object(text: str) -> str:
    """Return the outermost balanced {...} object in ``text``.

    The scan is JSON-string aware: braces that appear inside a JSON string literal
    do not count, so we correctly find the matching close brace even for nested
    objects/arrays. Returns "" when no complete object is present.
    """
    start = text.find("{")
    if start == -1:
        return ""
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _parse_json_object(content: str) -> Dict[str, Any]:
    """Parse the model's textual reply into a JSON object.

    Thinking/reasoning models often wrap the answer in a <think>…explain block
    (with or without tags) and that reasoning may itself contain braces, quotes
    and code. Strategy, in order of preference:
      1. strip the reasoning blocks, then parse what remains (handles a clean
         answer with surrounding whitespace/prose);
      2. if that fails, parse the raw reply as-is (in case there were no tags);
      3. finally, extract the outermost balanced {...} object from whichever of
         the two looks most promising.
    Raises ValueError if nothing parseable is found.
    """
    raw = (content or "").strip()
    if not raw:
        raise ValueError("the model returned an empty reply")

    cleaned = _strip_reasoning(raw).strip()
    # Order: cleaned answer first (most likely to be clean), then raw reply.
    for candidate in (cleaned, raw):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            pass
        snippet = _extract_json_object(candidate)
        if snippet:
            try:
                return json.loads(snippet)
            except ValueError:
                pass
    raise ValueError(f"no JSON object found in the model reply: {raw[:200]!r}")
