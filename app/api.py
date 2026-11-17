"""FastAPI 服务：聊天流式接口、索引状态、后台入库、原图与 PDF 页预览。"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.ingest.pdf_parser import render_page_jpeg
from app.ingest.pipeline import SUPPORTED_SUFFIXES, get_stats, ingest
from app.rag.qa import RAGChat

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="多模态 RAG 知识库", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

_chat = RAGChat()
_state_lock = threading.Lock()
_ingest_state: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "mode": None,
    "done": 0,
    "total": 0,
    "file": "",
    "chunks": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
}


class ChatRequest(BaseModel):
    question: str
    history: list[dict[str, Any]] | None = None
    doc_ids: list[str] | None = None
    answer_format: str | None = None  # number | json_array | json：结构化取值（可选）


class IngestRequest(BaseModel):
    rebuild: bool = False


# --------------------------------------------------------------------- 页面


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")


# --------------------------------------------------------------------- 状态


@app.get("/api/status")
def status() -> dict[str, Any]:
    stats = get_stats()
    with _state_lock:
        ingest_state = dict(_ingest_state)
    return {
        "ready": stats["chunks"] > 0,
        "chunks": stats["chunks"],
        "documents": stats["documents"],
        "by_type": stats["by_type"],
        "documents_detail": stats["documents_detail"],
        "manifest": stats["manifest"],
        "models": {
            "llm": settings.llm_model,
            "vlm": settings.vlm_model,
            "embed": settings.embed_model,
            "rerank": settings.rerank_model,
            "embed_dim": settings.embed_dim,
        },
        "api_key": settings.masked_key(),
        "has_api_key": settings.has_api_key,
        "files_dir": str(settings.files_path),
        "ingest": ingest_state,
    }


@app.get("/api/chunks")
def chunks(doc_id: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    store = _chat.get_store()
    items = [item for item in store.chunks if not doc_id or item.get("doc_id") == doc_id]
    return {"total": len(items), "items": items[:limit]}


# --------------------------------------------------------------------- 聊天


@app.post("/api/chat")
def chat(payload: ChatRequest) -> StreamingResponse:
    def event_stream() -> Iterator[str]:
        try:
            for event in _chat.ask_stream(
                payload.question,
                history=payload.history,
                doc_ids=payload.doc_ids,
                answer_format=payload.answer_format,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("聊天接口异常")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------- 入库


def _run_ingest(rebuild: bool) -> None:
    with _state_lock:
        _ingest_state.update(
            {
                "running": True,
                "stage": "prepare",
                "mode": "full" if rebuild else "incremental",
                "done": 0,
                "total": 0,
                "file": "",
                "chunks": 0,
                "message": "",
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
            }
        )

    def report(event: dict) -> None:
        with _state_lock:
            stage = event.get("stage")
            if stage:
                _ingest_state["stage"] = stage
            if "done" in event:
                _ingest_state["done"] = event["done"]
            if "total" in event:
                _ingest_state["total"] = event["total"]
            if "file" in event:
                _ingest_state["file"] = event["file"]
            if "chunks" in event:
                _ingest_state["chunks"] = event["chunks"]
            if "message" in event:
                _ingest_state["message"] = event["message"]

    try:
        result = ingest(rebuild=rebuild, progress=report)
        _chat.reset()
        with _state_lock:
            _ingest_state.update(
                {
                    "running": False,
                    "stage": "done",
                    "done": result.get("chunks", 0),
                    "total": result.get("chunks", 0),
                    "chunks": result.get("chunks", 0),
                    "message": f"完成：{result.get('processed_docs', 0)} 个文件，{result.get('new_chunks', 0)} 个新分块",
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "result": result,
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("入库失败")
        with _state_lock:
            _ingest_state.update(
                {
                    "running": False,
                    "stage": "error",
                    "message": str(exc)[:600],
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                }
            )


@app.post("/api/ingest")
def start_ingest(payload: IngestRequest) -> dict[str, Any]:
    with _state_lock:
        if _ingest_state["running"]:
            raise HTTPException(status_code=409, detail="已有入库任务在进行中")
        if not settings.has_api_key:
            raise HTTPException(status_code=400, detail="未配置 DASHSCOPE_API_KEY，请在 .env 中填写后重启服务")
        _ingest_state["running"] = True

    thread = threading.Thread(target=_run_ingest, args=(payload.rebuild,), daemon=True)
    thread.start()
    return {"started": True, "rebuild": payload.rebuild, "time": time.time()}


# --------------------------------------------------------------------- 文件预览


def _safe_files_path(name: str) -> Path:
    if not name or Path(name).name != name:
        raise HTTPException(status_code=404, detail="文件不存在")
    path = (settings.files_path / name).resolve()
    root = settings.files_path.resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(status_code=404, detail="不支持的文件类型")
    return path


@app.get("/api/files/{name}")
def get_file(name: str) -> FileResponse:
    return FileResponse(_safe_files_path(name))


@app.get("/api/page/{name}/{page}")
def get_pdf_page(name: str, page: int) -> FileResponse:
    path = _safe_files_path(name)
    if path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="仅支持 PDF 页面预览")
    cache_dir = settings.page_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{path.stem}_p{page}.jpg"
    if not cached.exists():
        try:
            data = render_page_jpeg(path, page)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"页面渲染失败：{exc}") from exc
        cached.write_bytes(data)
    return FileResponse(cached, media_type="image/jpeg")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "time": datetime.now().isoformat(timespec="seconds")}
