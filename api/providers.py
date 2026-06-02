"""
Provider configurations — all supported LLM providers.
No SDK. Just base URLs, headers, model names.

Our addition: unified provider system covering all models.
Rust only targets Anthropic + basic OpenAI compat separately.
"""

import os
import sys
from pathlib import Path

PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "base_url":    "https://api.anthropic.com",
        "endpoint":    "/v1/messages",
        "api_key_env": "ANTHROPIC_API_KEY",
        "format":      "anthropic",
        "models": [
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ],
        "default_model": "claude-opus-4-6",
    },
    "openai": {
        "base_url":    "https://api.openai.com",
        "endpoint":    "/v1/chat/completions",
        "api_key_env": "OPENAI_API_KEY",
        "format":      "openai",
        "models":      ["gpt-4o", "gpt-4-turbo", "gpt-4o-mini"],
        "default_model": "gpt-4o",
    },
    "ollama": {
        "base_url":    "http://localhost:11434",
        "endpoint":    "/v1/chat/completions",
        "api_key_env": None,               # no key needed
        "format":      "openai",           # ollama speaks OpenAI format
        "models":      ["llama3", "codellama", "mistral", "phi3", "deepseek-coder"],
        "default_model": "llama3",
    },
    "groq": {
        "base_url":    "https://api.groq.com",
        "endpoint":    "/v1/chat/completions",
        "api_key_env": "GROQ_API_KEY",
        "format":      "openai",
        "models":      ["llama3-70b-8192", "mixtral-8x7b-32768", "llama3-8b-8192"],
        "default_model": "llama3-70b-8192",
    },
    "deepseek": {
        "base_url":    "https://api.deepseek.com",
        "endpoint":    "/v1/chat/completions",
        "api_key_env": "DEEPSEEK_API_KEY",
        "format":      "openai",
        "models":      ["deepseek-coder", "deepseek-chat"],
        "default_model": "deepseek-coder",
    },
    "xai": {
        "base_url":    "https://api.x.ai",
        "endpoint":    "/v1/chat/completions",
        "api_key_env": "XAI_API_KEY",
        "format":      "openai",
        "models":      ["grok-beta"],
        "default_model": "grok-beta",
    },
    "mistral": {
        "base_url":    "https://api.mistral.ai",
        "endpoint":    "/v1/chat/completions",
        "api_key_env": "MISTRAL_API_KEY",
        "format":      "openai",
        "models":      ["mistral-large-latest", "mistral-medium-latest", "codestral-latest"],
        "default_model": "mistral-large-latest",
    },
}

# Short alias → provider:model
MODEL_ALIASES: dict[str, tuple[str, str]] = {
    "opus":     ("anthropic", "claude-opus-4-6"),
    "sonnet":   ("anthropic", "claude-sonnet-4-6"),
    "haiku":    ("anthropic", "claude-haiku-4-5-20251001"),
    "gpt4":     ("openai",    "gpt-4o"),
    "llama":    ("ollama",    "llama3"),
    "llama3":   ("ollama",    "llama3"),
    "codellama":("ollama",    "codellama"),
    "mistral":  ("ollama",    "mistral"),
    "groq":     ("groq",      "llama3-70b-8192"),
    "deepseek": ("deepseek",  "deepseek-coder"),
}


def resolve_provider_and_model(
    provider: str | None,
    model: str | None
) -> tuple[str, str]:
    """
    Given optional provider + model strings, return the resolved (provider, model).
    Handles aliases, defaults, env var fallbacks.
    """
    if model and model in MODEL_ALIASES:
        return MODEL_ALIASES[model]

    provider = provider or os.getenv("AGENT_PROVIDER", "anthropic")
    config   = PROVIDERS.get(provider)

    if not config:
        raise ValueError(
            f"Unknown provider '{provider}'. "
            f"Available: {', '.join(PROVIDERS.keys())}"
        )

    model = (
        model
        or os.getenv("AGENT_MODEL")
        or config["default_model"]
    )

    return provider, model


def get_api_key(provider: str) -> str:
    config  = PROVIDERS[provider]
    env_var = config.get("api_key_env")

    if env_var is None:
        return "ollama"    # Ollama doesn't need a real key

    key = os.getenv(env_var, "")
    if not key:
        raise ValueError(
            f"Missing API key for provider '{provider}'. "
            f"Set the {env_var} environment variable."
        )
    return key
