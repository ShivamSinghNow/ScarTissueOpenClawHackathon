"""Seed ChromaDB with incidents reconstructed from the curated demo kit.

Avoids cloning langchain (~minutes) by reading metadata.json + attack patch
from each demo_kit_langchain_full/attacks/<sha>/ folder and indexing the
resulting Incident objects into ChromaDB under the langchain-ai/langchain
collection. Idempotent — re-running just upserts.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

import os
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")

from app.models.schemas import Incident
from app.services.scar_index import ScarIndex

REPO = "langchain-ai/langchain"
ATTACKS_DIR = Path("demo_kit_langchain_full/attacks")


def load_incidents() -> list[Incident]:
    incidents: list[Incident] = []
    for attack_dir in sorted(ATTACKS_DIR.iterdir()):
        if not attack_dir.is_dir():
            continue
        meta = json.loads((attack_dir / "metadata.json").read_text())

        # attack.patch.clean is the inverted fix — use it as a proxy for the
        # fix diff text. ChromaDB embedding cares about token semantics, not
        # diff direction.
        diff_path = attack_dir / "attack.patch.clean"
        if not diff_path.exists():
            diff_path = attack_dir / "attack.patch"
        fix_diff = diff_path.read_text(encoding="utf-8", errors="replace")

        sha = meta["original_incident_sha"]
        incidents.append(
            Incident(
                commit_sha=sha,
                commit_message=meta["commit_message"],
                commit_date=datetime.now(tz=timezone.utc),
                author="langchain-team",
                files_changed=[meta["target_file"]],
                functions_changed=[],
                fix_diff=fix_diff,
                buggy_parent_sha="0" * 40,
                issue_refs=[],
                symptom_summary=None,
            )
        )
    return incidents


def main() -> int:
    incidents = load_incidents()
    print(f"Loaded {len(incidents)} incidents from {ATTACKS_DIR}")
    for inc in incidents:
        print(f"  {inc.commit_sha[:12]}  {inc.commit_message.splitlines()[0][:80]}")

    idx = ScarIndex(persist_dir="./chroma_db")
    written = idx.index_incidents(REPO, incidents)
    stats = idx.collection_stats(REPO)
    print(f"\nIndexed {written} incidents into ChromaDB.")
    print(f"Collection stats: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
