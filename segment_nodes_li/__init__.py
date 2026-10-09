from .mask_bounding_box_aligned import MaskBoundingBoxAligned

# 合并节点映射
NODE_CLASS_MAPPINGS = {"LayerMask: MaskBoundingBoxAligned": MaskBoundingBoxAligned}

NODE_DISPLAY_NAME_MAPPINGS = {"LayerMask: MaskBoundingBoxAligned": "心宝♥Mask遮罩"}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
