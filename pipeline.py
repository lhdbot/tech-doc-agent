"""写作流水线编排：调研 → 澄清 → 大纲（确认）→ 并行撰写 → 审校修订 → 格式校验 → 合并交付。"""
from __future__ import annotations

import datetime
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import format as fmt
import prompts
import review as reviewer_mod
from skills.loader import load_skills_for_chapter


def parse_json(text: str):
    """从模型输出中提取 JSON（容忍 ```json 代码块和前后多余文字）。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i != -1], default=-1)
    if start == -1:
        raise ValueError("模型输出中未找到 JSON：\n" + text[:500])
    text = text[start:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 截到最后一个 } 或 ] 再试一次（模型偶尔在 JSON 后追加解释）
        end = max(text.rfind("}"), text.rfind("]"))
        if end != -1:
            return json.loads(text[: end + 1])
        raise


def parse_section_output(text: str) -> dict:
    """解析章节写作 agent 的三段式输出。"""
    def _between(marker: str, rest: str) -> str:
        if marker not in rest:
            return ""
        part = rest.split(marker, 1)[1]
        for nxt in ("===CONTENT===", "===SUMMARY===", "===OPEN_QUESTIONS==="):
            if nxt != marker and nxt in part:
                part = part.split(nxt, 1)[0]
        return part.strip()

    content = _between("===CONTENT===", text)
    if not content:  # 模型没按格式输出时，全文当作内容
        content = text.strip()
    return {
        "content": content,
        "summary": _between("===SUMMARY===", text),
        "open_questions": _between("===OPEN_QUESTIONS===", text),
    }


def _slug(text: str, maxlen: int = 30) -> str:
    s = re.sub(r"[^\w一-鿿-]+", "-", text).strip("-").lower()
    return s[:maxlen] or "doc"


def _outline_text(outline: dict) -> str:
    lines = []
    for i, ch in enumerate(outline["chapters"], 1):
        lines.append("%d. %s（要点：%s）" % (i, ch["title"], "；".join(ch.get("points", []))))
    return "\n".join(lines)


class DocPipeline:
    def __init__(self, orchestrator, writer, out_root: str = "docs",
                 interactive: bool = True, max_workers: int = 6,
                 searcher=None, reviewer=None,
                 enable_research: bool = True, enable_review: bool = True,
                 research_override: Optional[str] = None):
        self.orchestrator = orchestrator  # 负责澄清/大纲/调研蒸馏/摘要
        self.writer = writer              # 负责章节撰写（可与编排者不同模型）
        self.reviewer = reviewer or orchestrator  # 负责章节审校
        self.searcher = searcher          # tools.web_search.Searcher，None 时跳过调研
        self.enable_research = enable_research
        self.enable_review = enable_review
        # 用户自备的调研材料（如用其他工具调研后保存的 md 文件内容）；
        # 一旦提供，调研阶段直接使用它，完全不触发联网搜索
        self.research_override = research_override
        self.out_root = Path(out_root)
        self.interactive = interactive
        self.max_workers = max_workers

    # ---------- 阶段 0：联网调研 ----------
    def research(self, topic: str, answers: str, out_dir: Path) -> str:
        if not self.enable_research or self.searcher is None:
            return ""
        print("\n=== 联网调研 ===")
        try:
            raw = self.orchestrator.chat(
                prompts.RESEARCH_SYSTEM,
                prompts.RESEARCH_USER.format(topic=topic, answers=answers))
            queries = parse_json(raw)
            if not isinstance(queries, list):
                raise ValueError("查询词不是数组")
        except Exception as e:
            print("  查询词生成失败（%s），改用主题本身搜索" % e)
            queries = [topic]
        queries = [str(q) for q in queries][:5] or [topic]

        seen, results = set(), []
        for q in queries:
            hits = self.searcher.search(q, max_results=5)
            print("  搜索「%s」-> %d 条" % (q, len(hits)))
            for h in hits:
                url = h.get("url", "")
                if url and url not in seen:
                    seen.add(url)
                    results.append(h)
            if len(results) >= 15:
                break
        if not results:
            print("  未获得搜索结果，跳过调研注入")
            return ""

        results_text = "\n".join(
            "- %s\n  %s\n  %s" % (r.get("title", ""), r.get("url", ""), r.get("snippet", ""))
            for r in results[:15])
        notes = self.orchestrator.chat(
            prompts.RESEARCH_DISTILL_SYSTEM,
            prompts.RESEARCH_DISTILL_USER.format(topic=topic, results=results_text))
        (out_dir / "research.md").write_text(
            "# 调研笔记\n\n%s\n" % notes, encoding="utf-8")
        print("  调研完成：%d 条结果蒸馏为 research.md" % len(results))
        return notes

    # ---------- 阶段 1：需求澄清 ----------
    def clarify(self, topic: str) -> str:
        if not self.interactive:
            return "（非交互模式：按常见情况合理假设）"
        raw = self.orchestrator.chat(
            prompts.CLARIFY_SYSTEM, prompts.CLARIFY_USER.format(topic=topic))
        try:
            questions = parse_json(raw)
        except ValueError:
            print("（澄清问题解析失败，跳过提问）")
            return "（用户未提供澄清回答）"
        answers = []
        print("\n=== 需求澄清 ===")
        for i, q in enumerate(questions, 1):
            print("\nQ%d. %s" % (i, q.get("question", "")))
            for j, opt in enumerate(q.get("options", []), 1):
                print("    %d) %s" % (j, opt))
            ans = input("   你的回答（输入序号或直接作答，回车跳过）：").strip()
            opts = q.get("options", [])
            if ans.isdigit() and 1 <= int(ans) <= len(opts):
                ans = opts[int(ans) - 1]
            answers.append("Q: %s\nA: %s" % (q.get("question", ""), ans or "（未回答，由你合理假设）"))
        return "\n".join(answers) or "（用户未提供澄清回答）"

    # ---------- 阶段 2：大纲 + 用户确认 ----------
    def outline(self, topic: str, answers: str, research: str) -> dict:
        previous = None
        while True:
            if previous is None:
                user = prompts.OUTLINE_USER.format(
                    topic=topic, answers=answers, research=research)
            else:
                user = prompts.OUTLINE_REVISE_USER.format(
                    topic=topic, answers=answers, research=research,
                    previous_outline=json.dumps(previous, ensure_ascii=False, indent=2),
                    feedback=feedback)
            outline = parse_json(self.orchestrator.chat(prompts.OUTLINE_SYSTEM, user))
            if "chapters" not in outline or not outline["chapters"]:
                raise ValueError("大纲格式不正确：缺少 chapters")
            if not self.interactive:
                return outline

            print("\n=== 文档大纲：《%s》===" % outline.get("doc_title", topic))
            for i, ch in enumerate(outline["chapters"], 1):
                print("\n%d. %s  （约 %s 字）" % (i, ch["title"], ch.get("words", "?")))
                for p in ch.get("points", []):
                    print("   - %s" % p)
            choice = input("\n确认大纲？[y=开始写作 / 直接输入修改意见]：").strip()
            if choice.lower() in ("y", "yes", ""):
                return outline
            feedback, previous = choice, outline

    # ---------- 阶段 3：并行撰写各章节 ----------
    def write_sections(self, topic: str, answers: str, research: str,
                       outline: dict, out_dir: Path) -> list:
        chapters = outline["chapters"]
        otext = _outline_text(outline)
        results = [None] * len(chapters)

        def _write_one(i: int, ch: dict) -> dict:
            skills = load_skills_for_chapter(ch["title"])
            raw = self.writer.chat(
                prompts.SECTION_SYSTEM,
                prompts.SECTION_USER.format(
                    topic=topic, answers=answers, research=research,
                    outline_text=otext, skills=skills,
                    index=i, title=ch["title"],
                    points="；".join(ch.get("points", [])), words=ch.get("words", 800)))
            sec = parse_section_output(raw)
            fname = "%02d-%s.md" % (i, _slug(ch["title"]))
            (out_dir / fname).write_text(sec["content"] + "\n", encoding="utf-8")
            return {"index": i, "title": ch["title"], "file": fname,
                    "summary": sec["summary"], "open_questions": sec["open_questions"],
                    "content": sec["content"]}

        workers = max(1, min(self.max_workers, len(chapters)))
        print("\n=== 并行撰写 %d 个章节（%d 个写作 agent）===" % (len(chapters), workers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_write_one, i, ch): i for i, ch in enumerate(chapters, 1)}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    sec = fut.result()
                    results[i - 1] = sec
                    print("  [完成] 第 %d 章《%s》 -> %s（%d 字）"
                          % (i, sec["title"], sec["file"], len(sec["content"])))
                except Exception as e:  # 单章失败不拖垮整体，标记后由合并阶段暴露
                    print("  [失败] 第 %d 章：%s" % (i, e))
                    results[i - 1] = {"index": i, "title": chapters[i - 1]["title"],
                                      "file": None, "summary": "", "open_questions": "",
                                      "error": str(e), "content": ""}
        return results

    # ---------- 阶段 3.5：审校修订 + 格式校验（并行）----------
    def polish_sections(self, outline: dict, sections: list, out_dir: Path) -> list:
        otext = _outline_text(outline)
        targets = [s for s in sections if not s.get("error")]
        if not targets:
            return sections

        def _polish_one(sec: dict) -> dict:
            content, title = sec["content"], sec["title"]
            # 1) reviewer agent 审校，分数不达标则写作模型修订一轮
            if self.enable_review:
                rep = reviewer_mod.review_section(self.reviewer, content, title, otext)
                sec["review_score"] = rep.get("score")
                sec["review_issues"] = rep.get("issues", [])
                if reviewer_mod.should_revise(rep):
                    content = reviewer_mod.revise_section(self.writer, content, rep, title)
                    sec["revised"] = True
            # 2) 格式 lint，有 error 时让 LLM 修复一次（修复后仍有 error 自动回退原文）
            issues = fmt.lint_section(content, title)
            if any(i["severity"] == "error" for i in issues):
                fixed = fmt.repair_section(self.orchestrator, content, issues, title)
                if fixed != content:
                    sec["format_repaired"] = True
                    content = fixed
                issues = fmt.lint_section(content, title)
            sec["lint_issues"] = issues
            sec["content"] = content
            if sec.get("file"):
                (out_dir / sec["file"]).write_text(content + "\n", encoding="utf-8")
            return sec

        print("\n=== 审校与格式校验 %d 个章节 ===" % len(targets))
        workers = max(1, min(self.max_workers, len(targets)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_polish_one, s): s["index"] for s in targets}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    sec = fut.result()
                    sections[i - 1] = sec
                    score = sec.get("review_score", "-")
                    n_err = sum(1 for x in sec.get("lint_issues", []) if x["severity"] == "error")
                    flags = []
                    if sec.get("revised"):
                        flags.append("已修订")
                    if sec.get("format_repaired"):
                        flags.append("已修复格式")
                    print("  [完成] 第 %d 章《%s》 评分=%s 格式error=%d %s"
                          % (i, sec["title"], score, n_err, " ".join(flags)))
                except Exception as e:
                    print("  [失败] 第 %d 章审校异常：%s（保留原稿）" % (i, e))
        return sections

    # ---------- 阶段 4：合并与交付 ----------
    def assemble(self, topic: str, answers: str, research: str,
                 outline: dict, sections: list, out_dir: Path) -> Path:
        summaries = "\n".join(
            "第 %s 章《%s》：%s" % (s["index"], s["title"], s["summary"] or "（撰写失败）")
            for s in sections)
        open_qs = "\n".join(
            "第 %s 章：%s" % (s["index"], s["open_questions"])
            for s in sections if s.get("open_questions") and s["open_questions"] != "无")
        raw = self.orchestrator.chat(
            prompts.SUMMARY_SYSTEM,
            prompts.SUMMARY_USER.format(
                doc_title=outline.get("doc_title", topic), topic=topic, answers=answers,
                research=research,
                outline_text=_outline_text(outline), summaries=summaries,
                open_questions=open_qs or "无"))
        abstract = raw.split("===ABSTRACT===")[-1].split("===CONCLUSION===")[0].strip() \
            if "===ABSTRACT===" in raw else ""
        conclusion = raw.split("===CONCLUSION===")[-1].strip() if "===CONCLUSION===" in raw else ""

        # 跨章一致性检查
        consistency = ""
        if self.enable_review:
            consistency = reviewer_mod.consistency_review(
                self.orchestrator, summaries, _outline_text(outline))
            if consistency:
                (out_dir / "consistency-report.md").write_text(
                    "# 跨章一致性检查\n\n%s\n" % consistency, encoding="utf-8")

        today = datetime.date.today().isoformat()
        toc = "\n".join("- [%s. %s](#%s)" % (s["index"], s["title"], s["file"] or "")
                        for s in sections)
        parts = [
            "# %s\n" % outline.get("doc_title", topic),
            "> 生成时间：%s ｜ 主题：%s\n" % (today, topic),
            "## 摘要\n\n%s\n" % abstract,
            "## 目录\n\n%s\n" % toc,
        ]
        for s in sections:
            if s.get("error"):
                parts.append("## %s. %s\n\n> ⚠️ 本章生成失败：%s\n" % (s["index"], s["title"], s["error"]))
            else:
                parts.append(s["content"].rstrip() + "\n")
        if conclusion:
            parts.append(conclusion if conclusion.startswith("## ") else "## 结论与风险\n\n" + conclusion)

        final_text = "\n".join(parts) + "\n"
        # 全文格式校验，问题写入质量报告（不改动已审校过的正文）
        doc_issues = fmt.lint_document(final_text)
        reports = [{"title": s["title"], "issues": s.get("lint_issues", [])}
                   for s in sections if not s.get("error")]
        report_md = "# 质量报告\n\n" + fmt.format_report(reports)
        if doc_issues:
            report_md += "\n## 全文级问题\n\n" + "\n".join(
                "- [%s] %s" % (i["severity"], i["message"]) for i in doc_issues)
        if consistency:
            report_md += "\n## 跨章一致性检查\n\n" + consistency
        (out_dir / "quality-report.md").write_text(report_md + "\n", encoding="utf-8")

        final = out_dir / "README.md"
        final.write_text(final_text, encoding="utf-8")
        (out_dir / "outline.json").write_text(
            json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")
        return final

    # ---------- 总入口 ----------
    def run(self, topic: str) -> Path:
        out_dir = self.out_root / ("%s-%s" % (_slug(topic), datetime.date.today().isoformat()))
        out_dir.mkdir(parents=True, exist_ok=True)
        answers = self.clarify(topic)
        if self.research_override is not None:
            # 用户自备调研材料：直接使用，不触发任何联网搜索
            research = self.research_override.strip()
            if research:
                (out_dir / "research.md").write_text(
                    "# 调研笔记（用户提供）\n\n%s\n" % research, encoding="utf-8")
                print("\n=== 使用用户提供的调研材料（跳过联网搜索）===")
        else:
            research = self.research(topic, answers, out_dir)
        outline = self.outline(topic, answers, research)
        sections = self.write_sections(topic, answers, research, outline, out_dir)
        sections = self.polish_sections(outline, sections, out_dir)
        final = self.assemble(topic, answers, research, outline, sections, out_dir)
        failed = [s for s in sections if s.get("error")]
        print("\n=== 交付 ===")
        print("最终文档：%s" % final)
        print("章节文件：%d 个（失败 %d 个），目录：%s" % (len(sections), len(failed), out_dir))
        print("质量报告：%s" % (out_dir / "quality-report.md"))
        if failed:
            print("失败章节：%s（可重跑或手动补写）" % "、".join("第 %s 章" % s["index"] for s in failed))
        return final
