"""
engine/ai_config.py - provider/model/config for the OpenAI-compatible engine.

GOAL: the engine must talk to ANY OpenAI-compatible API, not only Ollama, and the
provider / API key / model / timeout must live in a plain CONFIG FILE (VS Code /
other add-on style) instead of hard-coded values or a pile of environment vars.

Design (principle 5: config is neutral data; principle 9: graceful degradation):
  - The config is JSON (stdlib only) so the engine needs NO third-party packages.
    If PyYAML happens to be importable, a .yaml/.yml file is read too.
  - Default location: ~/.freecad-agent/ai_config.json, overridable with the
    FREECAD_AGENT_CONFIG environment variable (handy for tests / multiple setups).
  - If the file is missing or invalid, built-in sane DEFAULTS are used, so the
    agent still runs (with the local Ollama provider) out of the box.
  - Environment variables still override individual values for backward
    compatibility (FREECAD_AGENT_OLLAMA_*, FREECAD_AGENT_AI_*).
  - build_client() resolves the "default_provider" into a ready-to-use AiClient.

CONFIG SCHEMA (all fields optional; sensible defaults filled in):

  {
    "default_provider": "local",          // a provider name, or a base URL
    "base_url": null,                      // global default base URL
    "api_key": null,                       // global default API key
    "model": "qwen3:4b",                   // global default model
    "style": "auto",                       // "openai" | "ollama" | "auto"
    "timeout": null,                       // seconds/call, or null = unlimited
    "num_ctx": 8192,                       // context window (Ollama), or 0 = default
    "temperature": 0.0,
    "models": ["qwen3:4b"],                // global default model list
    "providers": [                         // named providers (added/removed freely)
      {"name": "local", "base_url": "http://127.0.0.1:11434", "style": "ollama",
       "api_key": "ollama", "models": ["qwen3:4b", "llama3.1:8b"]},
      {"name": "openai", "base_url": "https://api.openai.com/v1",
       "api_key": "sk-...", "models": ["gpt-4o-mini", "gpt-4o"]},
      {"name": "groq", "base_url": "https://api.groq.com/openai/v1",
       "api_key": "gsk_...", "models": ["llama-3.3-70b-versatile"]},
      {"name": "lmstudio", "base_url": "http://localhost:1234",
       "models": ["local-model"]}
    ]
  }

  To switch providers, set "default_provider" to the name you want (or paste a
  base URL directly). To add a provider, copy one of the examples into "providers"
  and give it a "name".
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai_client import DEFAULT_NUM_CTX, DEFAULT_TIMEOUT, AiClient


# --- locations ---------------------------------------------------------------

def default_config_dir() -> Path:
    """Directory that holds the config + engine log (matches the add-on's log dir)."""
    return Path.home() / ".freecad-agent"


def default_config_path() -> Path:
    p = os.environ.get("FREECAD_AGENT_CONFIG")
    if p:
        return Path(p).expanduser()
    return default_config_dir() / "ai_config.json"


def config_path() -> Path:
    """The config file actually used: env override, else the default location."""
    return default_config_path()


# --- built-in defaults (so the engine runs with no file at all) --------------

DEFAULT_PROVIDERS: List[Dict[str, Any]] = [
    {
        "name": "local",
        "base_url": "http://127.0.0.1:11434",
        "style": "ollama",
        "api_key": "ollama",
        "models": ["qwen3:4b", "llama3.1:8b"],
    },
    {
        "name": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": None,
        "models": ["gpt-4o-mini", "gpt-4o"],
    },
    {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": None,
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    },
    {
        "name": "lmstudio",
        "base_url": "http://localhost:1234",
        "api_key": None,
        "models": ["local-model"],
    },
]

DEFAULTS: Dict[str, Any] = {
    "default_provider": "local",
    "base_url": None,
    "api_key": None,
    "model": "qwen3:4b",
    "style": "auto",
    "timeout": None,
    "num_ctx": DEFAULT_NUM_CTX,
    "temperature": 0.0,
    "models": ["qwen3:4b"],
    "providers": DEFAULT_PROVIDERS,
}


# --- the resolved config -----------------------------------------------------

@dataclass
class Config:
    """Fully-resolved provider settings for one AiClient."""
    provider_name: str
    base_url: str
    api_key: Optional[str]
    model: str
    style: str
    timeout: "float | None"
    num_ctx: int
    temperature: float
    models: List[str] = field(default_factory=list)


def _as_float(value: Any) -> "float | None":
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-merge override onto base for the scalar top-level keys."""
    merged = dict(base)
    for k, v in (override or {}).items():
        if v is not None:
            merged[k] = v
    return merged


def load_config(path: Optional[str] = None) -> Config:
    """
    Load and resolve the AI config from `path` (or the default location), layered
    over the built-in DEFAULTS and then environment overrides. Never raises: a
    missing/invalid file yields the built-in defaults (graceful degradation).
    """
    cfg: Dict[str, Any] = dict(DEFAULTS)
    providers: List[Dict[str, Any]] = list(DEFAULTS["providers"])

    # 1) file (JSON, or YAML if PyYAML is available)
    path = path or None
    raw = _read_config_file(path or config_path())
    if raw:
        cfg = _merge(cfg, raw)
        if isinstance(raw.get("providers"), list) and raw.get("providers"):
            providers = raw["providers"]

    # 2) environment overrides layered onto the file config
    cfg = _apply_env(cfg)

    resolved = _resolve(cfg, providers)

    # 3) env vars are the highest-priority layer (applied after resolution,
    #    because _resolve() lets the chosen provider's own values win).
    _apply_env_to_config(resolved)

    return resolved


def _read_config_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        path = Path(path)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        ext = path.suffix.lower()
        if ext in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
            except Exception:
                # No PyYAML: fall back to JSON parsing (most YAML subsets parse as JSON).
                return json.loads(text)
            try:
                return yaml.safe_load(text) or {}
            except Exception:
                return json.loads(text)
        return json.loads(text)
    except Exception as exc:  # invalid config: degrade to defaults, do not crash
        print(f"[ai_config] ignoring invalid config {path}: {exc}")
        return None


def _env_overrides() -> Dict[str, Any]:
    """The FREECAD_AGENT_* env vars, read fresh so tests can mutate os.environ."""
    return {
        "base_url": os.environ.get("FREECAD_AGENT_OLLAMA_URL")
        or os.environ.get("FREECAD_AGENT_AI_BASE_URL"),
        "model": os.environ.get("FREECAD_AGENT_OLLAMA_MODEL")
        or os.environ.get("FREECAD_AGENT_AI_MODEL"),
        "api_key": os.environ.get("FREECAD_AGENT_AI_API_KEY"),
        "timeout": os.environ.get("FREECAD_AGENT_OLLAMA_TIMEOUT")
        or os.environ.get("FREECAD_AGENT_AI_TIMEOUT"),
        "num_ctx": os.environ.get("FREECAD_AGENT_OLLAMA_NUM_CTX")
        or os.environ.get("FREECAD_AGENT_AI_NUM_CTX"),
    }


def _apply_env(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Layer the FREECAD_AGENT_* env vars on top of the file config."""
    for key, value in _env_overrides().items():
        if value is not None:
            cfg[key] = value
    return cfg


def _apply_env_to_config(cfg: Config) -> Config:
    """Apply FREECAD_AGENT_* env overrides to the resolved Config.

    This is the HIGHEST-priority layer: because _resolve() lets the chosen
    provider's own values win, an env override (e.g. FREECAD_AGENT_OLLAMA_MODEL)
    would otherwise be ignored. Applying it last guarantees env wins.
    """
    overrides = _env_overrides()
    if overrides.get("base_url"):
        cfg.base_url = overrides["base_url"]
    if overrides.get("model"):
        cfg.model = overrides["model"]
    if overrides.get("api_key"):
        cfg.api_key = overrides["api_key"]
    if overrides.get("timeout") is not None:
        cfg.timeout = _as_float(overrides["timeout"])
    if overrides.get("num_ctx") is not None:
        cfg.num_ctx = int(overrides["num_ctx"])
    return cfg


def _resolve(cfg: Dict[str, Any], providers: List[Dict[str, Any]]) -> Config:
    """Pick the default provider and merge global + provider values."""
    default_name = cfg.get("default_provider")
    provider = None
    for p in providers:
        if not isinstance(p, dict):
            continue
        if default_name and (p.get("name") == default_name
                             or p.get("base_url") == default_name
                             or p.get("url") == default_name):
            provider = p
            break
    if provider is None and default_name and "://" in str(default_name):
        # default_provider is a bare base URL: synthesise a one-off provider.
        provider = {"name": default_name, "base_url": default_name,
                    "models": [cfg.get("model")]}

    prov_base_url = provider.get("base_url") if provider else None
    prov_style = provider.get("style") if provider else None
    prov_models = provider.get("models") if provider else None
    prov_key = provider.get("api_key") if provider else None

    base_url = prov_base_url or cfg.get("base_url")
    if not base_url:
        base_url = DEFAULT_PROVIDERS[0]["base_url"]  # Ollama, keeps old behaviour
    model = (prov_models and prov_models[0]) or cfg.get("model") or DEFAULTS["model"]
    style = prov_style or cfg.get("style") or "auto"
    api_key = prov_key if prov_key is not None else cfg.get("api_key")
    timeout = _as_float(cfg.get("timeout"))
    num_ctx = cfg.get("num_ctx") or DEFAULT_NUM_CTX
    temperature = cfg.get("temperature")
    if temperature in (None, ""):
        temperature = 0.0
    models = prov_models or list(cfg.get("models") or [])

    provider_name = provider.get("name") if provider else (default_name or base_url)
    return Config(
        provider_name=str(provider_name),
        base_url=str(base_url),
        api_key=api_key,
        model=str(model),
        style=str(style),
        timeout=timeout,
        num_ctx=int(num_ctx),
        temperature=float(temperature),
        models=[str(m) for m in models if m],
    )


def build_client(cfg: Optional[Config] = None) -> AiClient:
    """Build a ready-to-use AiClient from the resolved config (or `cfg`)."""
    cfg = cfg if cfg is not None else load_config()
    return AiClient(
        base_url=cfg.base_url,
        model=cfg.model,
        timeout=cfg.timeout,
        num_ctx=cfg.num_ctx,
        temperature=cfg.temperature,
        api_key=cfg.api_key,
        style=cfg.style,
        known_models=cfg.models,
    )


def write_sample_config(path: Optional[str] = None) -> Path:
    """
    Write a fully-documented sample config to disk (idempotent). Returns the path.
    Existing files are left untouched so the user's edits are not clobbered.
    """
    path = Path(path) if path else config_path()
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SAMPLE_TEXT, encoding="utf-8")
    return path


_SAMPLE_TEXT = """\
# FreeCAD Agent - AI provider configuration (OpenAI-compatible).
#
# The engine talks to ANY OpenAI-compatible API. Edit this file (or set the
# FREECAD_AGENT_CONFIG env var to point elsewhere) to choose your provider.
#
#   * "default_provider" selects which provider to use: its "name" below, or a bare
#     base URL.
#   * To add a provider, copy one of the examples under "providers" and give it a
#     unique "name" (and its own API key).
#   * "style" is "openai" (most providers), "ollama" (local Ollama), or "auto"
#     (detected from the URL). Leave it "auto" unless you know otherwise.
#   * "timeout" is seconds per call, or null = wait as long as the model needs.
#   * "num_ctx" is the context window (Ollama); 0 = let the server decide.
#
# After editing: restart the engine (or just open a new task in the panel).

{
  "default_provider": "local",
  "style": "auto",
  "timeout": null,
  "num_ctx": 8192,
  "temperature": 0.0,
  "models": ["qwen3:4b"],
  "providers": [
    {
      "name": "local",
      "base_url": "http://127.0.0.1:11434",
      "style": "ollama",
      "api_key": "ollama",
      "models": ["qwen3:4b", "llama3.1:8b"]
    },
    {
      "name": "openai",
      "base_url": "https://api.openai.com/v1",
      "api_key": "sk-REPLACE_ME",
      "models": ["gpt-4o-mini", "gpt-4o"]
    },
    {
      "name": "groq",
      "base_url": "https://api.groq.com/openai/v1",
      "api_key": "gsk_REPLACE_ME",
      "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    },
    {
      "name": "deepseek",
      "base_url": "https://api.deepseek.com/v1",
      "api_key": "sk-REPLACE_ME",
      "models": ["deepseek-chat", "deepseek-reasoner"]
    },
    {
      "name": "lmstudio",
      "base_url": "http://localhost:1234",
      "api_key": "",
      "models": ["local-model"]
    }
  ]
}
"""
