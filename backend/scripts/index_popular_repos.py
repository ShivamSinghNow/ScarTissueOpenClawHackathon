import time
import logging
from app.services.git_miner import GitMiner
from app.services.scar_index import ScarIndex

REPOS = [
    ("tiangolo/fastapi", 3000),
    ("openai/openai-python", 2000),
    ("anthropics/anthropic-sdk-python", 1500),
    ("langchain-ai/langchainjs", 3000),
    ("vercel/next.js", 3000),
]


def main():
    logging.basicConfig(level=logging.INFO)
    miner = GitMiner()
    scar_index = ScarIndex()

    results = []
    overall_start = time.time()

    for repo, max_commits in REPOS:
        try:
            print(f"\n[indexer] Starting {repo} with max_commits={max_commits}")
            start = time.time()
            incidents = miner.mine(repo, max_commits)
            count = scar_index.index_incidents(repo, incidents)
            duration = time.time() - start
            print(f"[indexer] Done: {repo} ({count} incidents, {duration:.1f}s)")
            results.append((repo, count, duration, None))
        except Exception as e:
            duration = time.time() - start if "start" in locals() else 0.0
            print(f"[indexer] FAILED: {repo} - {e}")
            results.append((repo, 0, duration, str(e)))
            continue

    total_duration = time.time() - overall_start
    total_incidents = sum(r[1] for r in results)
    succeeded = [r for r in results if r[3] is None]
    failed = [r for r in results if r[3] is not None]

    print("\n" + "=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)
    for repo, count, duration, err in results:
        status = "OK" if err is None else f"FAILED ({err})"
        print(f"  {repo}: {count} incidents, {duration:.1f}s — {status}")
    print("-" * 60)
    print(f"Repos succeeded: {len(succeeded)}/{len(REPOS)}")
    print(f"Repos failed:    {len(failed)}")
    print(f"Total incidents: {total_incidents}")
    print(f"Total wall time: {total_duration:.1f}s ({total_duration / 60:.1f} min)")


if __name__ == "__main__":
    main()
