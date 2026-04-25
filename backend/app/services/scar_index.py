from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Optional

import chromadb
from sentence_transformers import SentenceTransformer

from app.models.schemas import Incident

_MODEL_NAME = "all-MiniLM-L6-v2"
_MAX_QUERY_CHARS = 8_000

# Re-rank widening: pull this many candidates from cosine, then re-score with
# path-overlap before returning the requested top_k.
_RERANK_MULTIPLIER = 4

# Path-overlap re-rank weights (added on top of cosine similarity in [0, 1]).
_PATH_BOOST_EXACT = 0.30      # historical commit touched the exact PR file
_PATH_BOOST_DIRECTORY = 0.15  # same directory
_PATH_BOOST_BASENAME = 0.10   # same filename in a different directory

# Drop hits whose cosine similarity is below this floor before returning. The
# default is conservative — Chroma cosine similarity is in [0, 1] and a 0.35
# match on a sentence-transformer model is usually noise. Override via
# SCARTISSUE_MIN_SIMILARITY env var.
_DEFAULT_MIN_SIMILARITY = float(os.environ.get("SCARTISSUE_MIN_SIMILARITY", "0.35"))


def _build_embedding_text(incident: Incident) -> str:
    msg = incident.commit_message[:500]
    files = ", ".join(incident.files_changed)
    diff = incident.fix_diff[:1500]
    parts = [
        f"Commit message: {msg}",
        f"Files changed: {files}",
    ]
    if incident.symptom_summary:
        parts.append(f"Symptom: {incident.symptom_summary}")
    parts.append(f"Fix diff: {diff}")
    return "\n".join(parts)


def _collection_name(repo: str) -> str:
    return repo.replace("/", "_").replace("-", "_").lower()


def _path_overlap_boost(historical_files: list[str], pr_files: list[str]) -> float:
    """Return the largest path-overlap boost between any pr_file and any historical_file."""
    if not historical_files or not pr_files:
        return 0.0

    pr_paths = [PurePosixPath(p) for p in pr_files]
    hist_paths = [PurePosixPath(p) for p in historical_files]

    best = 0.0
    for pr in pr_paths:
        for hist in hist_paths:
            if hist == pr:
                return _PATH_BOOST_EXACT
            if hist.parent == pr.parent and hist.parent.parts:
                best = max(best, _PATH_BOOST_DIRECTORY)
            if hist.name == pr.name and hist.name:
                best = max(best, _PATH_BOOST_BASENAME)
    return best


def _flat_metadata(incident: Incident, repo: str) -> dict:
    """Per-record metadata that Chroma can filter on without unpacking the JSON blob."""
    return {
        "repo": repo,
        "commit_sha": incident.commit_sha,
        "files": " | ".join(incident.files_changed[:20]),
        "author": incident.author,
        "year": incident.commit_date.year,
        "commit_date_iso": incident.commit_date.isoformat(),
        "incident_json": incident.model_dump_json(),
    }


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
            metadatas = [_flat_metadata(inc, repo) for inc in batch]
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
            mutable["repo"] = repo  # canonical original name, for lossless decode
            col.modify(metadata=mutable)
        except Exception as exc:
            print(f"[scar_index] Warning: could not update collection metadata: {exc}")

        return total

    def search(
        self,
        repo: str,
        query: str,
        top_k: int = 5,
        pr_files: list[str] | None = None,
        min_similarity: float | None = None,
    ) -> list[tuple[Incident, float]]:
        """Returns (incident, cosine_similarity) sorted by relevance.

        When pr_files is given, results are re-ranked so historical commits
        touching paths that overlap with the PR's files float above otherwise
        equally-similar generic matches.

        Hits below min_similarity are dropped. The path-overlap boost is used
        only for ranking; the threshold applies to the raw cosine score so
        callers can compare against an absolute number.
        """
        floor = _DEFAULT_MIN_SIMILARITY if min_similarity is None else min_similarity
        try:
            col = self._client.get_collection(name=_collection_name(repo))
        except Exception:
            return []

        count = col.count()
        if count == 0:
            return []

        if len(query) > _MAX_QUERY_CHARS:
            query = query[:_MAX_QUERY_CHARS]

        # Pull a wider candidate set so re-ranking has room to work.
        widened_k = min(top_k * _RERANK_MULTIPLIER, count) if pr_files else min(top_k, count)

        embedding = self._model.encode([query], show_progress_bar=False).tolist()
        results = col.query(
            query_embeddings=embedding,
            n_results=widened_k,
            include=["metadatas", "distances"],
        )

        if not results["ids"] or not results["ids"][0]:
            return []

        scored: list[tuple[Incident, float, float]] = []  # (incident, cosine_sim, blended)
        for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
            try:
                incident = Incident.model_validate_json(meta["incident_json"])
                # chromadb cosine space: distance = 1 - cosine_similarity
                similarity = 1.0 - dist
                if similarity < floor:
                    continue
                if pr_files:
                    boost = _path_overlap_boost(incident.files_changed, pr_files)
                    blended = similarity + boost
                else:
                    blended = similarity
                scored.append((incident, similarity, blended))
            except Exception:
                continue

        scored.sort(key=lambda t: t[2], reverse=True)
        # Return cosine similarity (not blended) so callers can compare to
        # absolute thresholds without having to subtract the boost.
        return [(inc, sim) for inc, sim, _blended in scored[:top_k]]

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
        """Returns {'name': str, 'count': int, 'last_indexed': iso_string | None, 'repo': str}"""
        name = _collection_name(repo)
        try:
            col = self._client.get_collection(name=name)
            count = col.count()
            metadata = col.metadata or {}
            last_indexed = metadata.get("last_indexed")
            canonical_repo = metadata.get("repo") or repo
            return {
                "name": name,
                "count": count,
                "last_indexed": last_indexed,
                "repo": canonical_repo,
            }
        except Exception:
            return {"name": name, "count": 0, "last_indexed": None, "repo": repo}

    def list_repos(self) -> list[dict]:
        """List every indexed collection with its canonical repo name and stats.

        Reads the canonical 'owner/name' from the collection metadata (set at
        index time) so we don't have to guess at the slash position from the
        underscored collection name.
        """
        out: list[dict] = []
        for col in self._client.list_collections():
            metadata = col.metadata or {}
            repo = metadata.get("repo")
            if not repo:
                # Fall back to reading one record's metadata
                try:
                    sample = col.get(limit=1, include=["metadatas"])
                    if sample["metadatas"]:
                        repo = sample["metadatas"][0].get("repo")
                except Exception:
                    repo = None
            if not repo:
                # Last resort: collection name verbatim (lossy but better than guessing)
                repo = col.name
            out.append({
                "repo": repo,
                "incidents": col.count(),
                "last_indexed": metadata.get("last_indexed"),
            })
        out.sort(key=lambda r: r["repo"])
        return out


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
