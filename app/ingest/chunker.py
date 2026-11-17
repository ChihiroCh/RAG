"""分块：文本递归切分、表格转 HTML、来源标识注入。

每个分块都带 `doc_id`（取自 files/ 中的文件名主干），并写入检索文本头部，
使标识既可用于元数据过滤，也参与向量语义。
"""

from __future__ import annotations

import html
import logging
import re
from typing import Iterable

from app.config import Settings, settings
from app.ingest.models import ParsedDoc
from app.ingest.table_norm import normalize_table

logger = logging.getLogger(__name__)

CHUNKER_VERSION = "v6"
TABLE_MAX_CHARS = 4000
TYPE_LABEL = {"text": "文本", "table": "表格", "image": "图片", "table_row": "表格行"}

DEFAULT_SEPARATORS = ["\n\n", "\n", "。", "；", "！", "？", "，", " "]
TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
# 视觉模型常把表格（可能连续多张）包在 ```html 代码围栏里，入库前剥离围栏保留纯 HTML
FENCE_TABLE_RE = re.compile(
    r"```(?:html)?\s*((?:\s*<table\b.*?</table>)+)\s*```",
    re.IGNORECASE | re.DOTALL,
)


# --------------------------------------------------------------------- 文本切分


def _split_recursive(text: str, size: int, separators: list[str]) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    if not separators:
        return [text[i : i + size] for i in range(0, len(text), size)]

    separator = separators[0]
    if separator not in text:
        return _split_recursive(text, size, separators[1:])

    pieces: list[str] = []
    buffer = ""
    for part in text.split(separator):
        piece = part + separator
        if len(buffer) + len(piece) <= size:
            buffer += piece
            continue
        if buffer.strip():
            pieces.append(buffer.strip())
        if len(piece) > size:
            pieces.extend(_split_recursive(piece, size, separators[1:]))
        else:
            buffer = piece
    if buffer.strip():
        pieces.append(buffer.strip())
    return pieces


def split_text(text: str, size: int = 500, overlap: int = 80) -> list[str]:
    """按段落/句读递归切分，并添加字符级重叠。"""
    text = (text or "").strip()
    if not text:
        return []
    pieces = _split_recursive(text, max(80, size), DEFAULT_SEPARATORS)
    if overlap <= 0 or len(pieces) <= 1:
        return [piece for piece in pieces if piece.strip()]

    merged: list[str] = [pieces[0]]
    for piece in pieces[1:]:
        tail = merged[-1][-overlap:]
        merged.append(f"{tail}{piece}".strip())
    return [piece for piece in merged if piece.strip()]


def _paragraph_units(text: str, limit: int) -> list[str]:
    units: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if len(block) <= limit:
            units.append(block)
        else:
            units.extend(_split_recursive(block, limit, DEFAULT_SEPARATORS))
    return units


def _atomic_units(text: str, limit: int) -> list[str]:
    """把 Markdown 中的 <table> 视为不可切分单元，其余按段落切分。"""
    text = FENCE_TABLE_RE.sub(lambda match: match.group(1), text)
    # 未闭合的 ```html 围栏：去掉紧贴表格的开头标记与表格后的残留闭合标记
    text = re.sub(r"```(?:html)?\s*(?=<table\b)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(</table>)\s*```", r"\1", text, flags=re.IGNORECASE)
    units: list[str] = []
    position = 0
    for match in TABLE_RE.finditer(text):
        units.extend(_paragraph_units(text[position : match.start()], limit))
        units.append(match.group(0).strip())
        position = match.end()
    units.extend(_paragraph_units(text[position:], limit))
    return [unit for unit in units if unit.strip()]


def _pack(units: Iterable[str], size: int, overlap: int = 0) -> list[str]:
    chunks: list[str] = []
    buffer = ""
    for unit in units:
        if not buffer:
            buffer = unit
        elif len(buffer) + len(unit) + 2 <= size:
            buffer = f"{buffer}\n\n{unit}"
        else:
            chunks.append(buffer)
            buffer = f"{buffer[-overlap:]}\n\n{unit}".strip() if overlap > 0 else unit
    if buffer.strip():
        chunks.append(buffer)
    return [chunk for chunk in chunks if chunk.strip()]


# --------------------------------------------------------------------- 表格 HTML


def rows_to_html(rows: list[list[str]]) -> str:
    header = rows[0]
    body = rows[1:]
    thead = "<thead><tr>" + "".join(f"<th>{html.escape(cell, quote=False)}</th>" for cell in header) + "</tr></thead>"
    tbody = ""
    if body:
        body_rows = [
            "<tr>" + "".join(f"<td>{html.escape(cell, quote=False)}</td>" for cell in row) + "</tr>" for row in body
        ]
        tbody = "<tbody>" + "".join(body_rows) + "</tbody>"
    return f"<table>{thead}{tbody}</table>"


def _table_html_parts(rows: list[list[str]], max_chars: int = TABLE_MAX_CHARS) -> list[tuple[str, str]]:
    """返回 [(html, 行范围说明)]，超长表格按行切分并重复表头。"""
    full = rows_to_html(rows)
    if len(full) <= max_chars:
        return [(full, "")]

    header, body = rows[0], rows[1:]
    parts: list[tuple[str, str]] = []
    buffer: list[list[str]] = []
    buffer_len = len(rows_to_html([header]))
    start = 1

    for offset, row in enumerate(body, start=1):
        row_len = len(rows_to_html([header, row])) - buffer_len
        if buffer and buffer_len + row_len > max_chars:
            parts.append((rows_to_html([header, *buffer]), f"（第{start}-{start + len(buffer) - 1}行）"))
            start += len(buffer)
            buffer = []
            buffer_len = len(rows_to_html([header]))
        buffer.append(row)
        buffer_len += max(row_len, 1)
    if buffer:
        parts.append((rows_to_html([header, *buffer]), f"（第{start}-{start + len(buffer) - 1}行）"))
    return parts or [(full, "")]


# --------------------------------------------------------------------- 主流程


def source_header(doc_id: str, file_name: str, page: int, block_type: str, extra: str = "") -> str:
    label = TYPE_LABEL.get(block_type, block_type)
    return f"【来源标识：{doc_id}｜{file_name} 第{page}页｜{label}{extra}】"


def chunk_document(doc: ParsedDoc, cfg: Settings | None = None) -> list[dict]:
    """把一个文件的解析结果切成带来源标识的检索块。"""
    cfg = cfg or settings
    chunks: list[dict] = []
    counter = 0

    for block in doc.blocks:
        page = block.page or 0
        if block.type == "table" and (block.html or block.rows):
            source = block.html if block.html else (block.rows or [])
            norm = normalize_table(source, block.name or "")
            matrix = norm["matrix"]
            if norm["row_count"] < 1 or norm["col_count"] < 1:
                continue
            name = block.name or ""
            header_text = " | ".join(cell for cell in matrix[0] if cell)
            title = f"{name}｜{header_text}" if name else header_text
            body_text = " ".join(norm["row_texts"][1:])
            # 完整父表保存逻辑网格；长表的精确召回由行块承担。
            html_text = norm["html"]
            parts = [(html_text, "", title, body_text)]

            for part_index, (html_part, note, title_text, body) in enumerate(parts, start=1):
                header = source_header(doc.doc_id, doc.file_name, page, "table", f"{note}")
                table_chunk = _make_chunk(
                    doc=doc,
                    chunk_index=counter,
                    page=page,
                    block_type="table",
                    text=f"{header}\n{title_text}\n{body}"[:6000],
                    content=html_part,
                    image_path=None,
                    part=part_index,
                )
                table_chunk.update(table_id=f"{doc.doc_id}_p{page:03d}_t{counter:04d}",
                                   table_name=name, table_cells=norm["cells"],
                                   row_count=norm["row_count"], col_count=norm["col_count"],
                                   caption=block.caption)
                table_id = table_chunk["table_id"]
                chunks.append(table_chunk)
                counter += 1

                # 行级块：便于"某一行/某一项"的问题精确命中，命中后由检索层回填父表
                for row_index, row_text in enumerate(norm["row_texts"][1:], start=1):
                    row_chunk = _make_chunk(
                        doc=doc,
                        chunk_index=counter,
                        page=page,
                        block_type="table_row",
                        text=f"{header}\n{header_text}\n{row_text}"[:2000],
                        content=row_text,
                        image_path=None,
                        part=row_index,
                        parent_id=table_chunk["chunk_id"],
                    )
                    row_chunk.update(table_id=table_id, table_name=name)
                    chunks.append(row_chunk)
                    counter += 1
            continue

        if block.type == "image":
            if not block.text.strip():
                continue
            units = _atomic_units(block.text, max(cfg.chunk_size * 2, 800))
            pieces = _pack(units, max(cfg.chunk_size * 2, 800), 0)
            for part_index, piece in enumerate(pieces, start=1):
                header = source_header(doc.doc_id, doc.file_name, page, "image")
                chunks.append(
                    _make_chunk(
                        doc=doc,
                        chunk_index=counter,
                        page=page,
                        block_type="image",
                        text=f"{header}\n{piece}",
                        content=piece,
                        image_path=block.image_path,
                        part=part_index,
                    )
                )
                counter += 1
            continue

        if not block.text.strip():
            continue
        pieces = split_text(block.text, cfg.chunk_size, cfg.chunk_overlap)
        for part_index, piece in enumerate(pieces, start=1):
            header = source_header(doc.doc_id, doc.file_name, page, "text")
            chunks.append(
                _make_chunk(
                    doc=doc,
                    chunk_index=counter,
                    page=page,
                    block_type="text",
                    text=f"{header}\n{piece}",
                    content=piece,
                    image_path=None,
                    part=part_index,
                )
            )
            counter += 1

    return chunks


def _make_chunk(
    doc: ParsedDoc,
    chunk_index: int,
    page: int,
    block_type: str,
    text: str,
    content: str,
    image_path: str | None,
    part: int,
    parent_id: str | None = None,
) -> dict:
    return {
        "chunk_id": f"{doc.doc_id}_p{page:03d}_{block_type}_{chunk_index + 1:03d}",
        "doc_id": doc.doc_id,
        "file_name": doc.file_name,
        "file_type": doc.file_type,
        "page": page,
        "type": block_type,
        "image_path": image_path,
        "chunk_index": chunk_index,
        "parent_id": parent_id,
        "text": text,
        "content": content,
    }


def build_chunks(docs: Iterable[ParsedDoc], cfg: Settings | None = None) -> list[dict]:
    doc_list = list(docs)
    chunks: list[dict] = []
    for doc in doc_list:
        chunks.extend(chunk_document(doc, cfg))
    logger.info("分块完成：%d 个文件 -> %d 个块", len(doc_list), len(chunks))
    return chunks
