"""Phase C evaluation harness.

Produces the four numbers gating CLI readiness:
  1. Classifier precision on N=30 stage-1 candidates
  2. Retrieval top-5 recall on N=20 known fix commits (using buggy-parent
     diff as the query)
  3. False-positive warning rate on N=5 hand-picked clean PRs
  4. Catches at least one known reverted PR (regression harness against one
     fix commit)

Each sub-eval can be run independently:
    python -m scripts.phase_c_eval classifier --repo encode/httpx --n 30
    python -m scripts.phase_c_eval recall     --repo encode/httpx --n 20
    python -m scripts.phase_c_eval fp         --repo encode/httpx --pr-urls f.txt
    python -m scripts.phase_c_eval revert     --repo encode/httpx --fix-sha SHA
    python -m scripts.phase_c_eval all        --repo encode/httpx
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import anthropic
import git as git_lib

from app.services.bugfix_classifier import BugFixClassifier, keyword_classify
from app.services.git_miner import GitMiner
from app.services.nia_client import NiaClient
from app.services.pr_fetcher import PRFetcher, _parse_hunks, _unique_files, PRDiff
from app.services.reviewer import Reviewer
from app.services.scar_index import ScarIndex


# ── classifier precision ──────────────────────────────────────────────────────


@dataclass
class ClassifierSample:
    sha: str
    subject: str
    files: list[str]
    diff_excerpt: str
    llm_is_bug_fix: bool
    llm_bug_type: str
    llm_summary: str


def sample_classifier_candidates(repo: str, n: int = 30, max_walk: int = 2000, seed: int = 42) -> list[ClassifierSample]:
    """Walk recent commits, take the first `n` that pass the stage-1 keyword
    filter, run the stage-2 LLM classifier on them, and return the labeled set
    so a human can hand-grade each one.
    """
    miner = GitMiner()
    owner, name = repo.split("/", 1)
    git_repo = miner._get_repo(owner, name)

    print(f"[classifier-eval] Walking up to {max_walk} commits looking for {n} candidates…")
    candidates: list[tuple[str, str, list[str], str]] = []
    for commit in git_repo.iter_commits(max_count=max_walk):
        if not commit.parents or len(commit.parents) > 1:
            continue
        message = commit.message
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        if not keyword_classify(message):
            continue
        try:
            files = list(commit.stats.files.keys())
        except Exception:
            files = []
        try:
            diff = git_repo.git.diff(commit.parents[0].hexsha, commit.hexsha)[:3000]
        except Exception:
            diff = ""
        candidates.append((commit.hexsha, message, files, diff))
        if len(candidates) >= n:
            break

    print(f"[classifier-eval] Got {len(candidates)} candidates. Running stage-2 LLM…")

    classifier = BugFixClassifier()
    triples = [(sha, msg, diff) for sha, msg, _, diff in candidates]
    results = classifier.classify_many(triples)

    samples = []
    for sha, msg, files, diff in candidates:
        result = results.get(sha)
        samples.append(ClassifierSample(
            sha=sha,
            subject=(msg or "").splitlines()[0] if msg else "",
            files=files,
            diff_excerpt=diff[:500],
            llm_is_bug_fix=bool(result and result.is_bug_fix),
            llm_bug_type=result.bug_type if result else "?",
            llm_summary=result.summary if result else "",
        ))
    return samples


def write_classifier_samples(samples: list[ClassifierSample], out: Path) -> None:
    out.write_text(
        json.dumps([asdict(s) for s in samples], indent=2),
        encoding="utf-8",
    )
    print(f"[classifier-eval] Wrote {len(samples)} samples to {out}.")


# ── retrieval top-5 recall ────────────────────────────────────────────────────


@dataclass
class RecallProbe:
    fix_sha: str
    fix_subject: str
    files: list[str]
    rank_in_top5: Optional[int]   # 1..5 if found, None if missed
    top5_shas: list[str]


_HUNK_RE = __import__("re").compile(r"^(@@ -)(\d+)(?:,(\d+))?( \+)(\d+)(?:,(\d+))?( @@.*)")


def _invert_diff(diff_text: str) -> str:
    """Swap '+' / '-' lines and old/new ranges in @@ headers — produces the
    diff someone would write to undo the fix (i.e. reintroduce the bug)."""
    out: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("@@"):
            m = _HUNK_RE.match(line.rstrip("\n"))
            if m:
                _, os_, ol, _, ns, nl, rest = m.groups()
                ol = ol if ol is not None else "1"
                nl = nl if nl is not None else "1"
                out.append(f"@@ -{ns},{nl} +{os_},{ol} {rest.lstrip()}\n")
            else:
                out.append(line)
        elif line.startswith("---") or line.startswith("+++"):
            out.append(line)
        elif line.startswith("+"):
            out.append("-" + line[1:])
        elif line.startswith("-"):
            out.append("+" + line[1:])
        else:
            out.append(line)
    return "".join(out)


def measure_top5_recall(
    repo: str,
    n: int = 20,
    seed: int = 42,
) -> tuple[float, list[RecallProbe]]:
    """Pick `n` random indexed incidents. For each, take the FIX diff and
    invert it (so '+' becomes '-' and vice versa) — this is the regression-
    shaped query: it represents what someone undoing the fix would write.
    Recall@5 is the fraction where the original fix sha appears in the top-5
    hits.

    Why inverse-of-fix and not parent-of-buggy-parent: the buggy parent's
    parent is just whichever commit happened to land one earlier and is
    frequently unrelated to the bug. The inverse of the fix is, by
    construction, the bug being reintroduced — exactly the case the system
    is designed to catch.
    """
    scar = ScarIndex()
    col_name = repo.replace("/", "_").replace("-", "_").lower()
    col = scar._client.get_collection(name=col_name)
    count = col.count()
    print(f"[recall-eval] {count} incidents in collection {col_name}")
    sample_n = min(n, count)
    if sample_n == 0:
        return 0.0, []

    fetched = col.get(limit=count, include=["metadatas"])
    rng = random.Random(seed)
    indices = rng.sample(range(count), sample_n)
    chosen = [fetched["metadatas"][i] for i in indices]

    probes: list[RecallProbe] = []
    for i, meta in enumerate(chosen, 1):
        from app.models.schemas import Incident
        inc = Incident.model_validate_json(meta["incident_json"])
        if not inc.fix_diff or not inc.fix_diff.strip():
            print(f"  [{i}] {inc.commit_sha[:7]} skipped — empty fix_diff")
            continue
        query_diff = _invert_diff(inc.fix_diff)[:8000]

        hits = scar.search(repo=repo, query=query_diff, top_k=5, pr_files=inc.files_changed)
        top5_shas = [h[0].commit_sha for h in hits]
        rank = next((j for j, sha in enumerate(top5_shas, 1) if sha == inc.commit_sha), None)
        probes.append(RecallProbe(
            fix_sha=inc.commit_sha,
            fix_subject=inc.commit_message.splitlines()[0][:80] if inc.commit_message else "",
            files=inc.files_changed[:3],
            rank_in_top5=rank,
            top5_shas=[s[:7] for s in top5_shas],
        ))
        marker = "★" if rank else "·"
        rank_str = f"rank={rank}" if rank else "miss"
        print(f"  [{i}/{sample_n}] {marker} {inc.commit_sha[:7]} {rank_str}: {inc.commit_message.splitlines()[0][:70] if inc.commit_message else ''}")

    hits_in_top5 = sum(1 for p in probes if p.rank_in_top5 is not None)
    recall = hits_in_top5 / len(probes) if probes else 0.0
    return recall, probes


# ── false-positive rate on clean PRs ──────────────────────────────────────────


async def measure_fp_rate(repo: str, pr_urls: list[str]) -> tuple[float, list[dict]]:
    """For each clean PR URL, run the reviewer and count how many emit
    warnings. FP rate = (PRs with >=1 warning) / total.
    """
    fetcher = PRFetcher()
    scar = ScarIndex()
    nia = NiaClient()
    claude = anthropic.AsyncAnthropic()
    reviewer = Reviewer(scar_index=scar, nia=nia, anthropic_client=claude)

    rows: list[dict] = []
    for url in pr_urls:
        print(f"\n[fp-eval] Reviewing {url}")
        try:
            pr = fetcher.fetch(url)
        except Exception as exc:
            print(f"  fetch failed: {exc}")
            rows.append({"url": url, "error": str(exc), "warnings": -1})
            continue
        warnings = await reviewer.review(pr)
        rows.append({
            "url": url,
            "title": pr.title,
            "author": pr.author,
            "warnings": len(warnings),
            "explanations": [w.explanation[:200] for w in warnings],
        })
        print(f"  {len(warnings)} warning(s)")

    fp_count = sum(1 for r in rows if r.get("warnings", 0) > 0)
    fp_rate = fp_count / len(rows) if rows else 0.0
    return fp_rate, rows


# ── revert detection (single regression-harness invocation) ───────────────────


def run_revert_check(repo: str, fix_sha: str) -> int:
    """Returns 0 if the reviewer flagged the fix sha when given the inverted diff."""
    cmd = [
        sys.executable, "-m", "scripts.build_regression_test_pr",
        repo, fix_sha,
    ]
    print(f"[revert] running {' '.join(cmd)}")
    return subprocess.call(cmd)


# ── orchestration ─────────────────────────────────────────────────────────────


def cmd_classifier(args: argparse.Namespace) -> None:
    samples = sample_classifier_candidates(args.repo, n=args.n, max_walk=args.max_walk, seed=args.seed)
    out = Path(args.out)
    write_classifier_samples(samples, out)
    accepted = sum(1 for s in samples if s.llm_is_bug_fix)
    print(
        f"\n[classifier-eval] LLM accepted {accepted}/{len(samples)} as bug fixes."
        f" Hand-grade {out} to compute precision: precision = (correct LLM accepts) / (total LLM accepts)."
    )


def cmd_recall(args: argparse.Namespace) -> None:
    recall, probes = measure_top5_recall(args.repo, n=args.n, seed=args.seed)
    out = Path(args.out)
    out.write_text(json.dumps([asdict(p) for p in probes], indent=2), encoding="utf-8")
    hits = sum(1 for p in probes if p.rank_in_top5 is not None)
    print(f"\n[recall-eval] Top-5 recall: {hits}/{len(probes)} = {recall:.1%}")
    print(f"[recall-eval] Wrote per-probe details to {out}")


def cmd_fp(args: argparse.Namespace) -> None:
    pr_urls = [u.strip() for u in Path(args.pr_urls).read_text().splitlines() if u.strip() and not u.startswith("#")]
    rate, rows = asyncio.run(measure_fp_rate(args.repo, pr_urls))
    out = Path(args.out)
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    fp_count = sum(1 for r in rows if r.get("warnings", 0) > 0)
    print(f"\n[fp-eval] FP rate: {fp_count}/{len(rows)} = {rate:.1%}")
    print(f"[fp-eval] Wrote per-PR details to {out}")


def cmd_revert(args: argparse.Namespace) -> None:
    code = run_revert_check(args.repo, args.fix_sha)
    sys.exit(code)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("classifier")
    p.add_argument("--repo", required=True)
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--max-walk", type=int, default=2000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="phase_c_classifier_samples.json")
    p.set_defaults(func=cmd_classifier)

    p = sub.add_parser("recall")
    p.add_argument("--repo", required=True)
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="phase_c_recall_probes.json")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("fp")
    p.add_argument("--repo", required=True)
    p.add_argument("--pr-urls", required=True, help="Path to text file with one PR URL per line")
    p.add_argument("--out", default="phase_c_fp_rows.json")
    p.set_defaults(func=cmd_fp)

    p = sub.add_parser("revert")
    p.add_argument("--repo", required=True)
    p.add_argument("--fix-sha", required=True)
    p.set_defaults(func=cmd_revert)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
