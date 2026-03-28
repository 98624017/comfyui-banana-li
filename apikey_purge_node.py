"""
提供“心宝❤全局密钥管理”节点，用于集中填写全局 Banana/魔搭密钥，以及导出前自动清理的开关。
节点本身不修改磁盘配置，值仅用于前端扩展做清理和回填。
"""

from __future__ import annotations

from logger import logger


class XinbaoApiKeyPurge:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "banana_global_api_key": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "display": "全局 Banana API Key",
                        "tooltip": "用于缺省时回填心宝❤Banana/多模态节点，导出清理会一并清空",
                    },
                ),
                "modelscope_global_api_key": (
                    "STRING",
            {
                "default": "",
                "multiline": False,
                "display": "全局魔搭 API Key",
                "tooltip": "用于缺省时回填心宝❤魔搭文生图/多模态节点，导出清理会一并清空",
            },
                ),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("status",)
    FUNCTION = "apply"
    CATEGORY = "❤️‍🔥心宝专用/密钥与绑定"

    def apply(
        self,
        banana_global_api_key: str = "",
        modelscope_global_api_key: str = "",
    ):
        status = self._build_status(banana_global_api_key, modelscope_global_api_key)
        logger.info(status)
        return (status,)

    @staticmethod
    def _mask(value: str) -> str:
        cleaned = (value or "").strip()
        if len(cleaned) <= 6:
            return cleaned
        return f"{cleaned[:3]}***{cleaned[-3:]}"

    def _build_status(self, banana_key: str, modao_key: str) -> str:
        banana_part = self._mask(banana_key) or "未填写"
        modao_part = self._mask(modao_key) or "未填写"
        return f"密钥清理节点就绪 | Banana: {banana_part} | 魔搭: {modao_part}"


NODE_CLASS_MAPPINGS = {"XinbaoApiKeyPurge": XinbaoApiKeyPurge}
NODE_DISPLAY_NAME_MAPPINGS = {"XinbaoApiKeyPurge": "心宝❤全局密钥管理"}
