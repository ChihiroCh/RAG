"""全局配置：全部通过项目根目录 .env 读取，模型与检索参数可自由替换。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """从 .env 读取的配置项，字段名小写，环境变量名大写（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 百炼
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # 四类模型（可配置）
    llm_model: str = "qwen-plus"
    vlm_model: str = "qwen3-vl-plus"
    embed_model: str = "text-embedding-v4"
    embed_dim: int = 1024
    rerank_model: str = "qwen3.7-text-rerank"
    rerank_fallback_model: str = "gte-rerank-v2"
    # 主模型额度耗尽/不存在时自动降级到备用模型（留空则关闭降级）
    llm_fallback_model: str = ""
    vlm_fallback_model: str = ""
    # 质量优先的有序降级链；只在模型额度/权限错误时进入下一项。
    llm_fallback_models: str = "qwen-max,qwen-plus,qwen-flash"
    vlm_fallback_models: str = ""
    ocr_model: str = "qwen-vl-ocr-2025-11-20"
    model_unavailable_ttl: float = 600.0

    # 生成参数
    llm_temperature: float = 0.3
    llm_max_tokens: int = 2048

    # 检索参数
    top_k_vector: int = 20
    top_k_rerank: int = 5
    max_context_chars: int = 8000

    # 混合检索（向量 + BM25）
    bm25_enabled: bool = True
    hybrid_vector_top_k: int = 30
    hybrid_bm25_top_k: int = 30
    hybrid_fuse_top_k: int = 20
    rrf_k: int = 60
    bm25_weight: float = 0.6
    parent_expand_enabled: bool = True
    retrieval_max_table_per_doc: int = 3

    # 查询改写
    query_rewrite_enabled: bool = True
    query_rewrite_llm: bool = True
    query_rewrite_timeout: float = 8.0

    # 结构化答案（数值/数组/结构题走强约束输出）
    numeric_answer_enabled: bool = True

    # 分块参数
    chunk_size: int = 800
    chunk_overlap: int = 100

    # 置信度（0-1）：检索分即时计算 + 大模型一致性校准
    confidence_enabled: bool = True
    confidence_llm_enabled: bool = True
    confidence_high: float = 0.75
    confidence_low: float = 0.45
    # qwen3.7-text-rerank 实测分数区间约 0.31~0.91（gte-rerank-v2 约为 0.10~0.65）
    confidence_rerank_low: float = 0.30
    confidence_rerank_high: float = 0.90
    confidence_vector_low: float = 0.30
    confidence_vector_high: float = 0.75
    confidence_retrieval_weight: float = 0.55
    confidence_timeout: float = 8.0

    # 解析与索引
    vlm_concurrency: int = 4
    embed_batch_size: int = 10
    files_dir: str = "files"
    data_dir: str = "data"

    # ---------- 派生路径 ----------

    @property
    def files_path(self) -> Path:
        return self._resolve(self.files_dir)

    @property
    def data_path(self) -> Path:
        return self._resolve(self.data_dir)

    @property
    def index_dir(self) -> Path:
        return self.data_path / "index"

    @property
    def parsed_dir(self) -> Path:
        return self.data_path / "parsed"

    @property
    def page_cache_dir(self) -> Path:
        return self.data_path / "pages"

    @staticmethod
    def _resolve(value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (BASE_DIR / path)

    # ---------- 校验 ----------

    @property
    def has_api_key(self) -> bool:
        key = self.dashscope_api_key.strip()
        return bool(key) and not key.lower().startswith("sk-xxxx")

    def require_api_key(self) -> str:
        if not self.has_api_key:
            raise RuntimeError(
                "未检测到有效的 DASHSCOPE_API_KEY。\n"
                "请复制 .env.example 为 .env，并填入阿里云百炼的 API Key：\n"
                "  cp .env.example .env\n"
                "  # 编辑 .env 中的 DASHSCOPE_API_KEY=sk-...\n"
                "获取地址：https://bailian.console.aliyun.com/ -> API-KEY 管理"
            )
        return self.dashscope_api_key.strip()

    def masked_key(self) -> str:
        key = self.dashscope_api_key.strip()
        if not key:
            return "未配置"
        if len(key) <= 8:
            return key[:2] + "***"
        return f"{key[:5]}***{key[-4:]}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
