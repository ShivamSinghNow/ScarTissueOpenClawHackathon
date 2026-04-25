"""Two-stage bug-fix commit classifier.

Stage 1: extended keyword regex catches the obvious candidates (high recall,
modest precision ~70%). Stage 2: a batched LLM pass over the candidates,
asking the model to label each as a real bug fix vs documentation/typo/style
and to emit a one-sentence symptom summary that becomes part of the
embedding text downstream.

The LLM stage is optional. When disabled (SCARTISSUE_DISABLE_LLM_CLASSIFIER=1
or no classifier is passed to GitMiner.mine), we fall back to extended-keyword
classification only — useful for fast iteration and offline testing.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import anthropic

# ── Stage 1: extended keyword classifier ──────────────────────────────────────

# Words that suggest the commit fixes a real defect. Drawn from the standard
# MSR keyword set (Mockus & Votta 2000, Ray et al. 2014).
_POSITIVE_RE = re.compile(
    r"\b(?:fix|fixes|fixed|bug|bugs|bugfix|hotfix|patch|patches|regression|"
    r"revert|reverts|crash|crashes|leak|leaks|broken|fault|faults|defect|"
    r"defects|race|deadlock|deadlocks|npe|nullpointerexception|incorrect|"
    r"wrong|fail|fails|failed|failure|stale|hang|hangs|corrupt|corrupted|"
    r"overflow|underflow|panic|panics|segfault|exception|errno)\b",
    re.IGNORECASE,
)

# Bug-fix-shaped subjects that are almost always non-functional (docs, style,
# typo). Subtracting these from the positive set is the single biggest precision
# lift over the original "any 'fix' keyword" rule.
_NEGATIVE_RE = re.compile(
    r"\b(?:fix(?:es|ed)?|patch(?:es|ed)?|update[ds]?)\s+"
    r"(?:a\s+|the\s+|some\s+)?"
    r"(typo|typos|lint(?:ing|er)?|format(?:ting)?|style|styling|doc|docs|"
    r"documentation|comment|comments|indent(?:ation)?|import|imports|"
    r"whitespace|spelling|grammar|spacing|wording|wording|copy|markdown|"
    r"readme|changelog|todo|todos|copyright|license|version|changelog)\b",
    re.IGNORECASE,
)

# Commit closes an issue. Stronger signal than any keyword on its own.
_CLOSING_RE = re.compile(r"(?:fixes|closes|resolves)\s+#\d+", re.IGNORECASE)

# Issue references (separate from negative patterns).
_ISSUE_RE = re.compile(r"(?:fixes|closes|resolves|fix)\s+#(\d+)", re.IGNORECASE)

# Revert detection (subset of bug fixes — explicit undo of a prior change).
_REVERT_HEADER_RE = re.compile(r'^Revert\s+"', re.IGNORECASE)
_REVERT_BODY_RE = re.compile(r"this reverts commit", re.IGNORECASE)


def is_revert(message: str) -> bool:
    return bool(_REVERT_HEADER_RE.match(message)) or bool(_REVERT_BODY_RE.search(message))


def keyword_classify(message: str) -> bool:
    """Stage 1: returns True if this commit is a candidate bug fix."""
    if not message:
        return False
    first_line = message.splitlines()[0]

    # Reverts always count as candidates.
    if is_revert(message):
        return True

    # Closing-issue patterns are strong signal — accept even if subject is short.
    if _CLOSING_RE.search(message):
        if _NEGATIVE_RE.search(first_line):
            return False
        return True

    if _NEGATIVE_RE.search(first_line):
        return False

    return bool(_POSITIVE_RE.search(first_line))


def extract_issue_refs(message: str) -> list[int]:
    return [int(n) for n in _ISSUE_RE.findall(message or "")]


# ── Stage 2: LLM precision filter ─────────────────────────────────────────────


_BUG_TYPES = (
    "async_bug",
    "race_condition",
    "memory_leak",
    "retry_idempotency",
    "streaming_glitch",
    "auth_failure",
    "off_by_one",
    "validation",
    "error_handling",
    "concurrency",
    "data_corruption",
    "performance_regression",
    "other",
)

_SYSTEM = (
    "You classify git commits. Each input has a sha, message, and a diff "
    "excerpt. For each, decide whether it fixes a real functional defect "
    "(crash, wrong result, race, leak, regression, etc.) versus a "
    "non-functional change (docs, formatting, refactor, dependency bump, "
    "test-only, comment, typo). Return JSON only with this exact shape:\n"
    '{"results":[{"sha":"<sha>","is_bug_fix":true|false,'
    '"bug_type":"<one of: async_bug|race_condition|memory_leak|'
    "retry_idempotency|streaming_glitch|auth_failure|off_by_one|"
    "validation|error_handling|concurrency|data_corruption|"
    'performance_regression|other>","summary":"<one short sentence '
    "describing the symptom the fix addresses, or empty if "
    'is_bug_fix is false>"}]}\n'
    "Be strict: 'fix typo', 'fix lint', 'update docs', 'refactor X', "
    "'bump dependency', 'add test for Y' are NOT bug fixes. A revert is "
    "a bug fix only if it undoes a prior buggy change."
)


@dataclass(frozen=True)
class ClassifierResult:
    is_bug_fix: bool
    bug_type: str
    summary: str


class BugFixClassifier:
    """Batched LLM classifier for the candidate set produced by stage 1."""

    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str = "claude-sonnet-4-5-20250929",
        cache_path: Path | None = None,
        batch_size: int = 8,
        max_diff_chars: int = 2000,
    ) -> None:
        self._client = client or anthropic.Anthropic()
        self._model = model
        self._cache_path = cache_path
        self._batch_size = batch_size
        self._max_diff_chars = max_diff_chars
        self._cache: dict[str, dict] = self._load_cache()

    def _load_cache(self) -> dict[str, dict]:
        if self._cache_path is None or not self._cache_path.exists():
            return {}
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except Exception as exc:
            print(f"[classifier] Warning: ignoring unreadable cache: {exc}")
        return {}

    def _save_cache(self) -> None:
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(self._cache, indent=2), encoding="utf-8")

    def classify_many(
        self,
        candidates: Sequence[tuple[str, str, str]],
    ) -> dict[str, ClassifierResult]:
        """Classify a batch of (sha, message, diff_excerpt) triples.

        Returns sha → ClassifierResult. Cached results are reused without
        making API calls; uncached batches are sent to the model.
        """
        out: dict[str, ClassifierResult] = {}
        pending: list[tuple[str, str, str]] = []

        for sha, msg, diff in candidates:
            cached = self._cache.get(sha)
            if cached:
                out[sha] = ClassifierResult(
                    is_bug_fix=bool(cached.get("is_bug_fix", False)),
                    bug_type=str(cached.get("bug_type", "other")),
                    summary=str(cached.get("summary", "")),
                )
            else:
                pending.append((sha, msg, diff))

        for i in range(0, len(pending), self._batch_size):
            batch = pending[i : i + self._batch_size]
            classified = self._classify_batch(batch)
            for sha, result in classified.items():
                out[sha] = result
                self._cache[sha] = {
                    "is_bug_fix": result.is_bug_fix,
                    "bug_type": result.bug_type,
                    "summary": result.summary,
                }
            self._save_cache()
            print(
                f"[classifier] Classified {min(i + len(batch), len(pending))}/{len(pending)} "
                f"uncached commits ({len(out) - len(pending) + i + len(batch)} total)"
            )

        return out

    def _classify_batch(
        self,
        batch: list[tuple[str, str, str]],
    ) -> dict[str, ClassifierResult]:
        if not batch:
            return {}

        payload = {
            "commits": [
                {
                    "sha": sha,
                    "message": (msg or "")[:800],
                    "diff_excerpt": (diff or "")[: self._max_diff_chars],
                }
                for sha, msg, diff in batch
            ]
        }

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=2048,
                    temperature=0.0,
                    system=_SYSTEM,
                    messages=[{"role": "user", "content": json.dumps(payload)}],
                )
                text = _extract_text(response)
                results = _parse_response(text)
                shas_in_batch = {sha for sha, _, _ in batch}
                # Default any missing sha to a permissive result so we don't
                # silently drop commits if the model omits some entries.
                out: dict[str, ClassifierResult] = {}
                for sha in shas_in_batch:
                    if sha in results:
                        out[sha] = results[sha]
                    else:
                        out[sha] = ClassifierResult(
                            is_bug_fix=True, bug_type="other", summary=""
                        )
                return out
            except Exception as exc:
                last_exc = exc
                if attempt == 2:
                    break
                import time as _time
                _time.sleep(2 ** attempt)

        # On total failure, default to permissive (preserve recall).
        print(f"[classifier] Batch failed after retries: {last_exc}")
        return {
            sha: ClassifierResult(is_bug_fix=True, bug_type="other", summary="")
            for sha, _, _ in batch
        }


# ── helpers ───────────────────────────────────────────────────────────────────


def _extract_text(response) -> str:
    chunks: list[str] = []
    for block in getattr(response, "content", []):
        text = getattr(block, "text", None)
        if text is not None:
            chunks.append(text)
    return "\n".join(chunks).strip()


def _parse_response(text: str) -> dict[str, ClassifierResult]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    data = json.loads(stripped)
    out: dict[str, ClassifierResult] = {}
    for item in data.get("results", []):
        sha = str(item.get("sha", ""))
        if not sha:
            continue
        bug_type = str(item.get("bug_type", "other"))
        if bug_type not in _BUG_TYPES:
            bug_type = "other"
        out[sha] = ClassifierResult(
            is_bug_fix=bool(item.get("is_bug_fix", False)),
            bug_type=bug_type,
            summary=str(item.get("summary", "")).strip(),
        )
    return out


def llm_classifier_enabled() -> bool:
    return os.environ.get("SCARTISSUE_DISABLE_LLM_CLASSIFIER", "").lower() not in (
        "1", "true", "yes",
    )
