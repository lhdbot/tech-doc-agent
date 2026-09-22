"""技术文档多 agent 写作系统 — 命令行入口。

用法：
    python main.py "Kubernetes 集群高可用方案"
    python main.py "RAG 检索增强方案设计" --profile deepseek --auto
    python main.py "..." --orchestrator-profile kimi --writer-profile deepseek
    python main.py "..." --no-research --no-review   # 关闭调研与审校，省钱快跑
"""
from __future__ import annotations

import argparse
import sys

from llm import LLM, load_config
from pipeline import DocPipeline
from tools import create_searcher


def main():
    ap = argparse.ArgumentParser(description="技术文档多 agent 写作系统")
    ap.add_argument("topic", nargs="?", help="文档主题（不传则交互式询问）")
    ap.add_argument("--config", default="config.json", help="配置文件路径")
    ap.add_argument("--profile", help="统一使用的模型 profile 名")
    ap.add_argument("--orchestrator-profile", help="编排者（澄清/大纲/调研/摘要）使用的 profile")
    ap.add_argument("--writer-profile", help="章节写作 agent 使用的 profile")
    ap.add_argument("--reviewer-profile", help="审校 agent 使用的 profile（缺省同编排者）")
    ap.add_argument("--output", default="docs", help="输出根目录，默认 docs/")
    ap.add_argument("--workers", type=int, default=6, help="并行写作/审校 agent 数上限，默认 6")
    ap.add_argument("--auto", action="store_true",
                    help="非交互模式：跳过提问与大纲确认，直接生成（适合脚本/API 调用）")
    ap.add_argument("--no-research", action="store_true", help="关闭联网调研阶段")
    ap.add_argument("--research-file",
                    help="自备调研材料文件路径（如用其他工具调研后保存的 md）。"
                         "提供后直接使用该材料，完全不触发联网搜索")
    ap.add_argument("--no-review", action="store_true", help="关闭审校修订阶段")
    args = ap.parse_args()

    topic = args.topic or input("请输入文档主题：").strip()
    if not topic:
        sys.exit("主题不能为空")

    config = load_config(args.config)
    orchestrator = LLM.from_config(config, args.orchestrator_profile or args.profile)
    writer = LLM.from_config(config, args.writer_profile or args.profile)
    reviewer = (LLM.from_config(config, args.reviewer_profile)
                if args.reviewer_profile else orchestrator)
    # 自备调研材料优先：一旦提供，彻底关闭联网搜索
    research_override = None
    if args.research_file:
        from pathlib import Path
        rf = Path(args.research_file)
        if not rf.is_file():
            sys.exit("调研材料文件不存在: %s" % args.research_file)
        research_override = rf.read_text(encoding="utf-8")
        searcher = None
    else:
        searcher = None if args.no_research else create_searcher(config)

    print("编排模型：%s ｜ 写作模型：%s ｜ 审校模型：%s ｜ 调研：%s"
          % (orchestrator.model, writer.model, reviewer.model,
             "用户材料(%s)" % args.research_file if research_override is not None
             else "关闭" if args.no_research
             else (type(searcher).__name__ if searcher else "不可用（跳过）")))
    pipeline = DocPipeline(orchestrator, writer, out_root=args.output,
                           interactive=not args.auto, max_workers=args.workers,
                           searcher=searcher, reviewer=reviewer,
                           enable_research=not args.no_research,
                           enable_review=not args.no_review,
                           research_override=research_override)
    pipeline.run(topic)


if __name__ == "__main__":
    main()
