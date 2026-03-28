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
                "force_refresh": ("BOOLEAN", {"default": False, "label_on": "开启强制刷新", "label_off": "关闭强制刷新", "tooltip": "开启后将无视缓存，每次运行都强制生成新的绑定上下文"}),
            }
        }

    RETURN_TYPES = (BINDING_TYPE,)
    RETURN_NAMES = ("binding_context",)
    FUNCTION = "execute"
    CATEGORY = "❤️‍🔥心宝专用/密钥与绑定"

    def execute(self, force_refresh: bool = False) -> Tuple[dict]:
        ctx = create_binding_context()
        logger.success("已生成绑定上下文，请将其接入心宝❤Banana 节点与各增强节点")
        return (ctx,)

    @classmethod
    def IS_CHANGED(cls, force_refresh: bool = False):
        # 如果开启强制刷新，始终返回 NaN，强制 ComfyUI 每次都调用 execute
        if force_refresh:
            return float("NaN")

        # 否则使用时间桶机制：
        # 1. 在有效期窗口内（例如 12 分钟），返回值不变 -> ComfyUI 跳过此节点 -> 复用旧绑定 -> 下游生图节点也跳过（省钱）。
        # 2. 超过窗口期 -> 返回值改变 -> ComfyUI 重新运行此节点 -> 生成新绑定 -> 下游生图节点重新运行（激活新绑定）。
        # 预留 3 分钟 (180s) 的安全缓冲期，防止生图过程耗时导致中途过期。
        from banana_binding import _DEFAULT_TTL
        import time
        safety_buffer = 180
        refresh_interval = _DEFAULT_TTL - safety_buffer
        return int(time.time() // refresh_interval)




NODE_CLASS_MAPPINGS = {
    "BananaBindingGenerate": BananaBindingGenerate,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BananaBindingGenerate": "心宝❤绑定生成",
}
