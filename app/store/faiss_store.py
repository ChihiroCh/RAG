"""FAISS 索引：IndexFlatIP + L2 归一化，chunks.jsonl 与向量行号严格对齐。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import faiss
import numpy as np

from app.config import Settings, settings

logger = logging.getLogger(__name__)


class FaissStore:
    def __init__(
        self,
        index_dir: str | Path | None = None,
        dim: int | None = None,
        cfg: Settings | None = None,
    ) -> None:
        self.cfg = cfg or settings
        self.dir = Path(index_dir) if index_dir else self.cfg.index_dir
        self.dim = int(dim or self.cfg.embed_dim)
        self.index: faiss.Index | None = None
        self.chunks: list[dict[str, Any]] = []
        self.manifest: dict[str, Any] = {}
        self._vectors: np.ndarray | None = None

    # ------------------------------------------------------------------ 路径

    @property
    def index_path(self) -> Path:
        return self.dir / "index.faiss"

    @property
    def chunks_path(self) -> Path:
        return self.dir / "chunks.jsonl"

    @property
    def vectors_path(self) -> Path:
        return self.dir / "vectors.npy"

    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    # ------------------------------------------------------------------ 读写

    def exists(self) -> bool:
        return self.index_path.exists() and self.chunks_path.exists()

    def load(self) -> bool:
        if not self.exists():
            return False
        try:
            self.index = faiss.read_index(str(self.index_path))
            self.chunks = []
            with self.chunks_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        self.chunks.append(json.loads(line))
            if self.index.ntotal != len(self.chunks):
                logger.error(
                    "索引与元数据不一致（向量 %d / 分块 %d），请重建索引",
                    self.index.ntotal,
                    len(self.chunks),
                )
                self.index, self.chunks = None, []
                return False
            self.dim = int(self.index.d)
            if self.manifest_path.exists():
                self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            self._vectors = None
            logger.info("索引加载完成：%d 个分块，维度 %d", len(self.chunks), self.dim)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("索引加载失败：%s", exc)
            self.index, self.chunks = None, []
            return False

    def save(self, manifest: dict[str, Any] | None = None) -> None:
        if self.index is None:
            raise RuntimeError("索引为空，无法保存")
        self.dir.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(self.index_path))
        np.save(self.vectors_path, self.vectors)

        tmp_chunks = self.chunks_path.with_suffix(".jsonl.tmp")
        with tmp_chunks.open("w", encoding="utf-8") as handle:
            for chunk in self.chunks:
                handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
        tmp_chunks.replace(self.chunks_path)

        if manifest is not None:
            self.manifest = manifest
        self.manifest.setdefault("built_at", datetime.now().isoformat(timespec="seconds"))
        self.manifest["total_chunks"] = len(self.chunks)
        self.manifest["embed_dim"] = self.dim
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ------------------------------------------------------------------ 构建

    def rebuild(self, chunks: list[dict[str, Any]], vectors: np.ndarray) -> None:
        if len(chunks) != len(vectors):
            raise ValueError(f"分块数 {len(chunks)} 与向量数 {len(vectors)} 不一致")
        if not len(chunks):
            raise ValueError("没有可索引的分块")
        matrix = np.ascontiguousarray(np.asarray(vectors, dtype="float32"))
        faiss.normalize_L2(matrix)
        self.dim = int(matrix.shape[1])
        self.index = faiss.IndexFlatIP(self.dim)
        self.index.add(matrix)
        self.chunks = chunks
        self._vectors = matrix
        logger.info("索引重建完成：%d 个分块，维度 %d", len(chunks), self.dim)

    def append(self, chunks: list[dict[str, Any]], vectors: np.ndarray) -> None:
        if not len(chunks):
            return
        if self.index is None:
            self.rebuild(chunks, vectors)
            return
        if len(chunks) != len(vectors):
            raise ValueError(f"分块数 {len(chunks)} 与向量数 {len(vectors)} 不一致")
        matrix = np.ascontiguousarray(np.asarray(vectors, dtype="float32"))
        faiss.normalize_L2(matrix)
        if matrix.shape[1] != self.dim:
            raise ValueError(f"向量维度 {matrix.shape[1]} 与索引维度 {self.dim} 不一致，需要重建索引")
        self.index.add(matrix)
        self.chunks.extend(chunks)
        self._vectors = None
        logger.info("索引追加 %d 个分块（总计 %d）", len(chunks), len(self.chunks))

    @property
    def vectors(self) -> np.ndarray:
        if self._vectors is not None:
            return self._vectors
        if self.index is None:
            return np.zeros((0, self.dim), dtype="float32")
        matrix = self.index.reconstruct_n(0, self.index.ntotal)
        self._vectors = np.asarray(matrix, dtype="float32")
        return self._vectors

    # ------------------------------------------------------------------ 检索

    def search(
        self,
        vector: np.ndarray | list[float],
        top_k: int = 20,
        doc_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if self.index is None or self.index.ntotal == 0:
            return []
        query = np.ascontiguousarray(np.asarray([vector], dtype="float32"))
        faiss.normalize_L2(query)
        allow = {str(item) for item in doc_ids} if doc_ids else None
        # 带文档过滤时必须取全部候选再筛，否则目标文档的块可能排不进全局 top-N 导致漏检
        fetch = top_k if not allow else min(self.index.ntotal, 20000)

        scores, indices = self.index.search(query, fetch)
        results: list[dict[str, Any]] = []
        for score, row in zip(scores[0], indices[0]):
            if row < 0 or row >= len(self.chunks):
                continue
            chunk = self.chunks[int(row)]
            if allow and chunk.get("doc_id") not in allow:
                continue
            results.append({"chunk": chunk, "score": float(score), "row": int(row)})
            if len(results) >= top_k:
                break
        return results

    # ------------------------------------------------------------------ 统计

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        docs: dict[str, dict[str, Any]] = {}
        for chunk in self.chunks:
            by_type[chunk.get("type", "text")] = by_type.get(chunk.get("type", "text"), 0) + 1
            doc_id = chunk.get("doc_id", "")
            entry = docs.setdefault(
                doc_id,
                {
                    "doc_id": doc_id,
                    "file_name": chunk.get("file_name", ""),
                    "file_type": chunk.get("file_type", ""),
                    "chunks": 0,
                    "pages": 0,
                    "types": set(),
                },
            )
            entry["chunks"] += 1
            entry["pages"] = max(entry["pages"], int(chunk.get("page") or 0))
            entry["types"].add(chunk.get("type", "text"))

        document_list = []
        for entry in docs.values():
            entry["types"] = sorted(entry["types"])
            document_list.append(entry)
        document_list.sort(key=lambda item: item["doc_id"])

        return {
            "chunks": len(self.chunks),
            "documents": len(docs),
            "by_type": by_type,
            "dim": self.dim,
            "documents_detail": document_list,
            "manifest": {key: value for key, value in self.manifest.items() if key != "docs"},
        }
