# xinbao_psd_tool.py
# 心宝 PSD 工具节点 - 从 comfyui_psd_batch 融合并优化稳定性
# 分类：心宝❤工具

import os
import inspect
import torch
import numpy as np
from PIL import Image

try:
    from psd_tools import PSDImage
    from psd_tools.api.layers import PixelLayer
    PSD_AVAILABLE = True
except ImportError:
    PSD_AVAILABLE = False
    print("[心宝❤合成PSD] 警告: psd_tools 未安装，PSD 合成功能不可用")

import folder_paths


def _pixel_layer_frompil_compat(pil_image: Image.Image, parent, *, name: str) -> tuple[object, bool]:
    """兼容 psd-tools 新旧版本的 PixelLayer.frompil。

    部分版本的 `PixelLayer.frompil()` 需要额外传入 `parent`（PSDImage 或 Group），
    否则会出现：PixelLayer.frompil() missing 1 required positional argument: 'parent'
    """

    # 优先通过签名判断，避免误捕获其它 TypeError
    try:
        signature = inspect.signature(PixelLayer.frompil)
    except (TypeError, ValueError):
        signature = None

    if signature is not None:
        parent_param = signature.parameters.get("parent")
        if parent_param is None:
            return (PixelLayer.frompil(pil_image, name=name), False)
        if parent_param.kind == inspect.Parameter.POSITIONAL_ONLY:
            return (PixelLayer.frompil(pil_image, parent, name=name), True)
        return (PixelLayer.frompil(pil_image, parent=parent, name=name), True)

    # 极端情况下无法读取签名：尝试新签名（带 parent），失败再回退旧签名。
    try:
        return (PixelLayer.frompil(pil_image, parent, name=name), True)
    except TypeError:
        return (PixelLayer.frompil(pil_image, name=name), False)


class XinbaoBatchToPSD:
    """
    将多张图片合成为带图层的 PSD 文件
    图层命名：最底层叫"原图"，往上"分层01、分层02……"
    内部用 ASCII 名避免 mac-roman 编码错误
    """
    
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "filename_prefix": ("STRING", {"default": "psd_batch"}),
            },
        }

    RETURN_TYPES = ("STRING", "IMAGE")
    RETURN_NAMES = ("psd_path", "preview")
    FUNCTION = "write_psd"
    CATEGORY = "心宝❤工具"
    OUTPUT_NODE = True

    def write_psd(self, images, filename_prefix):
        # 检查 psd_tools 是否可用
        if not PSD_AVAILABLE:
            raise RuntimeError("psd_tools 未安装，请运行: pip install psd-tools")
        
        # 输入类型标准化
        if images is None:
            raise ValueError("输入图片不能为空")
        
        if not isinstance(images, (list, tuple)):
            images = [images]
        
        # 处理空列表
        if len(images) == 0:
            raise ValueError("输入图片列表为空")

        pil_images = []
        for idx, item in enumerate(images):
            try:
                if isinstance(item, torch.Tensor):
                    item_np = item.cpu().numpy()
                    if item_np.ndim == 3:
                        # 单张图片 (H, W, C)
                        img_np = np.clip(item_np * 255, 0, 255).astype(np.uint8)
                        mode = "RGBA" if img_np.shape[-1] == 4 else "RGB"
                        pil_images.append(Image.fromarray(img_np, mode=mode))
                    elif item_np.ndim == 4:
                        # 批量图片 (B, H, W, C)
                        for i in range(item_np.shape[0]):
                            img_np = np.clip(item_np[i] * 255, 0, 255).astype(np.uint8)
                            mode = "RGBA" if img_np.shape[-1] == 4 else "RGB"
                            pil_images.append(Image.fromarray(img_np, mode=mode))
                    else:
                        print(f"[心宝❤合成PSD] 警告: 跳过第 {idx} 项，维度不支持: {item_np.ndim}")
                elif isinstance(item, str):
                    if os.path.exists(item):
                        pil_images.append(Image.open(item))
                    else:
                        print(f"[心宝❤合成PSD] 警告: 文件不存在，已跳过: {item}")
                elif isinstance(item, Image.Image):
                    pil_images.append(item)
                else:
                    print(f"[心宝❤合成PSD] 警告: 跳过第 {idx} 项，不支持的类型: {type(item)}")
            except Exception as e:
                print(f"[心宝❤合成PSD] 警告: 处理第 {idx} 项时出错: {e}")
        
        # 再次检查是否有有效图片
        if len(pil_images) == 0:
            raise ValueError("没有有效的图片可以合成")
        
        # 确保所有图片模式一致（统一转为 RGBA）
        base_size = pil_images[0].size
        for i, img in enumerate(pil_images):
            if img.mode != "RGBA":
                pil_images[i] = img.convert("RGBA")
            # 确保尺寸一致
            if img.size != base_size:
                pil_images[i] = pil_images[i].resize(base_size, Image.LANCZOS)

        # 创建 PSD
        psd = PSDImage.new(mode="RGBA", size=base_size, depth=8)

        for idx, img in enumerate(pil_images):
            # ASCII 内部名，避免编码错误
            ascii_name = "yuantu" if idx == 0 else f"layer{idx:02d}"
            # 中文显示名
            display_name = "原图" if idx == 0 else f"分层{idx:02d}"

            layer, auto_attached = _pixel_layer_frompil_compat(img, psd, name=ascii_name)
            layer.unicode_name = display_name
            layer.left, layer.top = 0, 0
            # psd-tools 新版 frompil(parent=...) 会自动 append；旧版需要手动 append
            if not auto_attached:
                psd.append(layer)

        # 生成唯一文件名
        counter = 0
        while True:
            psd_filename = f"{filename_prefix}_{counter:04d}.psd"
            psd_path = os.path.join(self.output_dir, psd_filename)
            if not os.path.exists(psd_path):
                break
            counter += 1
        
        # 保存 PSD
        psd.save(psd_path)
        print(f"[心宝❤合成PSD] 已保存 -> {psd_path}")

        # 生成预览缩略图
        preview_rgb = pil_images[0].convert("RGB")
        preview_rgb.thumbnail((256, 256), Image.LANCZOS)
        preview_np = np.array(preview_rgb).astype(np.float32) / 255.0
        preview_tensor = torch.from_numpy(preview_np).unsqueeze(0)

        return {
            "ui": {"images": [{
                "filename": os.path.basename(psd_path),
                "subfolder": "",
                "type": "output"
            }]},
            "result": (psd_path, preview_tensor)
        }


class XinbaoLayerSelect:
    """
    选择分层数量
    """
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "layers": (["1层", "2层", "3层", "4层", "5层"], {"default": "1层"}),
            }
        }

    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("分层数",)
    FUNCTION = "get_number"
    CATEGORY = "心宝❤工具"

    LAYER_MAPPING = {
        "1层": 5,
        "2层": 9,
        "3层": 13,
        "4层": 17,
        "5层": 21
    }

    def get_number(self, layers):
        return (self.LAYER_MAPPING.get(layers, 5),)


# ComfyUI 节点映射
NODE_CLASS_MAPPINGS = {
    "XinbaoBatchToPSD": XinbaoBatchToPSD,
    "XinbaoLayerSelect": XinbaoLayerSelect,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "XinbaoBatchToPSD": "心宝❤合成PSD",
    "XinbaoLayerSelect": "心宝❤分层数",
}
