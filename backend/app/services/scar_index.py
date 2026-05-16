from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from typing import Optional

import chromadb
from sentence_transformers import SentenceTransformer

from app.models.schemas import Incident

if TYPE_CHECKING:
    from app.services.gbrain_client import GBrainClient

_MODEL_NAME = "all-MiniLM-L6-v2"
_MAX_QUERY_CHARS = 8_000


def _build_embedding_text(incident: Incident) -> str:
    msg = incident.commit_message[:500]
    files = ", ".join(incident.files_changed)
    diff = incident.fix_diff[:1500]
    return f"Commit message: {msg}\nFiles changed: {files}\nFix diff: {diff}"


def _collection_name(repo: str) -> str:
    return repo.replace("/", "_").replace("-", "_").lower()


class ScarIndex:
    def __init__(self, persist_dir: str | None = None) -> None:
        self.persist_dir = persist_dir or os.environ.get("CHROMA_PERSIST_DIR", "./chroma_db")
        self._client = chromadb.PersistentClient(path=self.persist_dir)
        print(f"[scar_index] Loading embedding model {_MODEL_NAME}…")
        self._model = SentenceTransformer(_MODEL_NAME)
        print("[scar_index] Model loaded.")

    def index_incidents(
        self,
        repo: str,
        incidents: list[Incident],
        batch_size: int = 64,
    ) -> int:
        """Embeds and upserts incidents. Returns count indexed."""
        if not incidents:
            return 0

        col = self._client.get_or_create_collection(
            name=_collection_name(repo),
            metadata={"hnsw:space": "cosine"},
        )

        total = 0
        for i in range(0, len(incidents), batch_size):
            batch = incidents[i : i + batch_size]
            texts = [_build_embedding_text(inc) for inc in batch]
            embeddings = self._model.encode(texts, show_progress_bar=False).tolist()
            ids = [inc.commit_sha for inc in batch]
            metadatas = [{"incident_json": inc.model_dump_json()} for inc in batch]
            # document text is used for display only; real data lives in metadata
            documents = [inc.commit_message.splitlines()[0][:200] for inc in batch]

            col.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )
            total += len(batch)
            print(f"[scar_index] Indexed {total}/{len(incidents)} incidents…")

        try:
            # Strip immutable hnsw:* keys — chromadb rejects modify() if they're present
            mutable = {k: v for k, v in (col.metadata or {}).items() if not k.startswith("hnsw:")}
            mutable["last_indexed"] = datetime.now(tz=timezone.utc).isoformat()
            col.modify(metadata=mutable)
        except Exception:
            pass

        return total

    def search(
        self,
        repo: str,
        query: str,
        top_k: int = 5,
    ) -> list[tuple[Incident, float]]:
        """Returns (incident, similarity_score) sorted by relevance."""
        try:
            col = self._client.get_collection(name=_collection_name(repo))
        except Exception:
            return []

        count = col.count()
        if count == 0:
            return []

        if len(query) > _MAX_QUERY_CHARS:
            query = query[:_MAX_QUERY_CHARS]

        embedding = self._model.encode([query], show_progress_bar=False).tolist()
        results = col.query(
            query_embeddings=embedding,
            n_results=min(top_k, count),
            include=["metadatas", "distances"],
        )

        if not results["ids"] or not results["ids"][0]:
            return []

        out: list[tuple[Incident, float]] = []
        for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
            try:
                incident = Incident.model_validate_json(meta["incident_json"])
                # chromadb cosine space: distance = 1 - cosine_similarity
                similarity = 1.0 - dist
                out.append((incident, similarity))
            except Exception:
                continue

        return out

    async def search_with_gbrain(
        self,
        repo: str,
        query: str,
        top_k: int = 5,
        gbrain_client: 'GBrainClient | None' = None,
    ) -> list[dict]:
        results: list[dict] = []
        seen_source_commits: set[str] = set()

        if gbrain_client is not None:
            try:
                gbrain_hits = await gbrain_client.search(query, top_k=top_k)
            except Exception:
                gbrain_hits = []

            for pattern in gbrain_hits:
                if pattern.source_commit in seen_source_commits:
                    continue

                results.append(
                    {
                        'source_commit': pattern.source_commit,
                        'message': pattern.trigger_condition,
                        'score': pattern.confidence,
                        'learned_from': pattern.learned_from,
                        'source': 'gbrain',
                        'incident': None,
                        'bug_pattern': pattern,
                    }
                )
                seen_source_commits.add(pattern.source_commit)

        for incident, score in self.search(repo, query, top_k=top_k):
            if len(results) >= top_k:
                break
            if incident.commit_sha in seen_source_commits:
                continue

            results.append(
                {
                    'source_commit': incident.commit_sha,
                    'message': incident.commit_message.splitlines()[0][:200],
                    'score': score,
                    'learned_from': 'git_history',
                    'source': 'chromadb',
                    'incident': incident,
                    'bug_pattern': None,
                }
            )
            seen_source_commits.add(incident.commit_sha)
            if len(results) >= top_k:
                break

        results.sort(key=lambda r: r['score'], reverse=True)
        return results[:top_k]

    def get_by_sha(self, repo: str, commit_sha: str) -> Incident | None:
        try:
            col = self._client.get_collection(name=_collection_name(repo))
        except Exception:
            return None

        try:
            result = col.get(ids=[commit_sha], include=["metadatas"])
            if not result["ids"]:
                return None
            return Incident.model_validate_json(result["metadatas"][0]["incident_json"])
        except Exception:
            return None

    def collection_stats(self, repo: str) -> dict:
        """Returns {'name': str, 'count': int, 'last_indexed': iso_string | None}"""
        name = _collection_name(repo)
        try:
            col = self._client.get_collection(name=name)
            count = col.count()
            last_indexed = (col.metadata or {}).get("last_indexed")
            return {"name": name, "count": count, "last_indexed": last_indexed}
        except Exception:
            return {"name": name, "count": 0, "last_indexed": None}


if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Allow running directly: python app/services/scar_index.py from backend/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    from app.services.git_miner import GitMiner

    REPO = "langchain-ai/langchain"

    print(f"\n{'=' * 60}")
    print(f"Mining {REPO} (max_commits=500)…")
    print("=" * 60)
    miner = GitMiner()
    incidents = miner.mine(REPO, max_commits=500)
    print(f"\nFound {len(incidents)} incidents.\n")

    print(f"{'=' * 60}")
    print("Indexing incidents…")
    print("=" * 60)
    index = ScarIndex()
    count = index.index_incidents(REPO, incidents)
    stats = index.collection_stats(REPO)
    print(f"\nIndexed {count} incidents. Collection stats: {stats}\n")

    QUERIES = [
        "streaming response callback cleanup",
        "retry logic duplicate request",
        "async iterator not properly closed",
    ]

    for query in QUERIES:
        print(f"\n{'=' * 60}")
        print(f"Query: {query!r}")
        print("=" * 60)
        hits = index.search(REPO, query, top_k=3)
        if not hits:
            print("  (no results)")
            continue
        for rank, (inc, score) in enumerate(hits, 1):
            first_line = inc.commit_message.splitlines()[0][:80]
            print(f"  {rank}. sha={inc.commit_sha[:12]}  score={score:.4f}")
            print(f"     {first_line}")
