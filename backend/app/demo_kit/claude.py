from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from anthropic import Anthropic

ATTACK_MODEL = "claude-sonnet-4-5-20250929"


class ClaudeClient:
    """Small Anthropic wrapper with retry and plain-text extraction."""

    def __init__(
        self,
        client: Anthropic | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or Anthropic()
        self._sleep = sleep

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str = ATTACK_MODEL,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> str:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
                # Strip leading/trailing markdown fences from the model output.
                # The attack generator asks for raw source code with no fences,
                # but the model often wraps anyway, which corrupts the resulting
                # patch with literal '```python' on line 1 of the target file.
                return _strip_code_fences(self._extract_text(response))
            except Exception as exc:
                last_exc = exc
                if attempt == 2:
                    break
                delay = 30 * (2**attempt) if exc.__class__.__name__ == "RateLimitError" else 2**attempt
                self._sleep(delay)
        raise RuntimeError("Claude request failed after 3 attempts") from last_exc

    def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        model: str = ATTACK_MODEL,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> Any:
        text = self.complete(
            system=system,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return json.loads(_strip_json_fences(text))

    @staticmethod
    def _extract_text(response: Any) -> str:
        chunks: list[str] = []
        for block in getattr(response, "content", []):
            text = getattr(block, "text", None)
            if text is not None:
                chunks.append(text)
            elif isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text", "")))
        return "\n".join(chunks).strip()


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def _strip_code_fences(text: str) -> str:
    """Strip a single leading ```... fence and trailing ``` from text-mode output.

    Trailing newline at the end of the file is preserved (Python source files
    conventionally end with one). Only outermost fences are stripped — fences
    that legitimately appear inside the file body (e.g. inside docstrings or
    string literals) are left alone.
    """
    if not text:
        return text
    lines = text.splitlines(keepends=True)
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines:
        last = lines[-1].rstrip("\n")
        if last.strip() == "```":
            lines = lines[:-1]
            # Keep a trailing newline to match the convention of source files.
            if lines and not lines[-1].endswith("\n"):
                lines[-1] = lines[-1] + "\n"
    return "".join(lines)
