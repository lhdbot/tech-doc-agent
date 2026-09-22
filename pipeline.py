"""写作流水线编排：澄清 → 大纲（确认）→ 并行分派章节写作 → 合并交付。"""
from __future__ import annotations

import datetime
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import prompts


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
                 interactive: bool = True, max_workers: int = 6):
        self.orchestrator = orchestrator  # 负责澄清/大纲/摘要
        self.writer = writer              # 负责章节撰写（可与编排者不同模型）
        self.out_root = Path(out_root)
        self.interactive = interactive
        self.max_workers = max_workers

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
    def outline(self, topic: str, answers: str) -> dict:
        previous = None
        while True:
            if previous is None:
                user = prompts.OUTLINE_USER.format(topic=topic, answers=answers)
            else:
                user = prompts.OUTLINE_REVISE_USER.format(
                    topic=topic, answers=answers,
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
    def write_sections(self, topic: str, answers: str, outline: dict, out_dir: Path) -> list:
        chapters = outline["chapters"]
        otext = _outline_text(outline)
        results = [None] * len(chapters)

        def _write_one(i: int, ch: dict) -> dict:
            raw = self.writer.chat(
                prompts.SECTION_SYSTEM,
                prompts.SECTION_USER.format(
                    topic=topic, answers=answers, outline_text=otext,
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

    # ---------- 阶段 4：合并与交付 ----------
    def assemble(self, topic: str, answers: str, outline: dict, sections: list, out_dir: Path) -> Path:
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
                outline_text=_outline_text(outline), summaries=summaries,
                open_questions=open_qs or "无"))
        abstract = raw.split("===ABSTRACT===")[-1].split("===CONCLUSION===")[0].strip() \
            if "===ABSTRACT===" in raw else ""
        conclusion = raw.split("===CONCLUSION===")[-1].strip() if "===CONCLUSION===" in raw else ""

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
        final = out_dir / "README.md"
        final.write_text("\n".join(parts) + "\n", encoding="utf-8")
        (out_dir / "outline.json").write_text(
            json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")
        return final

    # ---------- 总入口 ----------
    def run(self, topic: str) -> Path:
        answers = self.clarify(topic)
        outline = self.outline(topic, answers)
        out_dir = self.out_root / ("%s-%s" % (_slug(topic), datetime.date.today().isoformat()))
        out_dir.mkdir(parents=True, exist_ok=True)
        sections = self.write_sections(topic, answers, outline, out_dir)
        final = self.assemble(topic, answers, outline, sections, out_dir)
        failed = [s for s in sections if s.get("error")]
        print("\n=== 交付 ===")
        print("最终文档：%s" % final)
        print("章节文件：%d 个（失败 %d 个），目录：%s" % (len(sections), len(failed), out_dir))
        if failed:
            print("失败章节：%s（可重跑或手动补写）" % "、".join("第 %s 章" % s["index"] for s in failed))
        return final
