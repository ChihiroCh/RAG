"""入库流水线：扫描源文件 -> 解析（带缓存）-> 分块 -> 向量化 -> 写入 FAISS。

doc_id 唯一生成入口 build_doc_id()：取文件名为标识（001.pdf -> 001），
全项目其余位置均复用该值或直接读取分块元数据。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from app.clients.llm import get_llm_client
from app.config import Settings, settings
from app.ingest.chunker import CHUNKER_VERSION, build_chunks, chunk_document
from app.ingest.image_parser import parse_image
from app.ingest.models import ParsedDoc
from app.ingest.pdf_parser import parse_pdf
from app.store.faiss_store import FaissStore

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
# 解析产物版本：提示词/解析逻辑变更后必须递增，否则旧缓存会被继续命中
PARSER_VERSION = "v4"

ProgressFn = Callable[[dict], None]
VisionFn = Callable[[bytes], str]


# --------------------------------------------------------------------- 工具


def build_doc_id(file_path: str | Path) -> str:
    """来源标识：文件名主干（不含扩展名）。"""
    return Path(file_path).stem


def file_sha1(path: Path, block_size: int = 1 << 20) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while True:
            block = handle.read(block_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def collect_files(files_dir: str | Path) -> list[Path]:
    root = Path(files_dir)
    if not root.exists():
        raise RuntimeError(f"源数据目录不存在：{root}")
    files = [path for path in sorted(root.iterdir()) if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES]
    return files


def _vision_fn() -> VisionFn:
    from app.clients.llm import PAGE_STRUCT_PROMPT
    client = get_llm_client()
    return lambda data: client.vision_describe(data, prompt=PAGE_STRUCT_PROMPT, max_tokens=8192)


# --------------------------------------------------------------------- 解析缓存


def _cache_file(cfg: Settings, doc_id: str, sha1: str) -> Path:
    return cfg.parsed_dir / f"{doc_id}_{sha1[:12]}_{PARSER_VERSION}.json"


def parse_file(path: Path, doc_id: str, vision: VisionFn | None = None) -> ParsedDoc:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path, doc_id, vision=vision)
    if suffix in IMAGE_SUFFIXES:
        return parse_image(path, doc_id, vision=vision)
    raise ValueError(f"不支持的文件类型：{path.name}")


def load_or_parse(path: Path, doc_id: str, sha1: str, cfg: Settings, vision: VisionFn | None = None) -> ParsedDoc:
    cache_path = _cache_file(cfg, doc_id, sha1)
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if not data.get("blocks") or data.get("meta", {}).get("parse_errors"):
                raise ValueError("空解析缓存需要重新解析")
            logger.debug("命中解析缓存：%s", cache_path.name)
            return ParsedDoc.from_dict(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析缓存损坏，重新解析 %s：%s", cache_path.name, exc)

    doc = parse_file(path, doc_id, vision=vision)
    if not doc.blocks or doc.meta.get("parse_errors"):
        raise RuntimeError(f"{path.name} 解析没有产生内容，拒绝缓存或写入索引")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(doc.to_dict(), ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入解析缓存失败：%s", exc)
    return doc


# --------------------------------------------------------------------- 向量化


def _embed_with_progress(
    texts: list[str],
    cfg: Settings,
    report: ProgressFn | None = None,
) -> np.ndarray:
    client = get_llm_client()
    batch = max(1, cfg.embed_batch_size)
    total = len(texts)
    vectors: list[np.ndarray] = []
    for start in range(0, total, batch):
        part = texts[start : start + batch]
        vectors.append(client.embed_texts(part, batch_size=batch))
        if report:
            report({"stage": "embed", "done": min(start + batch, total), "total": total})
    if not vectors:
        return np.zeros((0, cfg.embed_dim), dtype="float32")
    return np.vstack(vectors)


# --------------------------------------------------------------------- 主流程


def ingest(
    rebuild: bool = False,
    cfg: Settings | None = None,
    progress: ProgressFn | None = None,
    vision: VisionFn | None = None,
) -> dict:
    """构建/更新索引，返回统计信息。"""
    cfg = cfg or settings
    started = time.time()

    def report(event: dict) -> None:
        if progress:
            try:
                progress(event)
            except Exception:  # noqa: BLE001 - 进度回调异常不影响入库
                logger.debug("进度回调异常", exc_info=True)

    cfg.require_api_key()

    files = collect_files(cfg.files_path)
    if not files:
        raise RuntimeError(f"{cfg.files_path} 下没有可解析的文件（支持 PDF 与图片）")

    fingerprints: dict[str, dict] = {}
    for path in files:
        doc_id = build_doc_id(path)
        if doc_id in fingerprints:
            raise RuntimeError(f"文件名重复（标识冲突）：{path.name}")
        fingerprints[doc_id] = {
            "file_name": path.name,
            "file_type": "pdf" if path.suffix.lower() == ".pdf" else "image",
            "size": path.stat().st_size,
            "mtime": path.stat().st_mtime,
            "sha1": file_sha1(path),
            "path": path,
        }

    report({"stage": "prepare", "total": len(files), "files": [item["file_name"] for item in fingerprints.values()]})

    store = FaissStore(cfg=cfg)
    loaded = False if rebuild else store.load()
    manifest = store.manifest if loaded else {}
    indexed_docs: dict[str, dict] = dict(manifest.get("docs", {})) if loaded else {}

    model_ok = (
        loaded
        and manifest.get("embed_model") == cfg.embed_model
        and int(manifest.get("embed_dim", 0)) == int(cfg.embed_dim)
        and manifest.get("chunker_version") == CHUNKER_VERSION
        and manifest.get("parser_version") == PARSER_VERSION
        and int(manifest.get("chunk_size", 0)) == int(cfg.chunk_size)
        and int(manifest.get("chunk_overlap", -1)) == int(cfg.chunk_overlap)
    )

    current_ids = set(fingerprints)
    known_ids = set(indexed_docs)
    removed = known_ids - current_ids
    modified = {
        doc_id
        for doc_id in known_ids & current_ids
        if indexed_docs.get(doc_id, {}).get("sha1") != fingerprints[doc_id]["sha1"]
    }
    new_ids = current_ids - known_ids

    full_rebuild = rebuild or not loaded or not model_ok or bool(removed) or bool(modified)
    target_ids = set(fingerprints) if full_rebuild else new_ids

    logger.info(
        "入库模式：%s；目标文件 %d 个（新增 %d，修改 %d，删除 %d）",
        "全量重建" if full_rebuild else "增量",
        len(target_ids),
        len(new_ids),
        len(modified),
        len(removed),
    )

    if not target_ids and loaded:
        stats = store.stats()
        report({"stage": "done", "stats": stats, "mode": "skip"})
        return {
            "mode": "skip",
            "message": "所有文件均未变化，已跳过重建",
            "processed_docs": 0,
            "new_chunks": 0,
            "chunks": stats["chunks"],
            "documents": stats["documents"],
            "elapsed": round(time.time() - started, 2),
            "errors": [],
        }

    # ---------- 解析（并发 + 缓存） ----------
    parsed: dict[str, ParsedDoc] = {}
    vision_fn = vision or _vision_fn()
    ordered_ids = sorted(target_ids)
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, cfg.vlm_concurrency)) as pool:
        futures = {}
        for doc_id in ordered_ids:
            info = fingerprints[doc_id]
            futures[
                pool.submit(load_or_parse, info["path"], doc_id, info["sha1"], cfg, vision_fn)
            ] = doc_id
        for future in as_completed(futures):
            doc_id = futures[future]
            info = fingerprints[doc_id]
            try:
                parsed[doc_id] = future.result()
                parsed[doc_id].meta.setdefault("sha1", info["sha1"])
            except Exception as exc:  # noqa: BLE001
                logger.error("解析失败 %s：%s", info["file_name"], exc)
                report({"stage": "parse_error", "file": info["file_name"], "message": str(exc)[:300]})
            done += 1
            report(
                {
                    "stage": "parse",
                    "done": done,
                    "total": len(ordered_ids),
                    "file": info["file_name"],
                    "doc_id": doc_id,
                }
            )

    if not parsed:
        raise RuntimeError("没有任何文件解析成功，请检查文件是否为有效 PDF/图片，或网络与 API Key 是否正常")
    if len(parsed) != len(target_ids):
        failed = sorted(target_ids - set(parsed))
        raise RuntimeError(f"解析未完成，保留当前索引，不写入不完整索引；失败文件 {len(failed)} 个：{','.join(failed)}")

    # ---------- 分块 ----------
    report({"stage": "chunk", "done": 0, "total": len(parsed)})
    if full_rebuild:
        chunks = build_chunks(list(parsed.values()), cfg)
        doc_chunks = {doc_id: sum(1 for chunk in chunks if chunk["doc_id"] == doc_id) for doc_id in parsed}
    else:
        chunks = []
        doc_chunks = {}
        for doc_id, doc in parsed.items():
            doc_chunks_list = chunk_document(doc, cfg)
            doc_chunks[doc_id] = len(doc_chunks_list)
            chunks.extend(doc_chunks_list)
    report({"stage": "chunk", "done": len(parsed), "total": len(parsed), "chunks": len(chunks)})

    if not chunks:
        raise RuntimeError("解析结果为空，未能生成任何分块")

    # ---------- 向量化 ----------
    texts = [chunk["text"] for chunk in chunks]
    vectors = _embed_with_progress(texts, cfg, report)

    # ---------- 写索引 ----------
    report({"stage": "save"})
    if full_rebuild:
        store.rebuild(chunks, vectors)
        docs_manifest = {}
    else:
        store.append(chunks, vectors)
        docs_manifest = dict(indexed_docs)

    for doc_id in parsed:
        info = fingerprints[doc_id]
        doc = parsed[doc_id]
        docs_manifest[doc_id] = {
            "doc_id": doc_id,
            "file_name": info["file_name"],
            "file_type": info["file_type"],
            "sha1": info["sha1"],
            "size": info["size"],
            "mtime": info["mtime"],
            "pages": doc.pages,
            "chunks": doc_chunks.get(doc_id, 0),
            "scan_pages": doc.meta.get("scan_pages", []),
            "table_count": doc.meta.get("table_count", 0),
        }

    manifest = {
        "complete": True,
        "version": 1,
        "chunker_version": CHUNKER_VERSION,
        "parser_version": PARSER_VERSION,
        "embed_model": cfg.embed_model,
        "embed_dim": cfg.embed_dim,
        "chunk_size": cfg.chunk_size,
        "chunk_overlap": cfg.chunk_overlap,
        "llm_model": cfg.llm_model,
        "vlm_model": cfg.vlm_model,
        "rerank_model": cfg.rerank_model,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "docs": docs_manifest,
    }
    store.save(manifest)

    elapsed = round(time.time() - started, 2)
    stats = store.stats()
    report({"stage": "done", "stats": stats, "mode": "full" if full_rebuild else "incremental"})
    logger.info(
        "入库完成（%s）：%d 个文件，%d 个分块，耗时 %.1fs",
        "全量" if full_rebuild else "增量",
        len(parsed),
        len(chunks),
        elapsed,
    )
    return {
        "mode": "full" if full_rebuild else "incremental",
        "processed_docs": len(parsed),
        "new_chunks": len(chunks),
        "chunks": stats["chunks"],
        "documents": stats["documents"],
        "elapsed": elapsed,
        "errors": [],
    }


def get_stats(cfg: Settings | None = None) -> dict:
    cfg = cfg or settings
    store = FaissStore(cfg=cfg)
    if not store.load():
        return {
            "chunks": 0,
            "documents": 0,
            "by_type": {},
            "dim": cfg.embed_dim,
            "documents_detail": [],
            "manifest": {},
        }
    return store.stats()


def iter_doc_ids(chunks: Iterable[dict]) -> list[str]:
    return sorted({chunk["doc_id"] for chunk in chunks})
