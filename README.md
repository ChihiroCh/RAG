# 多模态 RAG 知识库

基于阿里云百炼的多模态检索问答系统：把 `files/` 目录下的 **PDF、图片（PNG/JPG/WebP）** 解析入库（表格统一转为 HTML 结构），用 **FAISS + 百炼向量模型** 建索引，检索采用 **向量 + BM25 混合召回 + RRF 融合 + qwen3.7-text-rerank 精排**，由 **qwen 大模型** 流式生成带引用来源的回答。提供 Web 界面与命令行两种使用方式。

## 功能特性

- **多模态解析**：PDF 文本层优先提取；检测到的表格转为 `<table>` HTML 独立分块；扫描版 PDF（无文本层）自动渲染页面交视觉模型识别；独立图片由视觉模型输出结构化 Markdown（表格转 HTML）。
- **来源标识（doc_id）**：每个分块携带来源标识，取值为 `files/` 中文件名主干（`001.pdf` → `001`），并注入检索文本头部，贯穿索引、检索、引用展示全链路。
- **增量入库**：按文件内容哈希判断变化，新增文件追加向量；文件被修改或删除、或模型/分块参数变更时自动全量重建；解析结果缓存到 `data/parsed/`，重建索引不重复调用视觉模型。
- **可信回答**：答案逐句标注 `[1][2]` 引用编号，Web 端引用卡片可回看原图（扫描页可回看渲染页）。
- **置信度（0-1）**：检索完成即给出初判（不增加首字延迟），答案生成结束后由大模型评估"答案与引用资料的一致性"并融合为终值。Web 回答底部显示「置信度 0.82 · 高」胶囊（悬停看构成），CLI 在答案下方打印；低于阈值时提示"资料可能不足，请核实引用原文"。
- **可配置**：四类模型（对话/视觉/向量/重排）与检索、分块参数全部在 `.env` 中配置。

## 快速开始

```bash
# 1. 配置
cp .env.example .env
# 编辑 .env，填入百炼 API Key：https://bailian.console.aliyuncs.com/ -> API-KEY 管理

# 2. 安装依赖（本机已装齐则跳过）
pip install -r requirements.txt

# 3. 构建索引（首次约 3~10 分钟，含视觉模型调用）
python -m app.cli ingest

# 4a. 命令行问答
python -m app.cli ask "010 这张日程表里六天分别安排了什么？"
python -m app.cli ask            # 交互模式
python -m app.cli ask "问题" --doc 001,010   # 仅检索指定来源标识

# 4b. Web 界面
python -m app.cli serve          # 或 uvicorn app.api:app --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

## 配置说明（.env）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | — | 百炼 API Key（必填） |
| `DASHSCOPE_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容地址，国际站换成 `dashscope-intl` |
| `LLM_MODEL` | `qwen-plus` | 对话生成模型 |
| `VLM_MODEL` | `qwen3-vl-plus` | 视觉理解模型，可换 `qwen-vl-max` / `qwen3.5-omni-plus` 等 |
| `EMBED_MODEL` | `text-embedding-v4` | 向量模型 |
| `EMBED_DIM` | `1024` | 向量维度（需与模型匹配，改动会触发全量重建） |
| `RERANK_MODEL` | `qwen3.7-text-rerank` | 重排序模型（不支持时自动走 HTTP，可回退 `gte-rerank-v2`） |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` | `0.3` / `2048` | 生成参数 |
| `TOP_K_VECTOR` / `TOP_K_RERANK` | `20` / `5` | 向量召回数 / 精排保留数 |
| `BM25_ENABLED` / `HYBRID_VECTOR_TOP_K` / `HYBRID_BM25_TOP_K` | `true` / `30` / `30` | 混合检索开关与两路召回数 |
| `HYBRID_FUSE_TOP_K` / `RRF_K` / `BM25_WEIGHT` | `20` / `60` / `0.6` | RRF 融合候选数与权重 |
| `PARENT_EXPAND_ENABLED` / `RETRIEVAL_MAX_TABLE_PER_DOC` | `true` / `3` | 命中表格行时回填父表 / 单文件最多表格数 |
| `QUERY_REWRITE_ENABLED` / `QUERY_REWRITE_LLM` | `true` / `true` | 规则改写（去噪/同义归一）/ 大模型改写检索式 |
| `QUERY_REWRITE_TIMEOUT` | `8` | 大模型改写超时（秒），超时降级为规则改写 |
| `NUMERIC_ANSWER_ENABLED` | `true` | number/json_array/json 题走强约束取值输出 |
| `MAX_CONTEXT_CHARS` | `8000` | 送入大模型的上下文上限 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `800` / `100` | 分块大小与重叠 |
| `CONFIDENCE_ENABLED` | `true` | 置信度总开关（关闭后不计算、不下发） |
| `CONFIDENCE_LLM_ENABLED` | `true` | 是否用大模型做一致性校准（关闭则仅用检索分） |
| `CONFIDENCE_HIGH` / `CONFIDENCE_LOW` | `0.75` / `0.45` | 高/低等级阈值，低分附带核实提醒 |
| `CONFIDENCE_RERANK_LOW` / `_HIGH` | `0.30` / `0.90` | 精排分归一化区间（qwen3.7-text-rerank 实测；换回 gte 用 0.10/0.65） |
| `CONFIDENCE_VECTOR_LOW` / `_HIGH` | `0.30` / `0.75` | 向量分归一化区间（精排不可用时降级） |
| `CONFIDENCE_RETRIEVAL_WEIGHT` | `0.55` | 融合权重：`w×检索分 + (1-w)×一致性分` |
| `CONFIDENCE_TIMEOUT` | `8` | 一致性校准超时（秒），超时降级为检索分 |
| `VLM_CONCURRENCY` | `4` | 视觉模型并发（过高易限流） |
| `EMBED_BATCH_SIZE` | `10` | 向量化批大小（v4 上限 10） |
| `FILES_DIR` / `DATA_DIR` | `files` / `data` | 源数据与索引缓存目录 |

## 置信度如何计算

| 阶段 | 计算方式 | 说明 |
| --- | --- | --- |
| 检索分（即时） | `0.60×精排最高分归一化 + 0.22×top1与top2差距 + 0.18×有效来源覆盖` | 只用已召回候选，零网络调用，首字前即可展示 |
| 一致性分（生成后） | 大模型依据"问题 + 答案 + 前 3 条引用资料"输出 `{"consistency":0-1,"reason":...}` | 失败/超时/非法 JSON 均降级为纯检索分 |
| 终值 | `融合分 × (0.85 + 0.15×引用命中率)` | 答案出现"资料不足/无法回答"时封顶 0.30 |

等级：`≥ 0.75` 高、`0.45 ~ 0.75` 中、`< 0.45` 低（低分展示"资料可能不足，请核实引用原文"）。Web 端悬停胶囊可查看精排分、分数间隔、来源覆盖、一致性、引用命中等明细。

CLI 输出示例：

```text
置信度 0.85 · 高（检索 0.89 / 一致性 0.80）
  依据：答案主体内容与[1]一致，但部分细节在[1]中未明确提及
```

## 常用命令

```bash
python -m app.cli ingest [--rebuild]   # 增量入库 / 全量重建
python -m app.cli stats                # 索引统计（按来源标识分组）
python -m app.cli ask "问题" [--doc 001] [-i] [--format number|json_array|json]
python -m app.cli rerank               # 探测 rerank 模型可用性与分数区间
python -m app.cli eval --sample 30     # 分层抽样评测，产出 reports/eval-*.md 供人工核对
python -m app.cli serve [--host 0.0.0.0 --port 8000]
```

Web 端接口：

- `POST /api/chat` — SSE 流式问答（`question` / `history` / `doc_ids` / `answer_format`）
- `GET  /api/status` — 索引状态、模型配置、入库进度
- `POST /api/ingest` — 后台入库（`{"rebuild": true}` 全量）
- `GET  /api/files/{name}` — 原图预览；`GET /api/page/{name}/{page}` — PDF 页面渲染图
- `GET  /api/chunks?doc_id=001` — 查看某来源标识的全部分块

## 目录结构

```
app/
├── config.py            # .env 配置加载与校验
├── clients/             # 百炼客户端：对话/流式/视觉/向量 + 重排序（失败降级）
├── ingest/              # 解析与分块
│   ├── pdf_parser.py    # 文本层 + 表格检测（行片段合并）+ 扫描页渲染 VLM
│   ├── image_parser.py  # 图片预处理 + VLM 结构化识别
│   ├── chunker.py       # 递归分块、表格行切分、来源标识注入
│   └── pipeline.py      # doc_id 生成、并发调度、缓存、增量判定
├── store/faiss_store.py # FAISS IndexFlatIP + 元数据持久化
├── rag/                 # 检索（召回+精排）与问答编排
├── api.py               # FastAPI 接口
├── cli.py               # 命令行工具
└── web/                 # 单页前端（原生 HTML/CSS/JS）
data/
├── parsed/              # 解析缓存（按文件哈希）
├── index/               # index.faiss / chunks.jsonl / vectors.npy / manifest.json
└── pages/               # PDF 页面渲染缓存
files/                   # 源数据（不要改动）
```

## 常见问题

- **提示未配置 API Key**：确认 `.env` 中 `DASHSCOPE_API_KEY` 是真实 Key（不是 `sk-xxxx` 占位符），修改后重启服务。
- **视觉模型报 Model not exist**：当前账号未开通该模型，在 `.env` 中换一个账号可用的视觉模型（如 `qwen-vl-max`、`qwen3.5-omni-plus`），可用 `python -c "from app.clients.llm import get_llm_client; print([m.id for m in get_llm_client().client.models.list().data])"` 查看可用模型。
- **想彻底重建**：Web 端点「重建索引」或 `python -m app.cli ingest --rebuild`；解析缓存会避免重复调用视觉模型（缓存文件名带 `PARSER_VERSION`，改提示词后自动失效重解析），若要连缓存一起清空则删除 `data/parsed/`。
- **rerank 换模型后置信度偏低/偏高**：`python -m app.cli rerank` 看分数区间，再调 `CONFIDENCE_RERANK_LOW/HIGH`。
- **数值题答案带解释文字**：用 `--format number`（或 `json_array`）走结构化取值；Web 端可在请求体加 `answer_format`。
- **限流/超时**：降低 `VLM_CONCURRENCY` 与 `EMBED_BATCH_SIZE`；客户端已内置指数退避重试。
