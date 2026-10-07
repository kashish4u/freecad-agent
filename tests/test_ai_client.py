#!/usr/bin/env python3
"""
test_ai_client.py - the generic OpenAI-compatible client + config layer.

We never touch a real provider: a tiny http.server impersonates BOTH the OpenAI
style (/models, /chat/completions) and the Ollama style (/api/tags, /api/chat)
endpoints so we can prove the client adapts by `style`. We also test the config
layer (defaults, env override, file override, build_client) and the fact that
OllamaUnavailable is now a subclass of the generic AiUnavailable.

Runnable:
    python tests/test_ai_client.py
    pytest tests/test_ai_client.py
"""

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "engine"))

import ai_config  # noqa: E402
import ollama_client  # noqa: E402
from openai_client import AiClient, AiUnavailable, _parse_json_object, detect_style  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the test server
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    last_chat_payload = None
    last_auth = None

    def do_GET(self):
        _Handler.last_auth = self.headers.get("Authorization")
        if self.path == "/models":
            self._json({"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]})
        elif self.path == "/api/tags":
            self._json({"models": [{"name": "qwen3:4b"}]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        _Handler.last_auth = self.headers.get("Authorization")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            _Handler.last_chat_payload = json.loads(body.decode("utf-8"))
        except Exception:
            _Handler.last_chat_payload = None
        # A model that wraps JSON in a <think> preamble (real behaviour we must tolerate).
        content = ('<think>ok</think>\n'
                    '{"actions": [{"type": "command", "cmd": "create_box", '
                    '"params": {"length": 1, "width": 1, "height": 1}}]}')
        if self.path == "/chat/completions":
            # Standard OpenAI-compatible shape: answer nested at
            # choices[0].message.content (real endpoints return this, not a
            # top-level "message"). The client must read it from there.
            self._json({"choices": [{"message": {"role": "assistant", "content": content}}]})
        else:  # ollama /api/chat
            self._json({"message": {"role": "assistant", "content": content}})


def _start_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


class _ErrorHandler(BaseHTTPRequestHandler):
    """Returns HTTP 500 on every request, to exercise graceful degradation."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()


def run_scenarios():
    srv = _start_server()
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))

    # --- style auto-detection -------------------------------------------------
    check("detect ollama via :11434", detect_style("http://127.0.0.1:11434") == "ollama")
    check("detect ollama via host name", detect_style("http://ollama:8080") == "ollama")
    check("detect openai via url", detect_style("https://api.openai.com/v1") == "openai")
    check("detect openai via port", detect_style("http://localhost:1234") == "openai")
    check("explicit style wins", detect_style("http://x:11434", "openai") == "openai")

    # --- OpenAI-style client over the fake server -----------------------------
    c = AiClient(base_url=base, model="gpt-4o-mini", style="openai",
                 known_models=["gpt-4o-mini"], api_key="sk-test")
    check("openai style detected", c.style == "openai")
    check("openai list_models", "gpt-4o-mini" in c.list_models())
    check("openai is_available", c.is_available() is True)
    check("openai has_model", c.has_model("gpt-4o") is True)  # bare-name match
    check("openai effective_model", c.effective_model() == "gpt-4o-mini")

    reply = c.chat_json("system", "user")
    check("openai chat returns dict", isinstance(reply, dict))
    check("openai chat parsed actions", reply.get("actions")[0]["cmd"] == "create_box")
    payload = _Handler.last_chat_payload
    check("openai posts /chat/completions shape",
          payload.get("response_format", {}).get("type") == "json_object")
    check("openai uses effective model", payload.get("model") == "gpt-4o-mini")
    check("openai sends Bearer auth", _Handler.last_auth == "Bearer sk-test")

    # --- Ollama-style client keeps working (style=ollama) ---------------------
    c2 = AiClient(base_url=base, model="qwen3:4b", style="ollama",
                  known_models=["qwen3:4b"])
    check("ollama style detected", c2.style == "ollama")
    check("ollama list_models", "qwen3:4b" in c2.list_models())
    c2.chat_json("system", "user")
    p2 = _Handler.last_chat_payload
    check("ollama posts format=json", p2.get("format") == "json")
    check("ollama includes num_ctx when >0", p2.get("options", {}).get("num_ctx") == 8192)

    c3 = AiClient(base_url=base, model="qwen3:4b", style="ollama", num_ctx=0,
                  known_models=["qwen3:4b"])
    c3.chat_json("system", "user")
    p3 = _Handler.last_chat_payload
    check("ollama omits num_ctx when 0", "num_ctx" not in p3.get("options", {}))

    # --- thinking / reasoning models (R1, Qwen3-thinking, DeepSeek) ------------
    # These models emit a reasoning preamble wrapped in <think>…explain tags (which
    # may themselves contain braces, quotes and code) followed by the JSON answer.
    # The parser must strip the reasoning first and extract the answer robustly.
    thinking_cases = {
        "tagged preamble": '<think>ok</think>\n{"a": 1}',
        "reason with braces": '<think>build {width:1} ok</think>\n{"actions": [{"t": 1}]}',
        "thinking tags": '<thinking>reason {x}</thinking>\n{"a": 1}',
        "answer then reason": '{"a": 1}\n<think>done{x}</thinking>',
        "untagged trailing": '{"a": 1}\nthen I reason about {x} further',
        "prose before": 'Sure: {"a": 1}',
        "braces in string": '{"a": "use {this} verbatim"}',
        "empty reasoning": '<think></think>{"a": 1}',
    }
    thinking_expected = {
        "tagged preamble": {"a": 1},
        "reason with braces": {"actions": [{"t": 1}]},
        "thinking tags": {"a": 1},
        "answer then reason": {"a": 1},
        "untagged trailing": {"a": 1},
        "prose before": {"a": 1},
        "braces in string": {"a": "use {this} verbatim"},
        "empty reasoning": {"a": 1},
    }
    for name, reply in thinking_cases.items():
        try:
            check(f"thinking parse: {name}", _parse_json_object(reply) == thinking_expected[name])
        except Exception:
            check(f"thinking parse: {name}", False)

    # --- graceful degradation (fake server returning HTTP 500) ----------------
    # NOTE: this sandbox proxies loopback HTTP to "{}", so we cannot rely on a
    # dead port actually failing. A 500 response deterministically exercises the
    # AiUnavailable path. The design contract: low-level requests surface
    # AiUnavailable, while the high-level API degrades gracefully (never raises).
    err_srv = HTTPServer(("127.0.0.1", 0), _ErrorHandler)
    threading.Thread(target=err_srv.serve_forever, daemon=True).start()
    ep = f"http://127.0.0.1:{err_srv.server_address[1]}"
    dead = AiClient(base_url=ep, model="gpt-4o-mini", style="openai",
                    known_models=[])
    try:
        dead._get("/models")
        check("error server -> _get raises AiUnavailable", False)
    except AiUnavailable:
        check("error server -> _get raises AiUnavailable", True)
    try:
        lm = dead.list_models()  # must NOT raise; falls back to the known list
        check("error server -> list_models degrades (no raise)", True)
    except Exception:
        check("error server -> list_models degrades (no raise)", False)
        lm = None
    check("error server -> list_models falls back to known list", lm == [])
    try:
        av = dead.is_available()  # must NOT raise, returns a bool
        check("error server -> is_available never raises", isinstance(av, bool))
    except Exception:
        check("error server -> is_available never raises", False)

    # --- OllamaUnavailable is a subclass of AiUnavailable (backward compat) -----
    check("OllamaUnavailable subclass",
          issubclass(ollama_client.OllamaUnavailable, AiUnavailable))
    try:
        raise ollama_client.OllamaUnavailable("boom")
    except AiUnavailable:
        check("OllamaUnavailable caught as AiUnavailable", True)

    # --- config layer ---------------------------------------------------------
    # Point at a non-existent file so the defaults are exercised regardless of any
    # real config the host machine has at the default location.
    _no_cfg = os.path.join(tempfile.gettempdir(), "ai_client_test_no_config.json")
    cfg = ai_config.load_config(path=_no_cfg)  # no file -> defaults
    check("default provider is local", cfg.provider_name == "local")
    check("default base_url is ollama", cfg.base_url == "http://127.0.0.1:11434")
    check("default model", cfg.model == "qwen3:4b")
    check("default num_ctx", cfg.num_ctx == 8192)
    check("local provider style ollama", cfg.style == "ollama")  # provider style beats global "auto"

    # env override (backward compatible)
    os.environ["FREECAD_AGENT_OLLAMA_MODEL"] = "llama3.1:8b"
    try:
        cfg2 = ai_config.load_config()
        check("env overrides model", cfg2.model == "llama3.1:8b")
    finally:
        del os.environ["FREECAD_AGENT_OLLAMA_MODEL"]

    # file override + provider selection
    sample = {
        "default_provider": "openai",
        "providers": [
            {"name": "local", "base_url": "http://127.0.0.1:11434", "style": "ollama",
             "models": ["qwen3:4b"]},
            {"name": "openai", "base_url": "https://api.openai.com/v1",
             "api_key": "sk-xyz", "models": ["gpt-4o-mini", "gpt-4o"]},
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "ai_config.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(sample, fh)
        cfg3 = ai_config.load_config(p)
        check("file selects openai provider", cfg3.provider_name == "openai")
        check("file base_url", cfg3.base_url == "https://api.openai.com/v1")
        check("file api_key", cfg3.api_key == "sk-xyz")
        check("file default model from provider", cfg3.model == "gpt-4o-mini")

        # build_client() produces a working client from the resolved config
        client = ai_config.build_client(cfg3)
        check("build_client style openai", client.style == "openai")
        check("build_client api_key", client.api_key == "sk-xyz")
        check("build_client model", client.model == "gpt-4o-mini")
        check("build_client has chat_json", callable(client.chat_json))

    # --- write_sample_config writes a valid, loadable file --------------------
    with tempfile.TemporaryDirectory() as d:
        sp = ai_config.write_sample_config(os.path.join(d, "ai_config.json"))
        check("sample file exists", sp.exists())
        reloaded = ai_config.load_config(str(sp))
        check("sample is loadable", reloaded.provider_name == "local")
        # idempotent: writing again does not overwrite
        with open(sp, "a", encoding="utf-8") as fh:
            fh.write("\n// edit\n")
        sp2 = ai_config.write_sample_config(str(sp))
        check("sample idempotent", sp2 == sp)

    # --- report ----------------------------------------------------------------
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    print(f"PASS - {passed}/{len(results)} ai client/config checks")
    assert passed == len(results), f"{len(results) - passed} checks failed"


if __name__ == "__main__":
    run_scenarios()
