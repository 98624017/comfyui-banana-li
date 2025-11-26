"""
绑定上下文相关节点：
- 生成绑定上下文
"""

from __future__ import annotations

from typing import Tuple

from logger import logger
from banana_binding import BINDING_TYPE, create_binding_context


class BananaBindingGenerate:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "ttl_seconds": (
                    "INT",
                    {"default": 900, "min": 60, "max": 7200, "step": 30, "display": "slider"},
                ),
                "api_hint": ("STRING", {"default": "", "multiline": False, "tooltip": "可选：填写截断/模糊的渠道标识，便于排查来源"}),
            }
        }

    RETURN_TYPES = (BINDING_TYPE,)
    RETURN_NAMES = ("binding_context",)
    FUNCTION = "execute"
    CATEGORY = "❤️‍🔥心宝专用/绑定"

    def execute(self, ttl_seconds: int, api_hint: str) -> Tuple[dict]:
        ctx = create_binding_context(ttl_seconds, api_hint)
        logger.success("已生成绑定上下文，请将其接入心宝❤Banana 节点与各增强节点")
        return (ctx,)


NODE_CLASS_MAPPINGS = {
    "BananaBindingGenerate": BananaBindingGenerate,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BananaBindingGenerate": "心宝❤绑定生成",
}
