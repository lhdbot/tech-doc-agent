"""审校 harness：章节审校 → 条件修订 → 跨章一致性检查。

只依赖 LLM 实例的 ``chat(system, user) -> str`` 接口，
供 pipeline 编排层在章节写作与合并阶段之间插入调用。

注意：对 pipeline 的导入必须放在函数内（延迟导入），
因为 pipeline.py 会导入本模块，顶层导入会造成循环导入。
"""
from __future__ import annotations

# ---------------- 审校 prompt 常量 ----------------

REVIEW_SYSTEM = (
    "你是一名严格的技术文档审校专家。你会收到一个章节正文、它的章节标题和全文大纲，"
    "需要按检查清单逐项审校，并只输出 JSON，不要输出任何多余文字。"
)

REVIEW_USER = """请审校以下章节。

【章节标题】
{chapter_title}

【全文大纲】
{outline_text}

【章节正文】
{content}

【检查清单】
1. 可落地性：是否包含真实可执行的命令、配置项、代码片段，而非空泛描述；
2. 事实准确性：标出可疑或明显错误的技术论断（版本号、API、行为描述等）；
3. 格式规范：markdown 标题层级、代码块语言标注、列表格式是否规范；
4. 图表正确性：若引用了图表/mermaid，标号、引用与内容是否正确；
5. 大纲一致性：内容是否覆盖该章在大纲中的要点，是否与相邻章节定位冲突。

【输出格式】
只输出如下 JSON（score 为 0-100 的整数，issues 为具体问题字符串列表，suggestions 为整体修改建议）：
{{"score": 85, "issues": ["问题1", "问题2"], "suggestions": "修改建议文字"}}
"""

REVISE_SYSTEM = (
    "你是一名技术文档写作专家。你会收到一章正文和审校意见，"
    "请根据意见修订正文：保持原有章节结构和小节划分不变，只修改被指出的问题点，"
    "不要做无关改写。直接输出修订后的章节全文。"
)

REVISE_USER = """【章节标题】
{chapter_title}

【审校意见】
评分：{score}/100
问题列表：
{issues}
修改建议：{suggestions}

【章节正文】
{content}

请输出修订后的章节全文。如使用分隔符格式，请把正文放在 ===CONTENT=== 之后。
"""

CONSISTENCY_SYSTEM = (
    "你是一名技术文档一致性审校专家。你会收到全文大纲和各章节摘要，"
    "请做跨章一致性检查并输出一份 markdown 报告。"
)

CONSISTENCY_USER = """【全文大纲】
{outline_text}

【各章节摘要】
{summaries}

请检查跨章节一致性问题，至少覆盖：
1. 术语使用是否统一（同一概念是否有多种叫法）；
2. 版本号、依赖版本在各章是否一致；
3. 各章结论或建议是否互相矛盾。

输出一份 markdown 报告：先列发现的问题（按章节引用），没有问题则明确写"未发现一致性问题"；
最后给合并阶段的处理建议。只输出报告正文。
"""

# 审校解析失败时的兜底结果
_FALLBACK_REVIEW = {
    "score": 70,
    "issues": [],
    "suggestions": "（审校解析失败，跳过分）",
}


def review_section(reviewer_llm, content: str, chapter_title: str, outline_text: str) -> dict:
    """让 reviewer 模型按检查清单审校单个章节。

    返回固定含 score/issues/suggestions 三个键的 dict；
    模型调用或 JSON 解析失败时返回兜底结果。
    """
    from pipeline import parse_json  # 延迟导入，避免循环依赖
    try:
        raw = reviewer_llm.chat(
            REVIEW_SYSTEM,
            REVIEW_USER.format(
                chapter_title=chapter_title,
                outline_text=outline_text,
                content=content,
            ),
        )
        review = parse_json(raw)
        # 归一化字段，保证返回结构稳定
        return {
            "score": int(review.get("score", 70)),
            "issues": [str(i) for i in review.get("issues", [])],
            "suggestions": str(review.get("suggestions", "")),
        }
    except Exception:
        return dict(_FALLBACK_REVIEW)


def revise_section(writer_llm, content: str, review: dict, chapter_title: str) -> str:
    """把审校意见和原文发给写作模型修订一轮，返回修订后的章节文本。"""
    issues = review.get("issues") or []
    issues_text = "\n".join("- %s" % i for i in issues) or "（无具体问题，参考整体建议）"
    raw = writer_llm.chat(
        REVISE_SYSTEM,
        REVISE_USER.format(
            chapter_title=chapter_title,
            score=review.get("score", "?"),
            issues=issues_text,
            suggestions=review.get("suggestions", ""),
            content=content,
        ),
    )
    # 模型若沿用三段式分隔符，只取 ===CONTENT=== 之后的正文（复用项目现有解析器）
    if "===CONTENT===" in raw:
        from pipeline import parse_section_output  # 延迟导入，避免循环依赖
        return parse_section_output(raw)["content"]
    return raw.strip()


def should_revise(review: dict, threshold: int = 80) -> bool:
    """判断是否需要修订：分数低于阈值，或分数低于 90 且存在具体问题。"""
    score = review.get("score", 100)
    issues = review.get("issues") or []
    if score < threshold:
        return True
    if issues and score < 90:
        return True
    return False


def consistency_review(orchestrator_llm, summaries: str, outline_text: str) -> str:
    """跨章一致性检查（术语、版本号、结论冲突），返回 markdown 报告文本。

    模型调用异常时返回空串，不阻断流水线。
    """
    try:
        return orchestrator_llm.chat(
            CONSISTENCY_SYSTEM,
            CONSISTENCY_USER.format(outline_text=outline_text, summaries=summaries),
        ).strip()
    except Exception:
        return ""
