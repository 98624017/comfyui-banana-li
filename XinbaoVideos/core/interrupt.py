"""中断检查（从原 `Xinbao_Video_Generator.py` 迁移）。"""

from __future__ import annotations

import comfy.model_management


def _ensure_not_interrupted() -> None:
    comfy.model_management.throw_exception_if_processing_interrupted()

