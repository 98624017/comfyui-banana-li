"""
API Key 解析、线路选择与 KV 鉴权（从原 `Xinbao_Video_Generator.py` 抽离）。

目标：保证未来新增豆包/Veo 独立节点时可复用同一套规则，且与重构前行为一致。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Tuple

from banana_kv_auth import EdgeKVAuthClient
from config_manager import ConfigManager


_TEST_ROUTE_LABEL = "心宝❤新渠道"
_TEST_BASE_URL_B64 = "aHR0cHM6Ly94aW5iYW9hcGkuZHBkbnMub3Jn"


@dataclass(frozen=True)
class KVResult:
    allowed: bool
    fail_open: bool = False
    user_message: str = ""


def resolve_api_key_and_base(
    config_manager: ConfigManager,
    raw_input_key: str,
    route_choice: str,
) -> Tuple[str, str, str]:
    """
    返回 (api_key, base_url, key_source)。

    行为保持与历史版本一致：
    - 节点/全局输入优先，其次 config.ini
    - `fixsk-` 前缀会自动走隐藏线路（并将 key 去掉 `fix` 前缀后再 sanitize）
    - 测试渠道使用固定 base_url（Key 不通用）
    """
    candidate = (raw_input_key or "").strip()

    if route_choice == _TEST_ROUTE_LABEL:
        effective_base_url = base64.b64decode(_TEST_BASE_URL_B64).decode("utf-8")
    else:
        effective_base_url = config_manager.get_route_base_url(route_choice)

    lowered_input_key = candidate.lower()
    if lowered_input_key.startswith("fixsk-"):
        # 保持历史逻辑：仅剥离 "fix" 前缀，使其变为 "sk-..." 再走 sanitize
        stripped_key = candidate[len("fix") :]
        hidden_key = config_manager.sanitize_api_key(stripped_key)
        if hidden_key:
            return hidden_key, config_manager.get_hidden_base_url(), "隐藏线路临时密钥"

    sanitized_input_key = config_manager.sanitize_api_key(candidate)
    if sanitized_input_key:
        return sanitized_input_key, effective_base_url, "节点/全局输入"

    config_key = config_manager.sanitize_api_key(config_manager.load_api_key())
    if config_key:
        return config_key, effective_base_url, "config.ini 兜底"

    raise RuntimeError(
        "缺少 Banana API Key：请在节点或“心宝❤全局密钥管理”中填写，或在 config.ini 中配置后重试"
    )


def display_route_label(route_choice: str, raw_input_key: str) -> str:
    """用于日志展示的线路名：fixsk- 视为香港专线（保持历史文案）。"""
    if (raw_input_key or "").strip().lower().startswith("fixsk-"):
        return "香港专线"
    return route_choice


def check_kv_auth(
    kv_auth_client: EdgeKVAuthClient,
    api_key: str,
    route_choice: str,
    verify_ssl: bool,
    disable_ssl_override: bool,
) -> KVResult:
    """
    KV 预检：
    - 测试渠道直接放行（保持历史行为）
    - 其他渠道调用现有 `EdgeKVAuthClient.check`
    """
    if route_choice == _TEST_ROUTE_LABEL:
        return KVResult(allowed=True, fail_open=False, user_message="")

    result = kv_auth_client.check(
        api_key,
        verify_ssl=verify_ssl,
        disable_ssl_override=disable_ssl_override,
    )
    return KVResult(
        allowed=bool(getattr(result, "allowed", False)),
        fail_open=bool(getattr(result, "fail_open", False)),
        user_message=str(getattr(result, "user_message", "") or ""),
    )
