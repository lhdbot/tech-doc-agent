"""网络搜索后端封装（调研 harness）。

统一接口：
    searcher = create_searcher(config)   # 可能返回 None
    results = searcher.search(query, max_results=8)
    # results: [{"title": str, "url": str, "snippet": str}, ...]

支持两种后端：
- duckduckgo（默认）：使用 ddgs 包，无需 API key；
- tavily：需要 search.backend == "tavily" 且配置了 api_key，
  直接用 urllib 发 HTTP 请求，不引入新依赖。

设计原则：任何配置缺失、依赖未安装、网络异常都不向外抛异常，
create_searcher 返回 None 或 search 返回空列表，由调用方决定跳过调研。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from llm import _expand_env

# Tavily 搜索接口地址与超时时间
TAVILY_API_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT = 20


class Searcher:
    """搜索器基类，定义统一接口。

    子类实现 _do_search；search 对外兜底所有异常，保证永不抛出。
    """

    def search(self, query: str, max_results: int = 8) -> List[Dict[str, str]]:
        """执行搜索，返回 [{"title", "url", "snippet"}] 列表；失败时返回空列表。"""
        try:
            return self._do_search(query, max_results)
        except Exception:
            # 网络/解析等一切异常都吞掉，调研失败不应中断主流程
            return []

    def _do_search(self, query: str, max_results: int) -> List[Dict[str, str]]:
        """子类实现真正的搜索逻辑。"""
        raise NotImplementedError

    @staticmethod
    def _normalize(title: str, url: str, snippet: str) -> Dict[str, str]:
        """把任意来源的字段规整为统一的字符串格式。"""
        return {
            "title": (title or "").strip(),
            "url": (url or "").strip(),
            "snippet": (snippet or "").strip(),
        }


class DuckDuckGoSearcher(Searcher):
    """DuckDuckGo 后端，依赖 ddgs 包，免 API key。"""

    def __init__(self) -> None:
        # 延迟到实例化时导入，import 失败由工厂函数兜底
        from ddgs import DDGS

        self._ddgs = DDGS()

    def _do_search(self, query: str, max_results: int) -> List[Dict[str, str]]:
        raw = self._ddgs.text(query, max_results=max_results) or []
        results = []
        for item in raw:
            results.append(
                self._normalize(
                    item.get("title", ""),
                    item.get("href", ""),
                    item.get("body", ""),
                )
            )
        return results


class TavilySearcher(Searcher):
    """Tavily 后端，用 urllib 直接调用 REST API，不新增依赖。"""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _do_search(self, query: str, max_results: int) -> List[Dict[str, str]]:
        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
        }
        req = urllib.request.Request(
            TAVILY_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TAVILY_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = []
        for item in data.get("results", []):
            results.append(
                self._normalize(
                    item.get("title", ""),
                    item.get("url", ""),
                    item.get("content", ""),
                )
            )
        return results


def create_searcher(config: dict) -> Optional[Searcher]:
    """根据配置创建搜索器；配置缺失或后端不可用时返回 None，绝不抛异常。

    配置格式（config["search"]）：
        {
            "backend": "duckduckgo",                # 缺省为 duckduckgo
            "tavily_api_key": "${TAVILY_API_KEY}"   # 仅 tavily 后端需要
        }
    """
    try:
        if not isinstance(config, dict):
            return None
        search_cfg = config.get("search", {}) or {}
        backend = (search_cfg.get("backend") or "duckduckgo").strip().lower()

        if backend == "tavily":
            # Tavily 需要 api_key，支持 ${ENV_VAR} 展开
            api_key = _expand_env(search_cfg.get("tavily_api_key", ""))
            if not api_key:
                return None
            return TavilySearcher(api_key)

        if backend == "duckduckgo":
            try:
                return DuckDuckGoSearcher()
            except ImportError:
                # ddgs 未安装，视为后端不可用
                return None

        # 未知后端：静默降级为不可用
        return None
    except Exception:
        return None
