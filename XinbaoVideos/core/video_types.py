"""ComfyUI VIDEO 输出类型封装（从原 `Xinbao_Video_Generator.py` 迁移）。"""

from __future__ import annotations

import io


class BananaVideo:
    def __init__(self, data: io.BytesIO):
        self.data = data
        self.data.seek(0)

    def get_dimensions(self):
        # 无法轻易获取尺寸，返回 0,0 由 ComfyUI 处理
        return 0, 0

    def save_to(self, path, **kwargs):
        self.data.seek(0)
        with open(path, "wb") as f:
            f.write(self.data.read())

