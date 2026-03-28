"""
Facade: 即梦豆包系视频生成节点对外入口（保持 ComfyUI loader 兼容）。

内部实现位于 `XinbaoVideos/`，此文件仅负责导出节点类与 mappings。
"""

from XinbaoVideos.nodes.xinbao_doubao_video_generator import XinbaoDoubaoVideoGenerator


NODE_CLASS_MAPPINGS = {
    "XinbaoDoubaoVideoGenerator": XinbaoDoubaoVideoGenerator,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "XinbaoDoubaoVideoGenerator": "心宝视频生成-即梦豆包系",
}

__all__ = [
    "XinbaoDoubaoVideoGenerator",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
]

