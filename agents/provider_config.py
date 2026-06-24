"""
LLM provider + model resolution.

Reads the active provider, model, and API key from the DB-backed
`app_settings` KV store first; falls back to legacy env vars
(`AGENT_PROVIDER`, `AGENT_MODEL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
etc.) so deployments that haven't yet saved to the UI keep working.

This module is pure config — no LLM imports, no streaming logic, no
network calls. `_get_llm()` in `agents/nodes.py` calls
`load_active_provider_config()` on every invocation, so swapping the
active provider in the DB takes effect on the next LLM call without a
server restart.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Provider catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderDef:
    """Static definition of a supported provider.

    `kind` is the routing tag consumed by `_get_llm()`:
      - "anthropic"  → ChatAnthropic
      - "openai"     → ChatOpenAI  (OpenAI proper + every OpenAI-compat
                                 endpoint — Google Gemini, DeepSeek, Groq,
                                 xAI, Mistral, Together, MiniMax)
    """
    id: str
    name: str
    kind: str
    default_model: str
    default_models: tuple[str, ...]
    default_base_url: Optional[str] = None
    env_api_key: Optional[str] = None
    env_base_url: Optional[str] = None
    needs_base_url: bool = False


KNOWN_PROVIDERS: tuple[ProviderDef, ...] = (
    ProviderDef(
        id="anthropic",
        name="Anthropic",
        kind="anthropic",
        default_model="claude-opus-4-8",
        default_models=(
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ),
        env_api_key="ANTHROPIC_API_KEY",
        needs_base_url=False,
    ),
    ProviderDef(
        id="openai",
        name="OpenAI",
        kind="openai",
        default_model="gpt-5",
        default_models=(
            "gpt-5",
            "gpt-5-mini",
            "gpt-4o",
            "gpt-4o-mini",
            "o4-mini",
        ),
        env_api_key="OPENAI_API_KEY",
        env_base_url="OPENAI_BASE_URL",
        needs_base_url=False,
    ),
    ProviderDef(
        id="google",
        name="Google (Gemini)",
        kind="openai",  # routed via Google OpenAI-compat endpoint
        default_model="gemini-2.5-pro",
        default_models=(
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.0-pro",
        ),
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env_api_key="GOOGLE_API_KEY",
        env_base_url="GOOGLE_BASE_URL",
        needs_base_url=False,
    ),
    ProviderDef(
        id="minimax",
        name="MiniMax",
        kind="openai",
        default_model="MiniMax-M3",
        default_models=("MiniMax-M3",),
        default_base_url="https://api.minimax.io/v1",
        env_api_key="MINIMAX_API_KEY",
        env_base_url="MINIMAX_BASE_URL",
        needs_base_url=False,
    ),
    ProviderDef(
        id="deepseek",
        name="DeepSeek",
        kind="openai",
        default_model="deepseek-chat",
        default_models=("deepseek-chat", "deepseek-reasoner"),
        default_base_url="https://api.deepseek.com/v1",
        env_api_key="DEEPSEEK_API_KEY",
        env_base_url="DEEPSEEK_BASE_URL",
        needs_base_url=False,
    ),
    ProviderDef(
        id="groq",
        name="Groq",
        kind="openai",
        default_model="llama-3.3-70b-versatile",
        default_models=(
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
        ),
        default_base_url="https://api.groq.com/openai/v1",
        env_api_key="GROQ_API_KEY",
        env_base_url="GROQ_BASE_URL",
        needs_base_url=False,
    ),
    ProviderDef(
        id="xai",
        name="xAI (Grok)",
        kind="openai",
        default_model="grok-3",
        default_models=("grok-3", "grok-3-mini", "grok-2"),
        default_base_url="https://api.x.ai/v1",
        env_api_key="XAI_API_KEY",
        env_base_url="XAI_BASE_URL",
        needs_base_url=False,
    ),
    ProviderDef(
        id="mistral",
        name="Mistral",
        kind="openai",
        default_model="mistral-large-latest",
        default_models=(
            "mistral-large-latest",
            "mistral-small-latest",
            "codestral-latest",
        ),
        default_base_url="https://api.mistral.ai/v1",
        env_api_key="MISTRAL_API_KEY",
        env_base_url="MISTRAL_BASE_URL",
        needs_base_url=False,
    ),
    ProviderDef(
        id="together",
        name="Together",
        kind="openai",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        default_models=(
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "meta-llama/Llama-3.1-405B-Instruct-Turbo",
            "Qwen/Qwen2.5-72B-Instruct-Turbo",
        ),
        default_base_url="https://api.together.xyz/v1",
        env_api_key="TOGETHER_API_KEY",
        env_base_url="TOGETHER_BASE_URL",
        needs_base_url=False,
    ),
)


def get_provider_def(provider_id: str) -> Optional[ProviderDef]:
    pid = (provider_id or "").lower().strip()
    for p in KNOWN_PROVIDERS:
        if p.id == pid:
            return p
    return None


# ---------------------------------------------------------------------------
# Resolved config (what _get_llm actually consumes)
# ---------------------------------------------------------------------------

@dataclass
class ProviderConfig:
    provider: str
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None


def _db_get(key: str) -> Optional[str]:
    """Read from app_settings KV. Imported lazily to keep this module
    importable from anywhere (including sub-agent threads that don't have
    the DB open)."""
    try:
        from server import db as _db
        return _db.get_app_setting(key)
    except Exception:
        return None


def load_active_provider_config() -> ProviderConfig:
    """Resolve the active provider/model/credentials.

    Precedence: DB (app_settings) > env > built-in defaults. Unknown
    provider ids in the DB fall back to the first known provider and log
    a warning — never crash the LLM call path."""
    raw_provider = (
        _db_get("active_provider")
        or os.getenv("AGENT_PROVIDER")
        or "anthropic"
    )
    provider_id = raw_provider.lower().strip()
    defn = get_provider_def(provider_id)
    if defn is None:
        # Don't blow up the agent — pick a sane default and warn.
        defn = KNOWN_PROVIDERS[0]
        provider_id = defn.id
        try:
            import logging
            logging.getLogger(__name__).warning(
                "provider_config: unknown active_provider=%r — falling back to %s",
                raw_provider, defn.id,
            )
        except Exception:
            pass

    model = (
        _db_get("active_model")
        or os.getenv("AGENT_MODEL")
        or defn.default_model
    )

    api_key = (
        _db_get(f"{provider_id}_api_key")
        or (os.getenv(defn.env_api_key) if defn.env_api_key else None)
        or None
    )

    base_url = (
        _db_get(f"{provider_id}_base_url")
        or (os.getenv(defn.env_base_url) if defn.env_base_url else None)
        or defn.default_base_url
        or None
    )

    return ProviderConfig(
        provider=provider_id,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )


def list_providers(active: ProviderConfig) -> list[dict]:
    """Serialize the catalog for the GET /api/admin/providers response.

    `has_key` is true if EITHER the DB has a key OR the relevant env var
    is set — never leaks the key value, just whether it's configured."""
    out: list[dict] = []
    for p in KNOWN_PROVIDERS:
        db_key = _db_get(f"{p.id}_api_key")
        env_key = os.getenv(p.env_api_key) if p.env_api_key else None
        has_key = bool((db_key and db_key.strip()) or (env_key and env_key.strip()))
        out.append({
            "id": p.id,
            "name": p.name,
            "kind": p.kind,
            "default_model": p.default_model,
            "default_models": list(p.default_models),
            "default_base_url": p.default_base_url,
            "needs_base_url": p.needs_base_url,
            "has_key": has_key,
            "is_active": p.id == active.provider,
        })
    return out


def all_provider_ids() -> set[str]:
    """Helper for API validation."""
    return {p.id for p in KNOWN_PROVIDERS}