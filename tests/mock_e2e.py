"""离线 mock 端到端测试：用 FakeLLM/FakeSearcher 替代真实 API，
验证完整流水线（调研→大纲→并行写作→审校→格式校验→合并）的机制正确性。

运行：python tests/mock_e2e.py
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import format as fmt
from pipeline import DocPipeline


MERMAID_BLOCK = """```mermaid
graph TD
    A["接入层 LB"] --> B["服务层 App"]
    B --> C["存储层 DB"]
```"""

PSEUDO_BLOCK = """```pseudo
PROCEDURE HandleRequest(req)
    INPUT: req 请求
    OUTPUT: resp 响应
    resp <- route(req)
    RETURN resp
END PROCEDURE
```"""


class FakeLLM:
    """按 system prompt 关键词分流，返回各类固定响应。"""

    def __init__(self):
        self.calls = []

    def chat(self, system: str, user: str, temperature=None) -> str:
        self.calls.append(system[:20])
        if "搜索引擎查询词" in system:
            return json.dumps(["k8s 高可用 最佳实践", "kubernetes HA architecture"])
        if "蒸馏" in system:
            return "主流做法是控制平面三副本部署 [来源: https://example.com/ha]。"
        if "一致性审校" in system:
            return "未发现一致性问题。"
        if "审校专家" in system:  # 先于"一个章节"判断：REVIEW_SYSTEM 也含"一个章节"字样
            return '{"score": 92, "issues": [], "suggestions": "无需修改"}'
        if "根据意见修订正文" in system:
            # 修订：直接回显【章节正文】部分，模拟按意见修订后返回
            content = user.split("【章节正文】", 1)[1].split("请输出修订后", 1)[0]
            return "===CONTENT===\n" + content.strip()
        if "设计文档大纲" in system or ("修订" in user[:200] and "大纲" in system):
            return json.dumps({
                "doc_title": "测试主题技术方案",
                "chapters": [
                    {"title": "背景与目标", "points": ["背景", "目标"], "words": 400},
                    {"title": "总体架构", "points": ["分层架构"], "words": 600},
                    {"title": "详细设计", "points": ["核心逻辑"], "words": 600},
                    {"title": "风险与未决项", "points": ["风险"], "words": 300},
                ]}, ensure_ascii=False)
        if "一个章节" in system:
            m = re.search(r"第 (\d+) 章：(.+)", user)
            idx, title = m.group(1), m.group(2).strip()
            body = "## %s. %s\n\n### 小节\n\n本章内容，控制平面三副本 [来源: https://example.com/ha]。\n\n" % (idx, title)
            if "架构" in title or "流程" in title:
                body += MERMAID_BLOCK + "\n\n上图展示了分层调用关系。\n"
            if "设计" in title or "实现" in title:
                body += PSEUDO_BLOCK + "\n"
            return "===CONTENT===\n%s\n===SUMMARY===\n本章讲了%s。\n===OPEN_QUESTIONS===\n无" % (body, title)
        if "摘要与结论" in system:
            return "===ABSTRACT===\n这是摘要。\n===CONCLUSION===\n## 结论与风险\n\n结论内容。"
        raise AssertionError("FakeLLM 收到未预期的 prompt: " + system[:50])


class FakeSearcher:
    def search(self, query, max_results=8):
        return [{"title": "HA 实践", "url": "https://example.com/ha",
                 "snippet": "控制平面三副本"}]


def main():
    out_root = Path(tempfile.mkdtemp(prefix="techdoc-mock-"))
    try:
        llm = FakeLLM()
        pipe = DocPipeline(llm, llm, out_root=str(out_root), interactive=False,
                           searcher=FakeSearcher(), enable_research=True,
                           enable_review=True)
        final = pipe.run("测试主题")

        assert final.exists(), "README.md 未生成"
        text = final.read_text(encoding="utf-8")
        for needle in ["# 测试主题技术方案", "## 摘要", "这是摘要", "## 目录",
                       "## 1. 背景与目标", "## 2. 总体架构", "## 3. 详细设计",
                       "## 4. 风险与未决项", "## 结论与风险", "```mermaid", "```pseudo",
                       "[来源: https://example.com/ha]"]:
            assert needle in text, "最终文档缺少：%s" % needle

        out_dir = final.parent
        assert (out_dir / "research.md").exists(), "research.md 未生成"
        assert (out_dir / "outline.json").exists(), "outline.json 未生成"
        assert (out_dir / "quality-report.md").exists(), "quality-report.md 未生成"
        assert (out_dir / "consistency-report.md").exists(), "consistency-report.md 未生成"
        chapters = sorted(out_dir.glob("0*-*.md"))
        assert len(chapters) == 4, "章节文件数不对: %s" % chapters

        errors = [i for i in fmt.lint_document(text) if i["severity"] == "error"]
        assert not errors, "最终文档存在格式 error: %s" % errors

        review_calls = sum(1 for c in llm.calls if "严格的技术文档审校" in c)
        assert review_calls == 4, "审校环节调用次数不对: %d" % review_calls

        print("MOCK E2E PASSED ->", final)
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


if __name__ == "__main__":
    main()
