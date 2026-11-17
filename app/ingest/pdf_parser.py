"""PDF 解析：文本层优先 + 表格转结构化行 + 扫描页渲染后交视觉模型。

策略说明：
- 有文本层的页面用 pdfplumber 提取正文；检测到的表格区域从正文中剔除，避免重复与乱序。
- 表格以二维行数据返回，由 chunker 渲染成 <table> HTML 入库。
- 可提取字符数低于阈值（判定为扫描页）时，用 pypdfium2 渲染整页为 JPEG，交 qwen-vl 识别。
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Callable

import pdfplumber
import pypdfium2 as pdfium

from app.clients.llm import PAGE_STRUCT_PROMPT
from app.ingest.models import ParsedBlock, ParsedDoc
from app.ingest.table_norm import blocks_from_markdown, guess_table_name, normalize_table

logger = logging.getLogger(__name__)

# 单页可提取字符数低于该值视为扫描页
SCAN_CHAR_THRESHOLD = 20
# 渲染缩放倍率（2.6 倍 ≈ 190DPI，改善小字与千分位识别）
PAGE_RENDER_SCALE = 2.6
PAGE_JPEG_QUALITY = 90

VisionFn = Callable[[bytes], str]


def _default_vision(image_bytes: bytes) -> str:
    from app.clients.llm import get_llm_client

    return get_llm_client().vision_describe(image_bytes, prompt=PAGE_STRUCT_PROMPT)


def render_page_jpeg(
    pdf_path: str | Path,
    page_no: int,
    scale: float = PAGE_RENDER_SCALE,
    quality: int = 85,
) -> bytes:
    """把 PDF 指定页（1 基）渲染为 JPEG 字节流。"""
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        if page_no < 1 or page_no > len(doc):
            raise IndexError(f"页码越界：{page_no}/{len(doc)}")
        page = doc[page_no - 1]
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil().convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality)
        return buffer.getvalue()
    finally:
        doc.close()


def _clean_rows(rows: list[list[str | None]] | None) -> list[list[str]]:
    """表格单元格清洗：去换行、去首尾空白、补齐列数、丢弃全空行。"""
    if not rows:
        return []
    cleaned: list[list[str]] = []
    for row in rows:
        cells = [re.sub(r"\s+", " ", str(cell)).strip() if cell is not None else "" for cell in row]
        if any(cells):
            cleaned.append(cells)
    if not cleaned:
        return []
    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]
    # 单行单列的"表格"通常是误检
    if len(cleaned) < 2 and width < 2:
        return []
    return cleaned


def _merge_row_fragments(
    tables: list[tuple[tuple[float, float, float, float], list[list[str]]]],
) -> list[tuple[tuple[float, float, float, float], list[list[str]]]]:
    """把被横线切碎的"单行表格"合并成完整表格（资产负债表常见）。

    仅当相邻片段左右边界一致且垂直间距很小、且两者都是单行时才合并，
    避免把不同的表格错误拼接。
    """
    ordered = sorted(tables, key=lambda item: (round(item[0][1], 1), item[0][0]))
    merged: list[tuple[tuple[float, float, float, float], list[list[str]]]] = []
    for bbox, rows in ordered:
        if merged:
            prev_bbox, prev_rows = merged[-1]
            same_x = abs(bbox[0] - prev_bbox[0]) < 2 and abs(bbox[2] - prev_bbox[2]) < 2
            gap = bbox[1] - prev_bbox[3]
            if same_x and gap < 12 and len(prev_rows) == 1 and len(rows) == 1:
                merged[-1] = ((prev_bbox[0], prev_bbox[1], prev_bbox[2], bbox[3]), prev_rows + rows)
                continue
        merged.append((bbox, rows))
    return merged


def _is_useful_table(rows: list[list[str]]) -> bool:
    """过滤单行单列等明显误检的表格。"""
    if not rows:
        return False
    width = len(rows[0])
    if width < 2:
        return False
    if len(rows) < 2 and sum(1 for cell in rows[0] if cell) < 3:
        return False
    return True


def _text_outside_tables(page, bboxes: list[tuple[float, float, float, float]]) -> str:
    """剔除表格区域内的字符后再提取正文。"""

    def keep(obj) -> bool:
        if obj.get("object_type") != "char":
            return True
        cx = (obj["x0"] + obj["x1"]) / 2
        cy = (obj["top"] + obj["bottom"]) / 2
        for x0, top, x1, bottom in bboxes:
            if x0 <= cx <= x1 and top <= cy <= bottom:
                return False
        return True

    try:
        return page.filter(keep).extract_text() or ""
    except Exception as exc:  # noqa: BLE001 - 过滤失败则退回全文
        logger.warning("剔除表格区域失败，退回整页文本：%s", exc)
        return page.extract_text() or ""


def parse_pdf(
    path: str | Path,
    doc_id: str,
    vision: VisionFn | None = None,
) -> ParsedDoc:
    """解析整个 PDF，返回带 doc_id 的块列表。"""
    path = Path(path)
    vision = vision or _default_vision
    blocks: list[ParsedBlock] = []
    scan_pages: list[int] = []
    table_count = 0
    pages = 0
    parse_errors = []

    with pdfplumber.open(str(path)) as pdf:
        pages = len(pdf.pages)
        for page_no, page in enumerate(pdf.pages, start=1):
            kept_tables: list[tuple[tuple[float, float, float, float], list[list[str]]]] = []
            try:
                found = page.find_tables()
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s 第%d页表格检测失败：%s", path.name, page_no, exc)
                found = []
            # PDF 辅助线会产生极窄空列/大量合并占位；这类布局用视觉恢复。
            noisy_layout = len(found) > 8
            for candidate in found:
                raw = candidate.extract() or []
                total = sum(len(r) for r in raw)
                none_count = sum(v is None for r in raw for v in r)
                if total and none_count / total > 0.25:
                    noisy_layout = True
                if any(cell[2] - cell[0] < 8 for cell in candidate.cells):
                    noisy_layout = True
            if noisy_layout:
                found = []

            for table in found:
                try:
                    rows = _clean_rows(table.extract())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("%s 第%d页表格解析失败：%s", path.name, page_no, exc)
                    continue
                if rows:
                    kept_tables.append((table.bbox, rows))

            kept_tables = [
                item for item in _merge_row_fragments(kept_tables) if _is_useful_table(item[1])
            ]

            bboxes = [bbox for bbox, _ in kept_tables]
            text = _text_outside_tables(page, bboxes) if bboxes else (page.extract_text() or "")
            text = text.strip()
            compact = re.sub(r"\s+", "", text)

            if len(compact) < SCAN_CHAR_THRESHOLD and not kept_tables:
                # 扫描页：渲染后交视觉模型，拆成规范化表格块 + 正文块
                scan_pages.append(page_no)
                try:
                    image_bytes = render_page_jpeg(path, page_no, quality=PAGE_JPEG_QUALITY)
                    markdown = vision(image_bytes).strip()
                except Exception as exc:  # noqa: BLE001
                    logger.error("%s 第%d页视觉识别失败：%s", path.name, page_no, exc)
                    parse_errors.append({"page": page_no, "error": str(exc)[:300]})
                    continue
                if markdown:
                    page_blocks = blocks_from_markdown(markdown, doc_id, page_no, scan=True)
                    table_count += sum(1 for block in page_blocks if block.type == "table")
                    blocks.extend(page_blocks)
                continue

            if text:
                blocks.append(ParsedBlock(type="text", page=page_no, text=text))

            # 无边框/复杂排版表格可能有文本层，却无法由 find_tables 检测。
            if not kept_tables:
                try:
                    markdown = vision(render_page_jpeg(path, page_no, quality=PAGE_JPEG_QUALITY)).strip()
                    visual_blocks = blocks_from_markdown(markdown, doc_id, page_no)
                    visual_tables = [b for b in visual_blocks if b.type == "table"]
                    table_count += len(visual_tables)
                    blocks.extend(visual_tables)
                except Exception as exc:
                    parse_errors.append({"page": page_no, "error": str(exc)[:300]})
                    logger.error("%s 第%d页无边框表格识别失败：%s", path.name, page_no, exc)

            for index, (_, rows) in enumerate(kept_tables, start=1):
                table_count += 1
                norm = normalize_table(rows)
                if norm["row_count"] < 1 or norm["col_count"] < 1:
                    continue
                name = guess_table_name(text, norm["matrix"], fallback=f"表格{index}")
                blocks.append(
                    ParsedBlock(
                        type="table",
                        page=page_no,
                        rows=norm["matrix"],
                        html=norm["html"],
                        name=name,
                        caption=(
                            f"{doc_id} 第{page_no}页 {name}"
                            f"（{norm['row_count']}行{norm['col_count']}列）"
                            + " " + "；".join(re.findall(r"(?:单位|币种)[：:]\s*[^\n]{1,50}", text))
                        ),
                    )
                )

    meta = {
        "parse_errors": parse_errors,
        "scan_pages": scan_pages,
        "table_count": table_count,
        "char_count": sum(len(block.text) for block in blocks if block.type == "text"),
    }
    logger.info(
        "解析完成 %s：%d 页，%d 块（表格 %d，扫描页 %d）",
        path.name,
        pages,
        len(blocks),
        table_count,
        len(scan_pages),
    )
    return ParsedDoc(
        doc_id=doc_id,
        file_name=path.name,
        file_type="pdf",
        blocks=blocks,
        pages=pages,
        meta=meta,
    )
