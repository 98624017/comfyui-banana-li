"""
Facade: Xinbao 视频节点对外入口（保持 ComfyUI loader 兼容）。

内部实现已拆分至 `XinbaoVideos/`，此文件仅负责导出节点类与 mappings。

兼容性说明：历史版本的调用方/测试会从本模块 import `BananaVideo`、并通过
`Xinbao_Video_Generator.time.sleep` / `Xinbao_Video_Generator.ImageUploader` 进行打桩。
为避免重构导致的 import/patch 回归，此 facade 需要保留这些符号的导出。
"""

import time

from image_uploader import ImageUploader
from XinbaoVideos.core.video_types import BananaVideo
from XinbaoVideos.nodes.xinbao_batch_save_video import XinbaoBatchSaveVideo
from XinbaoVideos.nodes.xinbao_video_generator import XinbaoVideoGenerator


NODE_CLASS_MAPPINGS = {
    "XinbaoVideoGenerator": XinbaoVideoGenerator,
    "XinbaoBatchSaveVideo": XinbaoBatchSaveVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "XinbaoVideoGenerator": "心宝❤视频生成Sora",
    "XinbaoBatchSaveVideo": "心宝❤批量保存视频",
}

__all__ = [
    "BananaVideo",
    "ImageUploader",
    "XinbaoBatchSaveVideo",
    "XinbaoVideoGenerator",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "time",
]
