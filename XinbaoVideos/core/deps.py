"""
依赖单例（从原 `Xinbao_Video_Generator.py` 迁移）。

避免在 import-time 触发网络/文件 IO，仅构造对象并交由调用方使用。
"""

from __future__ import annotations

from config_manager import ConfigManager
from banana_kv_auth import EdgeKVAuthClient
from logger import logger


_CONFIG_MANAGER = ConfigManager()
_KV_AUTH_CLIENT = EdgeKVAuthClient(_CONFIG_MANAGER, logger)

