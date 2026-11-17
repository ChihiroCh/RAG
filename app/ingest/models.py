"""解析层的统一数据结构。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ParsedBlock:
    """一个解析块：正文段落 / 表格 / 整页图片（VLM 描述）。"""

    type: str  # text | table | image
    page: int
    text: str = ""
    rows: list[list[str]] | None = None
    caption: str = ""
    image_path: str | None = None
    scan: bool = False  # 是否为扫描页（无文本层）
    html: str = ""  # 表格规范化后的 HTML（含 rowspan/colspan，仅真实单元格）
    name: str = ""  # 表格名（从表题/上下文推断），参与检索

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParsedBlock":
        return cls(
            type=data.get("type") or "text",
            page=int(data.get("page") or 0),
            text=data.get("text") or "",
            rows=data.get("rows"),
            caption=data.get("caption") or "",
            image_path=data.get("image_path"),
            scan=bool(data.get("scan")),
            html=data.get("html") or "",
            name=data.get("name") or "",
        )


@dataclass
class ParsedDoc:
    """单个源文件的解析结果，doc_id 即文件名主干（如 001.pdf -> 001）。"""

    doc_id: str
    file_name: str
    file_type: str  # pdf | image
    blocks: list[ParsedBlock] = field(default_factory=list)
    pages: int = 1
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "file_name": self.file_name,
            "file_type": self.file_type,
            "blocks": [block.to_dict() for block in self.blocks],
            "pages": self.pages,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParsedDoc":
        return cls(
            doc_id=data["doc_id"],
            file_name=data["file_name"],
            file_type=data.get("file_type", "pdf"),
            blocks=[ParsedBlock.from_dict(item) for item in data.get("blocks", [])],
            pages=data.get("pages", 1),
            meta=data.get("meta", {}),
        )
