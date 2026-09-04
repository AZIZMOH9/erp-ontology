"""One place where model calls actually happen.

Both paths go through here -- the base path's single structured call and the agent path's tool
loop -- so token accounting, cost, caching and provider differences are handled once.
"""

from __future__ import annotations

import random
import re
import threading
import time
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from erp_planner.llm.providers import PRICES, Provider, build_model, cache_prefix, usage_from

# Rate limits are a normal condition, not a failure: a free-tier Gemini key allows 20 requests
# per day per model, and every provider throttles a burst. The API usually says how long to wait.
# Matched against the message with spaces and underscores stripped, because each provider spells
# it differently: "RESOURCE_EXHAUSTED" (Google), "Rate limit reached" (OpenAI), "rate_limit_error"
# (Anthropic). Comparing compact forms catches all of them.
RATE_LIMIT_MARKERS = ("ratelimit", "429", "resourceexhausted", "quota", "toomanyrequests")
_RETRY_AFTER = re.compile(r"retry(?:_?delay|.{0,12}in)[^0-9]{0,12}([0-9]+(?:\.[0-9]+)?)", re.I)


def is_rate_limited(exc: BaseException) -> bool:
    blob = re.sub(r"[\s_-]+", "", f"{type(exc).__name__} {exc}".lower())
    return any(marker in blob for marker in RATE_LIMIT_MARKERS)


def retry_delay(exc: BaseException, attempt: int, cap: float = 60.0) -> float:
    """Honour the delay the provider asked for, else exponential backoff with jitter."""
    match = _RETRY_AFTER.search(str(exc))
    if match:
        return min(float(match.group(1)) + 0.5, cap)
    return min(2.0**attempt + random.uniform(0, 1), cap)


class Usage(BaseModel):
    """Running cost, tracked from the first call rather than discovered later."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    by_model: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0

    def add(
        self, model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read
        self.cache_write_tokens += cache_write
        self.calls += 1
        self.by_model[model] = self.by_model.get(model, 0) + 1
        in_price, out_price = PRICES.get(model, (0.0, 0.0))
        # Cache reads bill at ~0.1x input, writes at ~1.25x.
        self.cost_usd += (
            input_tokens * in_price
            + cache_read * in_price * 0.1
            + cache_write * in_price * 1.25
            + output_tokens * out_price
        ) / 1_000_000


class ModelRunner:
    """A model plus the bookkeeping around it. Safe to call from several threads."""

    def __init__(
        self,
        provider: Provider,
        model: str,
        api_key: str | None = None,
        max_tokens: int = 16000,
        usage: Usage | None = None,
        max_retries: int = 4,
        parse_retries: int = 3,
    ) -> None:
        self.provider = provider
        self.model_name = model
        self.usage = usage if usage is not None else Usage()
        self.max_retries = max_retries
        self.parse_retries = parse_retries
        self.rate_limit_waits = 0
        self.parse_retries_used = 0
        self._lock = threading.Lock()
        self._model = build_model(provider, model, api_key=api_key, max_tokens=max_tokens)

    def _with_retries(self, call, what: str):
        """Retry a throttled call. Anything else is a real failure and is raised at once."""
        last: BaseException | None = None
        for attempt in range(self.max_retries):
            try:
                return call()
            except Exception as exc:
                if not is_rate_limited(exc):
                    raise
                last = exc
                delay = retry_delay(exc, attempt)
                with self._lock:
                    self.rate_limit_waits += 1
                time.sleep(delay)
        raise RuntimeError(f"{what}: rate limited after {self.max_retries} attempts") from last

    # -- prompt construction ---------------------------------------------------------------
    def system(self, prefix: str) -> SystemMessage:
        """The stable prefix, marked for caching where the provider supports it."""
        return SystemMessage(content=cache_prefix(self.provider, prefix))

    def _record(self, response: Any) -> None:
        with self._lock:
            self.usage.add(self.model_name, *usage_from(response))

    def _structured(self, invoke, what: str, schema: type[BaseModel]) -> BaseModel:
        """Call, validate, and sample again when the answer will not parse.

        An answer that does not satisfy the schema is usually one malformed or truncated
        generation, not a standing condition -- the same cluster parses on the next attempt. It
        used to raise on the first one, which lost the whole cluster from the run for the sake of
        the few seconds a retry costs.
        """
        problem: Any = None
        for attempt in range(self.parse_retries):
            result = self._with_retries(invoke, what)
            # Recorded per attempt: a discarded answer was still generated and still billed.
            self._record(result["raw"])
            if result["parsed"] is not None:
                return result["parsed"]
            problem = result.get("parsing_error") or problem
            with self._lock:
                self.parse_retries_used += 1
            if attempt + 1 < self.parse_retries:
                time.sleep(min(2.0**attempt + random.uniform(0, 1), 10.0))
        raise ValueError(
            f"{self.model_name} returned no parseable {schema.__name__} in "
            f"{self.parse_retries} attempts" + (f": {problem}" if problem else "")
        )

    # -- the two call shapes ---------------------------------------------------------------
    def structured(self, prefix: str, user: str, schema: type[BaseModel]) -> BaseModel:
        """One call, answer validated against ``schema``."""
        model = self._model.with_structured_output(schema, include_raw=True)
        return self._structured(
            lambda: model.invoke([self.system(prefix), HumanMessage(content=user)]),
            f"{self.model_name} structured call",
            schema,
        )

    def tool_loop(
        self,
        prefix: str,
        user: str,
        tools: list,
        execute,
        max_iterations: int = 8,
    ) -> list[BaseMessage]:
        """Let the model gather evidence, and return the conversation it produced.

        ``execute(name, args) -> str`` runs one tool. The loop stops when the model stops asking,
        or at ``max_iterations`` -- an agent that will not stop investigating is a cost incident,
        so the cap is not optional.
        """
        from langchain_core.messages import ToolMessage

        bound = self._model.bind_tools(tools)
        messages: list[BaseMessage] = [self.system(prefix), HumanMessage(content=user)]

        for _ in range(max_iterations):
            response: AIMessage = self._with_retries(
                lambda: bound.invoke(messages), f"{self.model_name} tool loop"
            )
            self._record(response)
            messages.append(response)
            if not getattr(response, "tool_calls", None):
                break
            for call in response.tool_calls:
                try:
                    output = execute(call["name"], call["args"])
                except Exception as exc:  # a broken tool is a result, not a crashed run
                    output = f"Tool error: {type(exc).__name__}: {exc}"
                messages.append(ToolMessage(content=str(output), tool_call_id=call["id"]))
        return messages

    def structured_after(
        self, messages: list[BaseMessage], instruction: str, schema: type[BaseModel]
    ) -> BaseModel:
        """Close an investigation with a validated answer.

        Kept separate from the tool loop on purpose: mixing structured output with tool calling
        behaves differently on every provider, whereas 'investigate, then answer' behaves the same
        everywhere and is far easier to read back afterwards.
        """
        model = self._model.with_structured_output(schema, include_raw=True)
        return self._structured(
            lambda: model.invoke([*messages, HumanMessage(content=instruction)]),
            f"{self.model_name} final answer",
            schema,
        )
