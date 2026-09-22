"""格式 lint 与自动修复模块。

提供纯确定性的 Markdown 章节质量校验（无需网络），以及借助 LLM
对问题进行自动修复的能力（修复失败或有风险时回退原文）。

对外接口：
- check_mermaid(block) -> list[str]
- lint_section(content, chapter_title) -> list[dict]
- lint_document(md) -> list[dict]
- repair_section(llm, content, issues, chapter_title) -> str
- format_report(sections_reports) -> str
"""
from __future__ import annotations

import re
from typing import List, Dict, Any

# 已知 mermaid 图类型首行白名单
_MERMAID_TYPES = (
    "graph TD", "graph LR",
    "flowchart TD", "flowchart LR",
    "sequenceDiagram",
    "classDiagram",
    "erDiagram",
    "stateDiagram-v2",
    "gantt",
    "pie",
)

# 标题层级正文匹配（行首 #）
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _extract_code_blocks(content: str):
    """按 ``` 围栏切分出所有代码块，返回 [(lang, body), ...]。

    只处理恰好三个反引号构成的围栏；围栏数量为奇数时，最后一个
    未闭合围栏的内容仍会作为一个块返回（便于对其做语法检查），
    围栏闭合问题本身由 lint_section 单独报告。
    """
    lines = content.splitlines()
    blocks = []
    in_block = False
    lang = ""
    buf = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") and not in_block:
            in_block = True
            lang = stripped[3:].strip()
            buf = []
        elif stripped.startswith("```") and in_block:
            in_block = False
            blocks.append((lang, "\n".join(buf)))
        elif in_block:
            buf.append(line)
    # 末尾未闭合的块也纳入检查，避免漏掉其中的语法问题
    if in_block:
        blocks.append((lang, "\n".join(buf)))
    return blocks


def check_mermaid(block: str) -> List[str]:
    """校验单个 mermaid 代码块内容，返回问题描述列表（空列表=通过）。

    检查项：
    1. 首行必须是已知图类型；
    2. 箭头语法 --> 两侧非空；
    3. 方括号 [] 与圆括号 () 全局配平。
    """
    issues = []
    # 过滤空行后取首个非空行作为图类型声明
    nonempty = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if not nonempty:
        issues.append("mermaid 代码块为空")
        return issues

    first = nonempty[0]
    if not any(first.startswith(t) for t in _MERMAID_TYPES):
        issues.append(
            "首行不是已知图类型（应为 graph TD/LR、flowchart TD/LR、"
            "sequenceDiagram、classDiagram、erDiagram、stateDiagram-v2、"
            "gantt、pie 之一），实际为: %s" % first
        )

    # 箭头语法：--> 两侧必须非空（忽略注释行 %%）
    for i, line in enumerate(block.splitlines(), start=1):
        code = line.split("%%", 1)[0]
        idx = code.find("-->")
        while idx != -1:
            left = code[:idx].strip()
            right = code[idx + 3:].strip()
            if not left:
                issues.append("第 %d 行：箭头 '-->' 左侧为空" % i)
            if not right:
                issues.append("第 %d 行：箭头 '-->' 右侧为空" % i)
            idx = code.find("-->", idx + 3)

    # 括号配平（[] 与 () 各自计数配平）
    for open_ch, close_ch, name in (("[", "]", "方括号"), ("(", ")", "圆括号")):
        if block.count(open_ch) != block.count(close_ch):
            issues.append(
                "%s不配平：'%s' 出现 %d 次，'%s' 出现 %d 次"
                % (name, open_ch, block.count(open_ch),
                   close_ch, block.count(close_ch))
            )
    return issues


def _check_fence_closed(content: str) -> List[Dict[str, str]]:
    """规则1：``` 围栏数量必须为偶数（闭合）。"""
    count = sum(
        1 for ln in content.splitlines() if ln.strip().startswith("```")
    )
    if count % 2 != 0:
        return [{
            "rule": "fence-closed",
            "message": "检测到 %d 个 ``` 围栏标记，数量为奇数，存在未闭合的代码块" % count,
            "severity": "error",
        }]
    return []


def _check_heading_levels(content: str) -> List[Dict[str, str]]:
    """规则3：标题层级连续，不得从 ## 直接跳到 ####（跨级）。"""
    issues = []
    prev = 0
    for m in _HEADING_RE.finditer(content):
        level = len(m.group(1))
        title = m.group(2)
        if prev > 0 and level > prev + 1:
            issues.append({
                "rule": "heading-level",
                "message": "标题层级跳跃：从 %s 级直接跳到 %s 级（标题：%s）"
                           % ("#" * prev, "#" * level, title),
                "severity": "error",
            })
        prev = max(prev, level) if prev == 0 else level
    return issues


def lint_section(content: str, chapter_title: str) -> List[Dict[str, Any]]:
    """对单个章节做格式 lint，返回问题字典列表。

    每项格式为 {"rule": str, "message": str, "severity": "error"|"warning"}。
    """
    issues: List[Dict[str, Any]] = []

    # 规则1：围栏闭合
    issues.extend(_check_fence_closed(content))

    # 提取代码块（供 mermaid / pseudo 检查使用）
    blocks = _extract_code_blocks(content)
    mermaid_blocks = [b for lang, b in blocks if lang == "mermaid"]
    pseudo_blocks = [b for lang, b in blocks if lang == "pseudo"]

    # 规则2：每个 ```mermaid 块跑 check_mermaid
    for idx, block in enumerate(mermaid_blocks, start=1):
        for problem in check_mermaid(block):
            issues.append({
                "rule": "mermaid-syntax",
                "message": "第 %d 个 mermaid 图：%s" % (idx, problem),
                "severity": "error",
            })

    # 规则3：标题层级连续
    issues.extend(_check_heading_levels(content))

    # 规则4：章节必须含至少一个 ## 标题
    if not re.search(r"^##\s+", content, re.MULTILINE):
        issues.append({
            "rule": "heading-required",
            "message": "章节缺少 ## 级标题",
            "severity": "error",
        })

    # 规则5：标题含 架构/流程 的章节必须含至少一个 mermaid 块
    if ("架构" in chapter_title or "流程" in chapter_title) and not mermaid_blocks:
        issues.append({
            "rule": "mermaid-required",
            "message": "章节标题含「架构/流程」但缺少 ```mermaid 图",
            "severity": "error",
        })

    # 规则6：标题含 设计/实现 的章节必须含 ```pseudo 块
    if ("设计" in chapter_title or "实现" in chapter_title) and not pseudo_blocks:
        issues.append({
            "rule": "pseudo-required",
            "message": "章节标题含「设计/实现」但缺少 ```pseudo 伪代码块",
            "severity": "error",
        })

    # 规则7：全文超过 300 字但无任何 [来源: ...] 引用，记 warning
    if len(content) > 300 and "[来源:" not in content:
        issues.append({
            "rule": "citation-missing",
            "message": "章节超过 300 字但没有任何 [来源: ...] 引用",
            "severity": "warning",
        })

    return issues


def lint_document(md: str) -> List[Dict[str, Any]]:
    """对合并后的全文做围栏闭合与标题层级检查。"""
    issues: List[Dict[str, Any]] = []
    issues.extend(_check_fence_closed(md))
    issues.extend(_check_heading_levels(md))
    return issues


def repair_section(llm, content: str, issues: List[Dict[str, Any]],
                   chapter_title: str) -> str:
    """借助 LLM 修复章节中的格式问题，返回修复后的章节文本。

    策略：
    - 把问题清单和原文发给 llm.chat，要求只修复问题、保持内容不变、
      返回完整的修复后章节；
    - 从返回中提取 markdown（若含 ===CONTENT=== 分隔符则取其后部分）；
    - 修复结果再跑一次 lint_section，若仍有 error 级问题则回退返回原文
      （绝不返回比原文更差的版本）。
    """
    if not issues:
        return content

    problem_lines = "\n".join(
        "- [%s] %s: %s" % (it.get("severity", ""), it.get("rule", ""), it.get("message", ""))
        for it in issues
    )
    system = (
        "你是技术文档格式修复助手。你的唯一职责是修复用户给出的 Markdown "
        "章节中列出的格式问题（如未闭合的代码围栏、非法 mermaid 语法、缺失的"
        " ```mermaid/```pseudo 代码块、标题层级跳跃等）。"
        "要求：1) 只做格式修复，绝不改动正文内容、观点和措辞；"
        "2) 返回完整的修复后章节，不要只返回片段；"
        "3) 直接输出 Markdown 正文，不要任何解释性文字。"
    )
    user = (
        "章节标题：%s\n\n"
        "检测到以下格式问题：\n%s\n\n"
        "原始章节全文如下，请修复后完整返回：\n\n%s"
        % (chapter_title, problem_lines, content)
    )

    repaired = llm.chat(system, user)

    # 提取 markdown：若返回含 ===CONTENT=== 分隔符，取其后部分
    if "===CONTENT===" in repaired:
        repaired = repaired.split("===CONTENT===", 1)[1].strip()

    # 修复后复检：仍有 error 则回退原文
    remaining = lint_section(repaired, chapter_title)
    if any(it["severity"] == "error" for it in remaining):
        return content
    return repaired


def format_report(sections_reports: List[Dict[str, Any]]) -> str:
    """把各章节的 lint 结果汇总成人类可读的 Markdown 质量报告。

    sections_reports 中每项预期形如：
        {"title": 章节标题, "issues": lint_section 返回的问题列表}
    若某项缺少 "title" 键，则以"章节 N"占位。
    """
    lines = ["# 文档格式质量报告", ""]
    total_error = 0
    total_warning = 0

    for idx, report in enumerate(sections_reports, start=1):
        title = report.get("title") or "章节 %d" % idx
        issues = report.get("issues", [])
        errors = [it for it in issues if it.get("severity") == "error"]
        warnings = [it for it in issues if it.get("severity") == "warning"]
        total_error += len(errors)
        total_warning += len(warnings)

        if not issues:
            lines.append("## %s" % title)
            lines.append("")
            lines.append("✅ 未发现问题")
            lines.append("")
            continue

        lines.append("## %s（错误 %d / 警告 %d）" % (title, len(errors), len(warnings)))
        lines.append("")
        for it in issues:
            icon = "❌" if it.get("severity") == "error" else "⚠️"
            lines.append(
                "- %s **%s** (%s): %s"
                % (icon, it.get("severity", ""), it.get("rule", ""), it.get("message", ""))
            )
        lines.append("")

    lines.insert(2, "共 %d 个章节：错误 %d 个，警告 %d 个"
                 % (len(sections_reports), total_error, total_warning))
    lines.insert(3, "")
    return "\n".join(lines).rstrip() + "\n"
