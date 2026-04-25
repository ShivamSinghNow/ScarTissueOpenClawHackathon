"""Re-index a repo with the current pipeline (extended classifier + voyage-code-3).

Usage (from backend/):
    python -m scripts.reindex <repo> [--max-commits N]

Drops any existing collection for the repo before re-indexing so a stale
embedding dimension or schema doesn't poison the new run.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.services.bugfix_classifier import BugFixClassifier, llm_classifier_enabled
from app.services.git_miner import GitMiner
from app.services.scar_index import ScarIndex


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", help="GitHub repo as owner/name")
    parser.add_argument("--max-commits", type=int, default=5000)
    parser.add_argument(
        "--no-llm-classifier",
        action="store_true",
        help="Skip the stage-2 LLM filter (fast iteration, lower precision)",
    )
    args = parser.parse_args()

    persist_dir = os.environ.get("CHROMA_PERSIST_DIR", "./chroma_db")
    print(f"[reindex] Repo: {args.repo}")
    print(f"[reindex] Persist dir: {persist_dir}")
    print(f"[reindex] Max commits: {args.max_commits}")
    print(f"[reindex] LLM classifier: {'ON' if not args.no_llm_classifier else 'OFF'}")
    print(f"[reindex] Voyage min interval: {os.environ.get('SCARTISSUE_VOYAGE_MIN_INTERVAL', '21')}s")
    print()

    overall_start = time.perf_counter()

    miner = GitMiner()
    classifier = None
    if not args.no_llm_classifier and llm_classifier_enabled():
        cache_dir = Path(persist_dir).parent / "classifier_cache"
        cache_path = cache_dir / f"{args.repo.replace('/', '_')}.json"
        classifier = BugFixClassifier(cache_path=cache_path)

    mine_start = time.perf_counter()
    incidents = miner.mine(args.repo, max_commits=args.max_commits, classifier=classifier)
    mine_seconds = time.perf_counter() - mine_start

    scar = ScarIndex(persist_dir=persist_dir)
    embed_start = time.perf_counter()
    indexed = scar.index_incidents(args.repo, incidents)
    embed_seconds = time.perf_counter() - embed_start

    overall_seconds = time.perf_counter() - overall_start

    print()
    print("=" * 65)
    print(f"[reindex] DONE — {args.repo}")
    print(f"[reindex]   incidents indexed : {indexed}")
    print(f"[reindex]   mining + classify : {mine_seconds:6.1f}s")
    print(f"[reindex]   voyage embedding  : {embed_seconds:6.1f}s")
    print(f"[reindex]   total wall clock  : {overall_seconds:6.1f}s")
    print(f"[reindex]   voyage tokens     : {scar.total_tokens:,}")
    print("=" * 65)


if __name__ == "__main__":
    main()
