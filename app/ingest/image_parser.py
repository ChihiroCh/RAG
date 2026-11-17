"""图片解析：预处理后交 qwen-vl 输出整页结构化内容（表格 HTML + 正文）。"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Callable

from PIL import Image

from app.clients.llm import PAGE_STRUCT_PROMPT
from app.ingest.models import ParsedDoc
from app.ingest.table_norm import blocks_from_markdown

logger = logging.getLogger(__name__)

MAX_SIDE = 1800
JPEG_QUALITY = 92

VisionFn = Callable[[bytes], str]


def _default_vision(image_bytes: bytes) -> str:
    from app.clients.llm import get_llm_client

    return get_llm_client().vision_describe(image_bytes, prompt=PAGE_STRUCT_PROMPT)


def prepare_image_bytes(path: str | Path, max_side: int = MAX_SIDE) -> bytes:
    """统一转 RGB（透明通道铺白底）、等比压缩到 max_side 内、编码为 JPEG。"""
    with Image.open(path) as raw:
        raw.load()
        has_alpha = raw.mode in ("RGBA", "LA") or (raw.mode == "P" and "transparency" in raw.info)
        if has_alpha:
            rgba = raw.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, (255, 255, 255))
            canvas.paste(rgba, mask=rgba.split()[-1])
            image = canvas
        else:
            image = raw.convert("RGB")

        if max(image.size) > max_side:
            image.thumbnail((max_side, max_side), Image.LANCZOS)

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buffer.getvalue()


def parse_image(
    path: str | Path,
    doc_id: str,
    vision: VisionFn | None = None,
) -> ParsedDoc:
    """解析单张图片：VLM 输出拆成规范化表格块 + 正文块。"""
    path = Path(path)
    vision = vision or _default_vision

    with Image.open(path) as raw:
        width, height = raw.size
        image_format = raw.format or path.suffix.lstrip(".").upper()

    image_bytes = prepare_image_bytes(path)
    try:
        markdown = vision(image_bytes).strip()
    except Exception as exc:  # noqa: BLE001
        logger.error("图片 %s 视觉识别失败：%s", path.name, exc)
        markdown = ""

    blocks = blocks_from_markdown(markdown, doc_id, 1, image_path=path.name) if markdown else []

    return ParsedDoc(
        doc_id=doc_id,
        file_name=path.name,
        file_type="image",
        blocks=blocks,
        pages=1,
        meta={
            "width": width,
            "height": height,
            "format": image_format,
            "table_count": sum(1 for block in blocks if block.type == "table"),
        },
    )
