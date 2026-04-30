from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from threading import Lock

from fastapi import HTTPException
from slowapi import Limiter
from slowapi.util import get_remote_address

DAILY_LIMIT_MESSAGE = "Daily limit reached, try again tomorrow."

limiter = Limiter(key_func=get_remote_address, headers_enabled=True)


@dataclass
class _DailyCounter:
    day: date
    count: int = 0


_counter_lock = Lock()
_daily_counters: dict[str, _DailyCounter] = {
    "review": _DailyCounter(day=date.today()),
    "index": _DailyCounter(day=date.today()),
}


def reserve_daily_capacity(name: str, limit: int) -> None:
    """Reserve one slot from a process-local daily counter."""
    today = date.today()
    with _counter_lock:
        counter = _daily_counters.setdefault(name, _DailyCounter(day=today))
        if counter.day != today:
            counter.day = today
            counter.count = 0
        if counter.count >= limit:
            raise HTTPException(status_code=503, detail=DAILY_LIMIT_MESSAGE)
        counter.count += 1
