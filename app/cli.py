"""命令行入口：

    python -m app.cli ingest [--rebuild]
    python -m app.cli ask "问题" [--doc 001,077] [--format number|json_array|json]
    python -m app.cli ask --interactive
    python -m app.cli rerank                 # 探测 rerank 模型可用性/分数区间
    python -m app.cli eval --sample 30       # 分层抽样评测，输出 reports/eval-*.md
    python -m app.cli stats
    python -m app.cli serve [--port 8000]
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")


def _split_docs(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def _print_sources(sources: list[dict[str, Any]]) -> None:
    if not sources:
        print("（无引用）")
        return
    print("\n引用来源：")
    for item in sources:
        score = f" 相关度 {item['score']}" if item.get("score") is not None else ""
        print(
            f"  [{item['index']}] 标识 {item['doc_id']}｜{item['file_name']} 第{item['page']}页"
            f" · {item['type_label']}{score}"
        )
        print(f"      {item['snippet']}")


def _print_confidence(confidence: dict[str, Any] | None) -> None:
    """答案下方的置信度（0-1）与构成。"""
    if not confidence or confidence.get("score") is None:
        return

    components = confidence.get("components") or {}
    parts: list[str] = []
    if components.get("retrieval") is not None:
        parts.append(f"检索 {float(components['retrieval']):.2f}")
    if components.get("consistency") is not None:
        parts.append(f"一致性 {float(components['consistency']):.2f}")
    elif confidence.get("degraded"):
        parts.append("精排降级")

    method = "" if confidence.get("method") == "hybrid" else " · 仅检索分"
    detail = f"（{' / '.join(parts)}）" if parts else ""
    print(f"\n置信度 {float(confidence['score']):.2f} · {confidence.get('level_label', '-')}{method}{detail}")
    if confidence.get("reason"):
        print(f"  依据：{confidence['reason']}")
    if confidence.get("hint"):
        print(f"  {confidence['hint']}")


def cmd_ingest(args: argparse.Namespace) -> int:
    from app.ingest.pipeline import ingest

    def progress(event: dict) -> None:
        stage = event.get("stage")
        if stage == "prepare":
            print(f"扫描到 {event.get('total', 0)} 个文件，开始解析…")
        elif stage == "parse":
            print(f"  解析 [{event.get('done')}/{event.get('total')}] {event.get('file')}")
        elif stage == "parse_error":
            print(f"  ! 解析失败 {event.get('file')}：{event.get('message')}")
        elif stage == "chunk":
            if event.get("chunks"):
                print(f"  分块完成：{event['chunks']} 个块")
        elif stage == "embed":
            print(f"  向量化 [{event.get('done')}/{event.get('total')}]")
        elif stage == "save":
            print("  写入索引…")

    try:
        result = ingest(rebuild=args.rebuild, progress=progress)
    except Exception as exc:  # noqa: BLE001
        print(f"\n入库失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"\n完成（{result['mode']}）：本次处理 {result.get('processed_docs', 0)} 个文件，"
        f"新增 {result.get('new_chunks', 0)} 个分块；索引共 {result['chunks']} 块 / {result['documents']} 个文件，"
        f"耗时 {result['elapsed']}s"
    )
    print(f"索引目录：{settings.index_dir}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from app.rag.qa import RAGChat

    chat = RAGChat()
    if not chat.ready:
        print("索引为空，请先运行：python -m app.cli ingest", file=sys.stderr)
        return 1

    doc_ids = _split_docs(args.doc)
    history: list[dict[str, Any]] = []

    answer_format = getattr(args, "answer_format", None)

    if args.question:
        result = chat.ask(args.question, history=None, doc_ids=doc_ids, answer_format=answer_format)
        if result["error"]:
            print(f"出错：{result['error']}", file=sys.stderr)
            return 1
        print(result["answer"])
        _print_confidence(result.get("confidence"))
        _print_sources(result["sources"])
        return 0

    print("多模态知识库交互问答（输入 exit / Ctrl+C 退出）")
    if doc_ids:
        print(f"仅检索来源标识：{', '.join(doc_ids)}")
    while True:
        try:
            question = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            return 0
        if question.lower() in {"exit", "quit", "q", ""}:
            print("再见")
            return 0
        result = chat.ask(question, history=history, doc_ids=doc_ids, answer_format=answer_format)
        if result["error"]:
            print(f"出错：{result['error']}", file=sys.stderr)
            continue
        print(f"\n助手 > {result['answer']}")
        _print_confidence(result.get("confidence"))
        _print_sources(result["sources"])
        history = history + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": result["answer"]},
        ]


def cmd_stats(args: argparse.Namespace) -> int:
    from app.ingest.pipeline import get_stats

    stats = get_stats()
    if not stats["chunks"]:
        print("索引为空，请先运行：python -m app.cli ingest")
        return 0

    print(f"索引分块：{stats['chunks']}")
    print(f"文件数量：{stats['documents']}")
    print(f"向量维度：{stats['dim']}")
    by_type = stats.get("by_type", {})
    print("分块类型：" + "，".join(f"{key} {value}" for key, value in by_type.items()))
    manifest = stats.get("manifest", {})
    if manifest:
        print(
            "模型：" + f"LLM={manifest.get('llm_model')} VLM={manifest.get('vlm_model')} "
            f"EMBED={manifest.get('embed_model')} RERANK={manifest.get('rerank_model')}"
        )
        print(f"构建时间：{manifest.get('built_at')}")
    print("\n文件明细：")
    for doc in stats["documents_detail"]:
        print(
            f"  {doc['doc_id']:>4}  {doc['file_name']:<14} 分块 {doc['chunks']:>3}  "
            f"类型 {'/'.join(doc['types'])}"
        )
    return 0


def cmd_rerank(args: argparse.Namespace) -> int:
    from app.clients.rerank import Reranker

    reranker = Reranker()
    info = reranker.probe(force=True)
    print(f"rerank 模型：{info['model']}")
    print(f"调用路径：{info['path']}（ok={info['ok']}）")
    print(f"分数区间：{info['score_range']}")
    if info.get("error"):
        print(f"错误信息：{info['error']}")
    return 0 if info["ok"] else 1


def cmd_eval(args: argparse.Namespace) -> int:
    from app.eval import run_eval

    result = run_eval(sample=args.sample, out=args.out, seed=args.seed, tests_path=args.tests)
    metrics = result["metrics"]
    print("评测完成")
    for key, value in metrics.items():
        print(f"  {key}: {value}")
    print(f"报告：{result['report']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    print(f"启动 Web 服务：http://{args.host}:{args.port}")
    uvicorn.run("app.api:app", host=args.host, port=args.port, reload=args.reload, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="多模态 RAG 知识库命令行工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="解析 files/ 并构建/更新向量索引")
    p_ingest.add_argument("--rebuild", action="store_true", help="忽略增量判断，全量重建索引")
    p_ingest.set_defaults(func=cmd_ingest)

    p_ask = sub.add_parser("ask", help="提问（单次或交互式）")
    p_ask.add_argument("question", nargs="?", default="", help="问题内容；留空则进入交互模式")
    p_ask.add_argument("--doc", default=None, help="仅检索指定来源标识，逗号分隔，如 001,077")
    p_ask.add_argument("--interactive", "-i", action="store_true", help="进入多轮交互模式")
    p_ask.add_argument(
        "--format",
        dest="answer_format",
        default=None,
        choices=["string", "number", "json_array", "json"],
        help="结构化取值格式（number/json_array/json 走强约束输出）",
    )
    p_ask.set_defaults(func=cmd_ask)

    p_rerank = sub.add_parser("rerank", help="探测 rerank 模型可用性与分数区间")
    p_rerank.set_defaults(func=cmd_rerank)

    p_eval = sub.add_parser("eval", help="按题型抽样评测并生成报告")
    p_eval.add_argument("--sample", type=int, default=30, help="抽样题数")
    p_eval.add_argument("--out", default=None, help="报告输出路径（默认 reports/eval-<ts>.md）")
    p_eval.add_argument("--seed", type=int, default=42)
    p_eval.add_argument("--tests", default=None, help="测试集路径（默认 ./tests.xlsx）")
    p_eval.set_defaults(func=cmd_eval)

    p_stats = sub.add_parser("stats", help="查看索引统计")
    p_stats.set_defaults(func=cmd_stats)

    p_serve = sub.add_parser("serve", help="启动 Web 界面")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "ask" and args.interactive:
        args.question = ""
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
