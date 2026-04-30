from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from threading import Lock
from typing import Any

SPEND_LIMIT_MESSAGE = "ScarTissue is taking a break to manage costs. Try again tomorrow."

DAILY_SPEND_LIMIT_USD = 25.0
SONNET_45_INPUT_USD_PER_MTOK = 3.0
SONNET_45_OUTPUT_USD_PER_MTOK = 15.0


class SpendCapExceeded(RuntimeError):
    pass


@dataclass
class _TokenUsage:
    day: date
    input_tokens: int = 0
    output_tokens: int = 0


_usage = _TokenUsage(day=date.today())
_usage_lock = Lock()


def _estimated_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        (input_tokens / 1_000_000) * SONNET_45_INPUT_USD_PER_MTOK
        + (output_tokens / 1_000_000) * SONNET_45_OUTPUT_USD_PER_MTOK
    )


def _current_cost_usd() -> float:
    today = date.today()
    with _usage_lock:
        if _usage.day != today:
            _usage.day = today
            _usage.input_tokens = 0
            _usage.output_tokens = 0
        return _estimated_cost_usd(_usage.input_tokens, _usage.output_tokens)


def _record_usage(response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return

    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    today = date.today()
    with _usage_lock:
        if _usage.day != today:
            _usage.day = today
            _usage.input_tokens = 0
            _usage.output_tokens = 0
        _usage.input_tokens += input_tokens
        _usage.output_tokens += output_tokens


class _SafeMessages:
    def __init__(self, messages: Any) -> None:
        self._messages = messages

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        if _current_cost_usd() >= DAILY_SPEND_LIMIT_USD:
            raise SpendCapExceeded(SPEND_LIMIT_MESSAGE)

        response = await self._messages.create(*args, **kwargs)
        _record_usage(response)
        return response


class SafeAsyncAnthropic:
    """Thin guard around AsyncAnthropic that preserves the messages API used here."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.messages = _SafeMessages(client.messages)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
