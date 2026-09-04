"""Provider-neutral model construction.

The product is customer-hosted, so the model endpoint is the customer's decision -- some will use
Claude, some are contractually stuck with Azure OpenAI or Vertex. One factory, chosen from the CLI.

What is *not* portable is prompt caching. Anthropic takes explicit ``cache_control`` breakpoints,
OpenAI caches long prefixes automatically with no control surface, and Gemini has a separate
context-cache API with its own lifecycle. The prefix itself is built identically for every
provider (see :mod:`erp_planner.mapping.llm.rendering`); only the marking of it differs, and that lives
in :func:`cache_prefix`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel


class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"


# Defaults per provider: (bulk model, model for hard clusters).
DEFAULT_MODELS: dict[Provider, tuple[str, str]] = {
    Provider.ANTHROPIC: ("claude-sonnet-5", "claude-opus-5"),
    # UNVERIFIED: never run against a live OpenAI account. The Gemini defaults here were once
    # gemini-2.5-*, which turned out to be retired and 404'd; assume the same risk until someone
    # runs it and corrects this. Override with --model / --hard-model.
    Provider.OPENAI: ("gpt-5", "gpt-5"),
    # gemini-2.5-* now 404s ("no longer available"), so these are the current generation. Google
    # retires model ids faster than the others; --model overrides when this goes stale too.
    Provider.GOOGLE: ("gemini-3.8-flash", "gemini-pro-latest"),
}

# Where each provider's key is looked up when --api-key is not passed. First match wins.
API_KEY_ENV: dict[Provider, tuple[str, ...]] = {
    Provider.ANTHROPIC: ("ERP_PLANNER_API_KEY", "ANTHROPIC_API_KEY"),
    Provider.OPENAI: ("ERP_PLANNER_API_KEY", "OPENAI_API_KEY"),
    Provider.GOOGLE: ("ERP_PLANNER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def api_key_for(provider: Provider) -> str | None:
    import os

    for name in API_KEY_ENV[provider]:
        value = os.environ.get(name)
        if value:
            return value
    return None


def has_price(model: str) -> bool:
    """Whether a cost figure for this model is anything but a guess."""
    return model in PRICES

# USD per million tokens (input, output). Anthropic only: a run on another provider reports its
# cost as unknown rather than a number nobody checked. Add a row here to get a figure, and check
# the vendor's price list on the day you do -- a stale price reported confidently is worse than no
# price at all.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def default_models(provider: Provider) -> tuple[str, str]:
    return DEFAULT_MODELS[provider]


def build_model(
    provider: Provider,
    model: str,
    api_key: str | None = None,
    temperature: float | None = None,
    max_tokens: int = 16000,
    **kwargs: Any,
) -> BaseChatModel:
    """Construct a chat model. Imports are local so one provider's SDK is never required."""
    if provider is Provider.ANTHROPIC:
        from langchain_anthropic import ChatAnthropic

        params: dict[str, Any] = {"model": model, "max_tokens": max_tokens, **kwargs}
        if api_key:
            params["api_key"] = api_key
        # Claude 4.6+ rejects temperature alongside adaptive thinking, so it is only sent when
        # the caller explicitly asked for one.
        if temperature is not None:
            params["temperature"] = temperature
        return ChatAnthropic(**params)

    if provider is Provider.OPENAI:
        from langchain_openai import ChatOpenAI

        params = {"model": model, "max_tokens": max_tokens, **kwargs}
        if api_key:
            params["api_key"] = api_key
        if temperature is not None:
            params["temperature"] = temperature
        return ChatOpenAI(**params)

    from langchain_google_genai import ChatGoogleGenerativeAI

    params = {"model": model, "max_output_tokens": max_tokens, **kwargs}
    if api_key:
        params["google_api_key"] = api_key
    if temperature is not None:
        params["temperature"] = temperature
    return ChatGoogleGenerativeAI(**params)


# Anthropic will not cache a prefix shorter than this, and silently declines rather than erroring.
# The system prompt alone is ~566 tokens, which is why marking it achieved nothing.
MIN_CACHEABLE_TOKENS = 1024
CHARS_PER_TOKEN = 4


def cacheable(text: str) -> bool:
    return len(text) / CHARS_PER_TOKEN >= MIN_CACHEABLE_TOKENS


def cache_prefix(provider: Provider, text: str) -> str | list[dict[str, Any]]:
    """Return the prefix in whatever form makes the provider cache it.

    Anthropic gets an explicit breakpoint. OpenAI caches long prefixes on its own. Gemini needs a
    separate explicit-cache API that is not worth its lifecycle management at this size, so both
    receive plain text and simply pay full price.
    """
    if provider is Provider.ANTHROPIC and cacheable(text):
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
    return text


def usage_from(response: Any) -> tuple[int, int, int, int]:
    """(input, output, cache_read, cache_write) from a LangChain response.

    Providers report usage under different keys; LangChain normalises the top two and leaves the
    cache counters in provider-specific sub-dicts.
    """
    meta = getattr(response, "usage_metadata", None) or {}
    input_tokens = int(meta.get("input_tokens", 0) or 0)
    output_tokens = int(meta.get("output_tokens", 0) or 0)
    details = meta.get("input_token_details", {}) or {}
    cache_read = int(details.get("cache_read", 0) or 0)
    cache_write = int(details.get("cache_creation", 0) or 0)
    # LangChain reports input_tokens inclusive of cached reads; keep them disjoint for costing.
    return max(input_tokens - cache_read - cache_write, 0), output_tokens, cache_read, cache_write
