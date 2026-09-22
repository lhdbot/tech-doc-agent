"""技能库加载器：按章节主题匹配 markdown 技能片段，拼接为 prompt 注入文本。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

# 所有章节都附带的全局技能（Markdown 硬性规范）
GLOBAL_SKILL = "markdown-format.md"

# 章节关键词 -> 技能文件名列表（按此顺序拼接）
CHAPTER_SKILL_RULES = [
    (("架构", "设计", "流程"), ["architecture-diagram.md", "pseudocode.md"]),
    (("选型", "对比"), ["adr-tradeoff.md"]),
    (("容量", "性能", "SLO"), ["slo-capacity.md"]),
    (("高可用", "容灾", "稳定性"), ["ha-dr.md"]),
]


def _default_skills_dir() -> Path:
    """技能目录默认取本文件所在目录。"""
    return Path(__file__).resolve().parent


def _read_skill(skills_dir: Path, filename: str) -> str:
    """读取单个技能文件；缺失或读取失败时容错返回空串。"""
    try:
        path = skills_dir / filename
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_global_skills(skills_dir: Optional[Union[str, Path]] = None) -> str:
    """加载全局技能（markdown-format），所有章节通用。文件缺失时返回空串。"""
    directory = Path(skills_dir) if skills_dir is not None else _default_skills_dir()
    return _read_skill(directory, GLOBAL_SKILL)


def load_skills_for_chapter(chapter_title: str,
                            skills_dir: Optional[Union[str, Path]] = None) -> str:
    """按章节标题关键词匹配技能，拼接为注入文本。

    匹配规则：
    - 含 架构/设计/流程 -> architecture-diagram + pseudocode
    - 含 选型/对比     -> adr-tradeoff
    - 含 容量/性能/SLO -> slo-capacity
    - 含 高可用/容灾/稳定性 -> ha-dr
    - 所有章节都附带 markdown-format
    命中的技能按规则顺序拼接，之间用空行分隔；文件缺失的跳过，全缺返回空串。
    """
    directory = Path(skills_dir) if skills_dir is not None else _default_skills_dir()
    title = chapter_title or ""

    # 保持规则声明顺序去重收集技能文件
    files = []
    for keywords, skill_files in CHAPTER_SKILL_RULES:
        if any(kw in title for kw in keywords):
            for f in skill_files:
                if f not in files:
                    files.append(f)
    files.append(GLOBAL_SKILL)  # 全局技能始终放最后

    parts = [_read_skill(directory, f) for f in files]
    return "\n\n".join(p for p in parts if p)
