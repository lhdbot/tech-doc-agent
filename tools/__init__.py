"""tools 包：调研 harness，提供统一的网络搜索接口。

对外暴露 create_searcher(config) 工厂函数，主流程调用后若返回 None
表示调研不可用，应跳过调研阶段。
"""
from __future__ import annotations

from .web_search import Searcher, create_searcher

__all__ = ["Searcher", "create_searcher"]
