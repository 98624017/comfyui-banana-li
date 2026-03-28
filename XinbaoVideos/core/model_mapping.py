"""
模型映射与请求体构建（从原 `Xinbao_Video_Generator.py` 迁移）。

保持签名与行为不变，避免影响工作流与节点 UI 默认值兼容。
"""

from __future__ import annotations

import re
from typing import Dict, Optional


def _normalize_aspect_ratio(aspect_ratio: str) -> str:
    # 映射表：支持中文和旧版英文
    aspect_map = {
        "横版": "landscape",
        "竖版": "portrait",
        "landscape": "landscape",
        "portrait": "portrait",
    }
    raw_aspect = (aspect_ratio or "").strip()
    aspect = aspect_map.get(raw_aspect) or aspect_map.get(raw_aspect.lower())
    if not aspect:
        raise ValueError(f"仅支持横板（landscape）或竖版（portrait），当前输入：{aspect_ratio}")
    return aspect


def _parse_sora_pro_seconds(model_type: str) -> Optional[str]:
    normalized = (model_type or "").strip().lower()
    match = re.fullmatch(r"sora-2-pro-(\d+)s", normalized)
    if not match:
        return None
    seconds = match.group(1).strip()
    if seconds not in ("10", "15", "25"):
        raise ValueError(f"不支持的 Sora Pro 时长：{seconds}s")
    return seconds


def _build_sora_pro_create_payload(model_type: str, aspect_ratio: str) -> Optional[Dict[str, object]]:
    seconds = _parse_sora_pro_seconds(model_type)
    if seconds is None:
        return None

    aspect = _normalize_aspect_ratio(aspect_ratio)
    size = "1792x1024" if aspect == "landscape" else "1024x1792"
    return {
        "seconds": seconds,
        "size": size,
    }


def _build_model_id(model_type: str, aspect_ratio: str) -> str:
    aspect = _normalize_aspect_ratio(aspect_ratio)

    # Sora Pro: 请求体 model 固定为 sora-2-pro，其它控制参数通过 seconds/size 字段传递
    if _parse_sora_pro_seconds(model_type) is not None:
        return "sora-2-pro"

    normalized = (model_type or "").strip().lower()
    if normalized in ("sora-2", "sora-2-10s"):
        return f"sora-2-{aspect}"
    if normalized == "sora-2-15s":
        return f"sora-2-{aspect}-15s"
    if normalized == "veo3.1":
        return f"veo3.1-{aspect}"
    raise ValueError("不支持的模型类型，请检查 model_type 设置")

