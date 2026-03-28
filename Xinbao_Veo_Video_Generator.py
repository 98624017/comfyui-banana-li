"""
Facade: Veo 视频生成节点对外入口（保持 ComfyUI loader 兼容）。

内部实现位于 `XinbaoVideos/`，此文件仅负责导出节点类与 mappings。
"""

from XinbaoVideos.nodes.xinbao_veo_video_generator import XinbaoVeoVideoGenerator


NODE_CLASS_MAPPINGS = {
    "XinbaoVeoVideoGenerator": XinbaoVeoVideoGenerator,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "XinbaoVeoVideoGenerator": "心宝视频生成-Veo",
}

__all__ = [
    "XinbaoVeoVideoGenerator",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
]

