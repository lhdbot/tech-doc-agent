"""OpenAI 兼容的多模型客户端。

通过 config.json 配置不同厂商的 API（base_url + api_key + model），
任何提供 OpenAI 兼容 /chat/completions 接口的服务都可以接入：
DeepSeek、Moonshot(Kimi)、通义千问、智谱、OpenAI、vLLM/Ollama 本地模型等。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from openai import OpenAI


def _expand_env(value: str) -> str:
    """把字符串里的 "${VAR}" 替换为同名环境变量的值。"""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value or "")


def load_config(path: str = "config.json") -> dict:
    p = Path(path)
    if not p.exists():
        p = Path(__file__).resolve().parent / path
    if not p.exists():
        raise FileNotFoundError(
            "找不到配置文件 config.json，请复制 config.example.json 为 config.json 并填写"
        )
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class LLM:
    """一个模型 profile 对应一个 LLM 实例，调用方式统一为 chat(system, user)。"""

    def __init__(self, profile: dict):
        api_key = _expand_env(profile.get("api_key", ""))
        if not api_key:
            raise ValueError(
                "profile 未提供有效 api_key（支持 ${ENV_VAR} 引用环境变量）: %s" % profile.get("model")
            )
        base_url = _expand_env(profile.get("base_url", "")) or None
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = profile["model"]
        self.temperature = float(profile.get("temperature", 0.7))
        self.max_tokens = profile.get("max_tokens")  # 可选

    @classmethod
    def from_config(cls, config: dict, profile_name: Optional[str] = None) -> "LLM":
        name = profile_name or config.get("default")
        profiles = config.get("profiles", {})
        if name not in profiles:
            raise KeyError(
                "config.json 中不存在 profile '%s'，可选: %s" % (name, list(profiles))
            )
        return cls(profiles[name])

    def chat(self, system: str, user: str, temperature: Optional[float] = None) -> str:
        kwargs = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self.temperature if temperature is None else temperature,
        )
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        resp = self.client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()
