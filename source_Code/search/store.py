"""Persistent, incremental index store.

Layout (one shard per bid, so adding a bid never re-embeds the others):

    index_store/bids/<bid_id>/chunks.jsonl     chunk text + metadata
    index_store/bids/<bid_id>/embeddings.npy   float32 [n_chunks, dim] (L2-normalised)
    index_store/bids/<bid_id>/manifest.json    file hashes, model, chunking version, ingestion report

At query time shards are concatenated into one FAISS inner-product index
(exact search; at RFP scale - thousands of chunks - exact search is fast and
avoids recall loss from approximate indexes).
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ingestion.models import Chunk

logger = logging.getLogger("rfp.search.store")

# Bump whenever parsing/chunking logic changes in a way that should trigger re-indexing.
CHUNKING_VERSION = "2026.11.4"


class IndexStore:
    def __init__(self, root: Path):
        self.root = Path(root) / "bids"
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, bid_id: str) -> Path:
        return self.root / bid_id

    def list_bids(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if (p / "manifest.json").exists())

    def manifest(self, bid_id: str) -> dict | None:
        f = self._dir(bid_id) / "manifest.json"
        if not f.exists():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None

    def is_up_to_date(self, bid_id: str, file_hashes: dict[str, str], embedding_model: str) -> bool:
        m = self.manifest(bid_id)
        return bool(
            m
            and m.get("file_hashes") == file_hashes
            and m.get("embedding_model") == embedding_model
            and m.get("chunking_version") == CHUNKING_VERSION
        )

    def save(self, bid_id: str, chunks: list[Chunk], embeddings: np.ndarray, manifest: dict) -> None:
        target = self._dir(bid_id)
        tmp = self.root / f".{bid_id}.tmp"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        with (tmp / "chunks.jsonl").open("w", encoding="utf-8") as fh:
            for c in chunks:
                fh.write(c.model_dump_json() + "\n")
        np.save(tmp / "embeddings.npy", embeddings.astype(np.float32))
        manifest = {
            **manifest,
            "bid_id": bid_id,
            "chunk_count": len(chunks),
            "chunking_version": CHUNKING_VERSION,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        tmp.rename(target)
        logger.info("Saved shard %s (%d chunks)", bid_id, len(chunks))

    def delete(self, bid_id: str) -> bool:
        d = self._dir(bid_id)
        if d.exists():
            shutil.rmtree(d)
            return True
        return False

    def load_bid(self, bid_id: str) -> tuple[list[Chunk], np.ndarray]:
        d = self._dir(bid_id)
        chunks = [Chunk.model_validate_json(line) for line in (d / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        emb = np.load(d / "embeddings.npy")
        if len(chunks) != len(emb):
            raise ValueError(f"shard {bid_id} is corrupt: {len(chunks)} chunks vs {len(emb)} vectors")
        return chunks, emb

    def load_all(self, embedding_model: str | None = None) -> tuple[list[Chunk], np.ndarray, dict[str, dict]]:
        all_chunks: list[Chunk] = []
        mats: list[np.ndarray] = []
        manifests: dict[str, dict] = {}
        for bid in self.list_bids():
            m = self.manifest(bid) or {}
            if embedding_model and m.get("embedding_model") != embedding_model:
                logger.warning("Shard %s was built with %s (current: %s) - re-index it", bid,
                               m.get("embedding_model"), embedding_model)
                continue
            try:
                chunks, emb = self.load_bid(bid)
            except Exception as exc:
                logger.error("Skipping shard %s: %s", bid, exc)
                continue
            all_chunks.extend(chunks)
            mats.append(emb)
            manifests[bid] = m
        emb = np.vstack(mats) if mats else np.zeros((0, 1), dtype=np.float32)
        return all_chunks, emb, manifests
