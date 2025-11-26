from .segment_anything_ultra_Li import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .mask_bounding_box_aligned import MaskBoundingBoxAligned

# 合并节点映射
NODE_CLASS_MAPPINGS.update(
    {
        "LayerMask: MaskBoundingBoxAligned": MaskBoundingBoxAligned,
    }
)

NODE_DISPLAY_NAME_MAPPINGS.update(
    {
        "LayerMask: MaskBoundingBoxAligned": "Mask遮罩❤️‍🔥心宝专用",
    }
)

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
