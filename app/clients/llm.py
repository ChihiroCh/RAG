"""百炼（OpenAI 兼容模式）客户端：对话、流式、视觉理解、向量化。"""

from __future__ import annotations

import base64
import logging
import time
from threading import local
from functools import lru_cache
from typing import Iterable, Iterator, Sequence

import numpy as np
from openai import OpenAI

from app.config import Settings, settings

logger = logging.getLogger(__name__)

_RETRY_HINTS = (
    "429",
    "rate limit",
    "ratelimit",
    "thrott",
    "timeout",
    "timed out",
    "502",
    "503",
    "504",
    "connection",
    "temporarily",
    "internal error",
)

# 模型级不可用（额度耗尽/模型不存在/无权限）：重试无用，只能换模型
_MODEL_ERROR_HINTS = (
    "insufficient_quota",
    "free quota exhausted",
    "quota",
    "model not exist",
    "model_not_found",
    "unsupported model",
    "invalid model",
    "does not exist",
    "permission",
    "403",
    "404",
)

# 进程内记住已不可用的模型，后续调用直接走备用模型，避免每次都浪费一次请求
_unavailable_models: dict[str, str] = {}
_unavailable_until: dict[str, float] = {}

VLM_PROMPT = """你是一名专业的文档信息提取专家。请仔细阅读图片，输出结构化的中文 Markdown 内容，要求：
1. 第一行用一句话概括图片主题（加粗）。
2. 随后用 Markdown 小标题与要点列表，完整提取图中所有关键信息：标题、层级、条目、时间、编号、单位、备注等。
3. 如果图片中包含表格，必须转换为标准 HTML `<table>`（含 `<thead>`/`<tbody>`），保留全部表头与单元格数值，不要省略行、不要合并单元格。
4. 保留原始数字、符号、英文缩写与专有名词，不得改写数值，不得臆造图中不存在的信息。
5. 不要输出"以下是识别结果"之类的客套话，直接输出正文。
"""

# 整页结构化：表格在前、正文在后，表名与期间字样必须保留（检索强依赖）
PAGE_STRUCT_PROMPT = """你是一名财务报表与文档结构识别专家。请识别图片中的全部内容，严格按以下格式输出：

1. 先输出该页所有表格，每个表格前单独一行写表名（取自图中的表题，如"应收账款""坏账准备""存货"；没有表题时用表头关键词概括），紧接着输出标准 HTML 表格：
   - 必须使用 `<table><tr><td>` 结构，不要用 Markdown 管道表；
   - 合并单元格必须用 `rowspan`/`colspan` 属性表达，**不要输出被合并覆盖的空单元格**；
   - 表头与数据行都要输出，不得省略任何行或列；
   - 数字原样保留（含千分位与小数位），不得换算单位、不得四舍五入；
   - 表名、单位（元/万元）、期间（期末余额/期初余额/本期/上期）若出现在图中，必须写在表名行或单元格文本里。
2. 表格输出完毕后，用 `## 正文` 开头，按 Markdown 小标题与要点列出其余文字内容（标题、编号、口径说明、附注等），保留原始措辞与数字。
3. 不要输出"以下是识别结果"等客套话，不要解释识别过程，不要编造图中不存在的内容。
"""

# 单表重识别：只输出表格 HTML
TABLE_HTML_PROMPT = """请把图片中的表格转换为标准 HTML 表格，要求：
1. 只输出 `<table>...</table>`，不要输出任何解释、标题或 Markdown 围栏；
2. 每个真实单元格输出一次；横向合并用 `colspan`、纵向合并用 `rowspan`，**被合并覆盖的位置不要输出空单元格**；
3. 表头与所有数据行都要完整输出，不得省略行列；
4. 数字原样保留（含千分位与小数位），不得换算单位、不得改写；
5. 表格上方的表名与单位说明，作为第一行前置文本写在 `<caption>` 中（没有则省略）。
"""


def _is_model_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(hint in message for hint in _MODEL_ERROR_HINTS)


def friendly_error(exc: Exception) -> str:
    """把上游报错翻译成用户能直接处理的提示。"""
    message = str(exc)
    lowered = message.lower()
    if "insufficient_quota" in lowered or "free quota exhausted" in lowered:
        return (
            "当前模型免费额度已用尽。请在百炼控制台充值或关闭「仅用免费额度」模式，"
            "或把 .env 中的 LLM_MODEL 换成仍有额度的模型（如 qwen-plus / qwen-flash）后重启服务。"
        )
    if "model not exist" in lowered or "unsupported model" in lowered or "model_not_found" in lowered:
        return f"模型不存在或当前账号无权限：{message[:160]}。请检查 .env 中的模型名配置。"
    return message


class LLMClient:
    """一个实例内复用底层连接，供解析、检索、问答共用。"""

    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or settings
        self._client: OpenAI | None = None
        self._trace = local()

    def reset_call_trace(self) -> None:
        self._trace.calls = []

    def call_trace(self) -> list[dict]:
        return list(getattr(self._trace, "calls", []))

    def ocr_read(self, image_bytes: bytes, prompt: str = "识别图片中的全部原始文字和数字，不推理、不计算。") -> str:
        return self.vision_describe(image_bytes, prompt=prompt, model=self.cfg.ocr_model, max_tokens=8192)

    # ------------------------------------------------------------------ 基础

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            api_key = self.cfg.require_api_key()
            self._client = OpenAI(
                api_key=api_key,
                base_url=self.cfg.dashscope_base_url,
                timeout=180.0,
                max_retries=0,
            )
        return self._client

    def _with_retry(self, fn, desc: str, attempts: int = 4, base_delay: float = 1.5):
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - 统一重试策略
                last_exc = exc
                message = str(exc).lower()
                retryable = any(hint in message for hint in _RETRY_HINTS)
                if attempt == attempts - 1 or not retryable:
                    raise
                delay = base_delay * (2**attempt)
                logger.warning(
                    "%s 调用失败（第 %d 次），%.1fs 后重试：%s",
                    desc,
                    attempt + 1,
                    delay,
                    str(exc)[:200],
                )
                time.sleep(delay)
        raise last_exc  # pragma: no cover

    def _run_with_fallback(
        self,
        make_call,
        model: str,
        fallback: str,
        desc: str,
        attempts: int = 4,
    ):
        """先试主模型；额度耗尽/模型不存在等"模型级"错误时自动切到备用模型。"""
        configured = self.cfg.llm_fallback_models if desc in ("对话模型", "流式对话") else self.cfg.vlm_fallback_models
        # 显式指定模型的探测调用可通过空配置禁用所有降级。
        candidates = list(dict.fromkeys([model, *[m.strip() for m in (configured + "," + fallback).split(",") if m.strip()]]))
        errors = []
        for name in candidates:
            if _unavailable_until.get(name, 0) > time.monotonic():
                errors.append(f"{name}: { _unavailable_models.get(name, '不可用') }")
                continue
            try:
                result = self._with_retry(lambda: make_call(name), desc=f"{desc} {name}", attempts=attempts)
                actual = getattr(result, "model", None) or name
                calls = getattr(self._trace, "calls", [])
                calls.append({"kind": desc, "requested_model": model, "selected_model": name,
                              "actual_model": actual, "degraded": name != model})
                self._trace.calls = calls
                return result
            except Exception as exc:
                if not _is_model_error(exc):
                    raise
                _unavailable_models[name] = str(exc)[:200]
                _unavailable_until[name] = time.monotonic() + self.cfg.model_unavailable_ttl
                errors.append(f"{name}: {str(exc)[:120]}")
                logger.warning("%s %s 不可用，检查下一档模型：%s", desc, name, str(exc)[:120])
        raise RuntimeError("配置的模型均不可用：" + "；".join(errors))

    # ------------------------------------------------------------------ 文本

    def complete(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        def _call(name: str):
            return self.client.chat.completions.create(
                model=name,
                messages=messages,
                temperature=self.cfg.llm_temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.cfg.llm_max_tokens,
            )

        resp = self._run_with_fallback(
            _call,
            model or self.cfg.llm_model,
            self.cfg.llm_fallback_model,
            desc="对话模型",
        )
        return (resp.choices[0].message.content or "").strip()

    def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        def _call(name: str):
            return self.client.chat.completions.create(
                model=name,
                messages=messages,
                temperature=self.cfg.llm_temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.cfg.llm_max_tokens,
                stream=True,
            )

        stream = self._run_with_fallback(
            _call,
            model or self.cfg.llm_model,
            self.cfg.llm_fallback_model,
            desc="流式对话",
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield piece

    # ------------------------------------------------------------------ 视觉

    def vision_describe(
        self,
        image_bytes: bytes,
        prompt: str = VLM_PROMPT,
        mime: str = "image/jpeg",
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        def _call(name: str):
            return self.client.chat.completions.create(
                model=name,
                messages=messages,
                temperature=0.1,
                max_tokens=max_tokens,
            )

        resp = self._run_with_fallback(
            _call,
            model or self.cfg.vlm_model,
            self.cfg.vlm_fallback_model,
            desc="视觉模型",
            attempts=3,
        )
        return (resp.choices[0].message.content or "").strip()

    # ------------------------------------------------------------------ 向量

    def embed_texts(
        self,
        texts: Sequence[str],
        batch_size: int | None = None,
        model: str | None = None,
    ) -> np.ndarray:
        """批量向量化，返回 (n, dim) float32 数组；失败自带重试。"""
        if not texts:
            return np.zeros((0, self.cfg.embed_dim), dtype="float32")

        model_name = model or self.cfg.embed_model
        size = max(1, batch_size or self.cfg.embed_batch_size)
        vectors: list[list[float]] = []

        for start in range(0, len(texts), size):
            batch = [self._clean_text(t) for t in texts[start : start + size]]
            payload = {
                "model": model_name,
                "input": batch,
                "encoding_format": "float",
                "dimensions": self.cfg.embed_dim,
            }

            def _call():
                try:
                    return self.client.embeddings.create(**payload)
                except Exception as exc:  # 部分模型不支持 dimensions 参数
                    if "dimension" in str(exc).lower():
                        payload.pop("dimensions", None)
                        return self.client.embeddings.create(**payload)
                    raise

            resp = self._with_retry(_call, desc=f"向量模型 {model_name}")
            ordered = sorted(resp.data, key=lambda item: item.index)
            vectors.extend([item.embedding for item in ordered])
            logger.debug("向量化进度 %d/%d", min(start + size, len(texts)), len(texts))

        array = np.asarray(vectors, dtype="float32")
        if array.ndim != 2 or array.shape[0] != len(texts):
            raise RuntimeError(f"向量化返回数量异常：期望 {len(texts)}，实际 {array.shape}")
        return array

    @staticmethod
    def _clean_text(text: str, limit: int = 6000) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            return "空内容"
        return cleaned[:limit]


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    return LLMClient()
