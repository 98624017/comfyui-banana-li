"""
心宝局部裁切预处理与贴回节点
"""

from __future__ import annotations

import base64
import threading
from collections import OrderedDict
from io import BytesIO
from typing import Dict, Tuple

import torch
import torch.nn.functional as F
import torchvision.transforms.functional as TF
import comfy.utils
import folder_paths
import random
import os
from aiohttp import web
from PIL import Image, ImageFilter

from segment_nodes_li.mask_bounding_box_aligned import MaskBoundingBoxAligned
from banana_binding import (
    BINDING_TYPE,
    BindingError,
    require_activated,
)


def _align_to_multiple(value: int, multiple: int) -> int:
    multiple = max(1, multiple)
    aligned = int(round(value / multiple) * multiple)
    return max(multiple, aligned)


def _upscale_image(image: torch.Tensor, width: int, height: int, method: str = "lanczos", crop: str = "center") -> torch.Tensor:
    return (
        comfy.utils.common_upscale(
            image.permute(0, 3, 1, 2),
            width,
            height,
            upscale_method=method,
            crop=crop,
        ).permute(0, 2, 3, 1)
    )


def _upscale_mask(mask: torch.Tensor, width: int, height: int, crop: str = "center") -> torch.Tensor:
    return (
        comfy.utils.common_upscale(
            mask.unsqueeze(1),
            width,
            height,
            upscale_method="nearest-exact",
            crop=crop,
        ).squeeze(1)
    )


def _grow_mask_like_kj(mask: torch.Tensor, iterations: int, blur_radius: float = 20.0, tapered_corners: bool = True) -> torch.Tensor:
    """
    复刻 KJNodes GrowMaskWithBlur 的核心形变：
    - 采用十字核（tapered_corners=True）做膨胀/腐蚀
    - 随后使用 PIL GaussianBlur(radius) 生成柔和过渡
    """
    if mask.dim() == 2:
        mask = mask.unsqueeze(0)

    m = mask
    if iterations != 0:
        try:
            import kornia.morphology as morph

            kernel = torch.tensor(
                [[0, 1, 0], [1, 1, 1], [0, 1, 0]] if tapered_corners else [[1, 1, 1], [1, 1, 1], [1, 1, 1]],
                device=m.device,
                dtype=m.dtype,
            )
            for _ in range(abs(iterations)):
                if iterations > 0:
                    m = morph.dilation(m.unsqueeze(1), kernel).squeeze(1)
                else:
                    m = morph.erosion(m.unsqueeze(1), kernel).squeeze(1)
        except Exception:
            # 降级：使用方形核近似，保证在缺少 kornia 时仍可运行
            m_sq = m.unsqueeze(1)
            for _ in range(abs(iterations)):
                if iterations > 0:
                    m_sq = F.max_pool2d(m_sq, kernel_size=3, stride=1, padding=1)
                else:
                    m_sq = -F.max_pool2d(-m_sq, kernel_size=3, stride=1, padding=1)
            m = m_sq.squeeze(1)

    if blur_radius > 0:
        blurred = []
        for single in m:
            pil_img = TF.to_pil_image(single.detach().cpu().clamp(0, 1))
            pil_img = pil_img.filter(ImageFilter.GaussianBlur(blur_radius))
            blurred.append(TF.pil_to_tensor(pil_img).float().squeeze(0) / 255.0)
        m = torch.stack(blurred, dim=0).to(mask.device)

    return m.clamp(0, 1)


def _gaussian_blur_zero_pad(mask: torch.Tensor, kernel_size: int = 41, sigma: float = 20.0) -> torch.Tensor:
    """使用零填充的高斯模糊，确保边缘会淡出而非保持满值。"""
    pad = (kernel_size - 1) // 2
    blurred = TF.gaussian_blur(
        F.pad(mask.unsqueeze(1), (pad, pad, pad, pad), mode="constant", value=0),
        kernel_size=kernel_size,
        sigma=sigma,
    )
    return blurred[:, :, pad:-pad, pad:-pad].squeeze(1)


def _hex_to_rgb_tensor(color_hex: str) -> torch.Tensor:
    hex_value = color_hex.lstrip("#")
    if len(hex_value) == 3:
        hex_value = "".join([c * 2 for c in hex_value])
    rgb = [int(hex_value[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
    return torch.tensor(rgb, dtype=torch.float32)


def _letterbox_to_square(tensor: torch.Tensor, target_size: int, fill_value: float = 0.0) -> torch.Tensor:
    """将 tensor 居中填充为 target_size 的正方形，匹配旧工作流的 letterbox 行为。"""
    if target_size <= 0:
        return tensor

    b, h, w = tensor.shape[:3]
    if h == target_size and w == target_size:
        return tensor

    pad_w = max(0, target_size - w)
    pad_h = max(0, target_size - h)
    pad_left = pad_w // 2
    pad_right = pad_w - pad_left
    pad_top = pad_h // 2
    pad_bottom = pad_h - pad_top

    if tensor.ndim == 4:
        padded = torch.full(
            (b, target_size, target_size, tensor.shape[3]),
            fill_value,
            device=tensor.device,
            dtype=tensor.dtype,
        )
        padded[:, pad_top : pad_top + h, pad_left : pad_left + w, :] = tensor
    else:
        padded = torch.full(
            (b, target_size, target_size),
            fill_value,
            device=tensor.device,
            dtype=tensor.dtype,
        )
        padded[:, pad_top : pad_top + h, pad_left : pad_left + w] = tensor

    return padded


_PREVIEW_CACHE: "OrderedDict[str, Dict[str, torch.Tensor]]" = OrderedDict()
_PREVIEW_CACHE_LOCK = threading.Lock()
_PREVIEW_CACHE_LIMIT = 8


def _tensor_to_base64_png(tensor: torch.Tensor) -> str:
    if tensor.ndim == 4:
        sample = tensor[0]
    else:
        sample = tensor
    sample = sample.detach().cpu().clamp(0, 1)
    if sample.ndim == 3 and sample.shape[-1] == 1:
        sample = sample.repeat(1, 1, 3)
    arr = (sample.numpy() * 255).astype("uint8")
    img = Image.fromarray(arr)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _save_preview(image_tensor: torch.Tensor, filename_prefix: str = "Banana_Preview") -> Dict:
    if image_tensor.ndim == 4:
        image_tensor = image_tensor[0]
    
    image_tensor = image_tensor.detach().cpu().clamp(0, 1)
    if image_tensor.ndim == 3 and image_tensor.shape[-1] == 1:
        image_tensor = image_tensor.repeat(1, 1, 3)
        
    img = Image.fromarray((image_tensor.numpy() * 255).astype("uint8"))
    
    filename = f"{filename_prefix}_{random.randint(100000, 999999)}.png"
    subfolder = "banana_preview"
    full_output_folder = os.path.join(folder_paths.get_temp_directory(), subfolder)
    
    if not os.path.exists(full_output_folder):
        os.makedirs(full_output_folder)
        
    img.save(os.path.join(full_output_folder, filename))
    
    return {
        "filename": filename,
        "subfolder": subfolder,
        "type": "temp"
    }


@torch.no_grad()
def _run_preprocess_core(
    image: torch.Tensor,
    mask: torch.Tensor,
    ref_image: torch.Tensor | None,
    padding_slider: float,
    blend_slider: float,
    expand_slider: float,
    scale_to_length: int,
    target_size: int,
    round_to_multiple: int,
    overlay_color: str,
    skip_binding_validation: bool = False,
) -> Tuple[torch.Tensor, torch.Tensor, Dict]:
    b, h, w, _ = image.shape
    target_long = _align_to_multiple(scale_to_length, round_to_multiple)
    longest = max(h, w)
    scale_ratio = target_long / float(longest)
    scaled_w = _align_to_multiple(max(1, int(round(w * scale_ratio))), round_to_multiple)
    scaled_h = _align_to_multiple(max(1, int(round(h * scale_ratio))), round_to_multiple)
    base_image = _upscale_image(image, scaled_w, scaled_h)
    base_mask = _upscale_mask(mask, scaled_w, scaled_h)

    # 用户反馈：原工作流（LayerUtility: ImageScaleByAspectRatio V2）并不强制 letterbox 到方形，
    # 而是保留缩放后的尺寸（8倍数对齐）。因此移除 _letterbox_to_square。
    # base_image = _letterbox_to_square(base_image, target_long, fill_value=0.0)
    # base_mask = _letterbox_to_square(base_mask, target_long, fill_value=0.0)

    bbox_node = MaskBoundingBoxAligned(skip_binding_validation=skip_binding_validation)
    # 兼容旧工作流：padding = clamp(0~10) * 32，再取整
    try:
        padding_numeric = float(padding_slider)
    except (TypeError, ValueError):
        padding_numeric = 0.0
    padding_numeric = max(0.0, min(10.0, padding_numeric))
    padding_pixels = int(padding_numeric * 32)
    crop_mask, crop_image, x, y, crop_w, crop_h = bbox_node.execute(
        None,
        base_mask,
        padding_pixels,
        0,
        base_image,
    )
    # crop_size = int(crop_w) # Removed single scalar
    crop_w = int(crop_w)
    crop_h = int(crop_h)

    target_size_aligned = _align_to_multiple(target_size, round_to_multiple)
    patch_image = _upscale_image(crop_image, target_size_aligned, target_size_aligned)
    # 可视化和贴回都沿用裁剪得到的掩膜形状，避免整块填满导致回贴范围错误
    viz_crop_mask = crop_mask.clamp(0, 1)
    patch_mask = _upscale_mask(viz_crop_mask, target_size_aligned, target_size_aligned).unsqueeze(-1)

    # 贴回用掩膜：计算实际遮罩的 Bounding Box，并扩展为正方形（keep_ratio）
    # 这对应原工作流中 Mask To Region (keep_ratio, min=64) -> Image To Mask 的逻辑
    if crop_mask.max().item() > 0:
        # 使用 nonzero(as_tuple=True) 以兼容 2D [H, W] 或 3D [B, H, W]
        indices = torch.nonzero(crop_mask > 0.5, as_tuple=True)
        if len(indices) >= 2 and len(indices[0]) > 0:
            # 取最后两个维度作为 y (高度) 和 x (宽度)
            y_indices = indices[-2]
            x_indices = indices[-1]
            
            y_min, y_max = y_indices.min().item(), y_indices.max().item()
            x_min, x_max = x_indices.min().item(), x_indices.max().item()
            
            # 计算当前 BBox 的中心和最大边长，以生成正方形
            # 原工作流 Mask To Region 设置了 min_width=64, min_height=64
            h = y_max - y_min + 1
            w = x_max - x_min + 1
            size = max(h, w, 64)
            
            # 限制 size 不能超过图像本身尺寸
            H, W = crop_mask.shape[-2:]
            size = min(size, H, W)
            
            center_y = (y_min + y_max) / 2
            center_x = (x_min + x_max) / 2
            
            # 计算正方形边界
            y1 = int(center_y - size / 2)
            x1 = int(center_x - size / 2)
            y2 = y1 + size
            x2 = x1 + size
            
            # 边界平移逻辑：如果超出边界，尝试向反方向移动以保持 size
            if y1 < 0:
                y2 -= y1 # y1 is negative, so this adds to y2
                y1 = 0
            if x1 < 0:
                x2 -= x1
                x1 = 0
            if y2 > H:
                y1 -= (y2 - H)
                y2 = H
            if x2 > W:
                x1 -= (x2 - W)
                x2 = W
            
            # 最终再次 clamp，防止平移后另一侧溢出（当 size == H 或 W 时）
            y1 = max(0, y1)
            x1 = max(0, x1)
            y2 = min(H, y2)
            x2 = min(W, x2)
            
            raw_crop_mask = torch.zeros_like(crop_mask)
            # 使用切片赋值，自动广播到 batch 维度（如果存在）
            raw_crop_mask[..., y1:y2, x1:x2] = 1.0
        else:
            raw_crop_mask = crop_mask.clone().clamp(0, 1)
    else:
        raw_crop_mask = crop_mask.clone().clamp(0, 1)

    expand_iter = int(round(expand_slider * 100))
    # 先做形态学扩展/腐蚀，不做模糊 (blur_radius=0)
    expanded_mask = _grow_mask_like_kj(raw_crop_mask, expand_iter, blur_radius=0.0, tapered_corners=True)
    # 再做零填充高斯模糊，产生晕影
    expanded_mask = _gaussian_blur_zero_pad(expanded_mask, kernel_size=61, sigma=20.0).clamp(0, 1)

    rgb = _hex_to_rgb_tensor(overlay_color).to(patch_image.device)
    color_patch = rgb.view(1, 1, 1, 3).repeat(1, target_size_aligned, target_size_aligned, 1)
    overlay = patch_image * (1 - patch_mask) + color_patch * patch_mask
    blended_patch = patch_image * (1 - blend_slider) + overlay * blend_slider

    if ref_image is not None:
        ref_scaled = _upscale_image(ref_image, target_size_aligned, target_size_aligned)
    else:
        # 如果没有参考图，生成一张纯白图作为占位
        ref_scaled = torch.ones((1, target_size_aligned, target_size_aligned, 3), dtype=torch.float32, device=image.device)

    region_data = {
        "base_image": base_image,
        "mask": expanded_mask,
        "x": int(x),
        "y": int(y),

        "crop_w": crop_w,
        "crop_h": crop_h,
        "target_size": target_size_aligned,
        "original_size": (w, h),
        "scaled_size": (scaled_w, scaled_h),
    }
    return blended_patch, ref_scaled, region_data


def _cache_preview_state(
    node_id: str,
    image: torch.Tensor,
    mask: torch.Tensor,
    ref_image: torch.Tensor,
) -> None:
    if not node_id:
        return
    with _PREVIEW_CACHE_LOCK:
        _PREVIEW_CACHE[node_id] = {
            "image": image.detach().cpu(),
            "mask": mask.detach().cpu(),
            "ref_image": ref_image.detach().cpu() if ref_image is not None else None,
        }
        while len(_PREVIEW_CACHE) > _PREVIEW_CACHE_LIMIT:
            _PREVIEW_CACHE.popitem(last=False)


class BananaLocalCropPreprocess:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE", {}),
                "mask": ("MASK", {}),
                # "ref_image": ("IMAGE", {}),  <-- Moved to optional
                # 与旧工作流一致：滑块值 * 32 -> padding 像素，再取整
                "padding_slider": (
                    "FLOAT",
                    {"default": 3.0, "min": 0.0, "max": 10.0, "step": 0.01, "display": "slider"},
                ),
                "blend_slider": (
                    "FLOAT",
                    {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01, "display": "slider"},
                ),
                "expand_slider": (
                    "FLOAT",
                    {"default": 0.3, "min": -1.0, "max": 1.0, "step": 0.01, "display": "slider"},
                ),
                "scale_to_length": ("INT", {"default": 2048, "min": 64, "max": 8192, "step": 1}),
                "target_size": ("INT", {"default": 1536, "min": 64, "max": 8192, "step": 1}),
                "round_to_multiple": ("INT", {"default": 8, "min": 1, "max": 64, "step": 1}),
                "overlay_color": ("STRING", {"default": "#7f7f7f"}),
            },
            "optional": {
                "ref_image": ("IMAGE", {}),
            },
            "hidden": {
                # 使用 ComfyUI 提供的 UNIQUE_ID，保证前端 node.id 与后端节点标识一致
                "preview_node_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("IMAGE", "IMAGE", "BANANA_REGION_DATA")
    RETURN_NAMES = ("image_1", "image_2", "region_data")
    FUNCTION = "execute"
    CATEGORY = "❤️‍🔥心宝专用/增强工具"

    def execute(
        self,
        image: torch.Tensor,
        mask: torch.Tensor,
        padding_slider: float,
        blend_slider: float,
        expand_slider: float,
        scale_to_length: int,
        target_size: int,
        round_to_multiple: int,
        overlay_color: str,
        ref_image: torch.Tensor | None = None,
        preview_node_id: str | None = None,
    ) -> Dict:
        blended_patch, ref_scaled, region_data = _run_preprocess_core(
            image,
            mask,
            ref_image,
            padding_slider,
            blend_slider,
            expand_slider,
            scale_to_length,
            target_size,
            round_to_multiple,
            overlay_color,
            skip_binding_validation=True,
        )

        # 使用隐藏输入提供的 UNIQUE_ID 作为缓存键，避免依赖实现细节；
        # 如若运行环境为旧版本或 UNIQUE_ID 不可用，退回到历史属性以提高兼容性。
        legacy_node_id = str(getattr(self, "id", "") or getattr(self, "unique_id", "") or "")
        node_id = str(preview_node_id or legacy_node_id)

        _cache_preview_state(node_id, image, mask, ref_image)
        if legacy_node_id and legacy_node_id != node_id:
            _cache_preview_state(legacy_node_id, image, mask, ref_image)

        region_data["preview_node_id"] = node_id

        # 保存预览图到临时目录，以便前端直接显示
        preview_images = []
        try:
            preview_images.append(_save_preview(blended_patch, "Banana_Crop"))
            preview_images.append(_save_preview(ref_scaled, "Banana_Ref"))
        except Exception:
            pass

        return {
            "ui": {"banana_images": preview_images},
            "result": (blended_patch, ref_scaled, region_data),
        }


class BananaLocalCropPaste:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "banana_image": ("IMAGE", {}),
                "region_data": ("BANANA_REGION_DATA", {}),
                "binding_context": (
                    BINDING_TYPE,
                    {"tooltip": "需已在心宝❤Banana 节点激活的绑定上下文"},
                ),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "execute"
    CATEGORY = "❤️‍🔥心宝专用/增强工具"

    def execute(self, binding_context, banana_image: torch.Tensor, region_data: Dict) -> Tuple[torch.Tensor]:
        ctx = binding_context or region_data.get("binding_context")
        try:
            require_activated(ctx)
        except BindingError as exc:
            raise RuntimeError(f"{exc}；请确认绑定已接入心宝❤Banana 节点后再执行贴回。") from exc
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"绑定校验失败：{exc}") from exc
        base_image: torch.Tensor = region_data["base_image"]
        mask: torch.Tensor = region_data["mask"]
        x = region_data["x"]
        y = region_data["y"]
        crop_w = region_data.get("crop_w", region_data.get("crop_size")) # Fallback for legacy data
        crop_h = region_data.get("crop_h", region_data.get("crop_size")) # Fallback for legacy data
        
        # Ensure we have valid dimensions
        if crop_w is None or crop_h is None:
             raise ValueError("Region data missing crop dimensions")
             


        banana_batch = banana_image.shape[0]
        base_batch = base_image.shape[0]
        if banana_batch != base_batch:
            if base_batch == 1 and banana_batch > 1:
                base_image = base_image.repeat(banana_batch, 1, 1, 1)
                mask = mask.repeat(banana_batch, 1, 1)
            elif banana_batch == 1 and base_batch > 1:
                banana_image = banana_image.repeat(base_batch, 1, 1, 1)
            else:
                raise ValueError(f"banana_image batch({banana_batch}) 与区域数据 batch({base_batch}) 不匹配，且无法广播")

        paste_source = _upscale_image(banana_image, crop_w, crop_h)
        paste_mask = _upscale_mask(mask, crop_w, crop_h).unsqueeze(-1).clamp(0, 1)

        result = base_image.clone()
        y2 = min(result.shape[1], y + crop_h)
        x2 = min(result.shape[2], x + crop_w)
        region_h = y2 - y
        region_w = x2 - x
        if region_h > 0 and region_w > 0:
            result_slice = result[:, y:y2, x:x2, :]
            source_slice = paste_source[:, :region_h, :region_w, :]
            mask_slice = paste_mask[:, :region_h, :region_w, :]

            result[:, y:y2, x:x2, :] = source_slice * mask_slice + result_slice * (1 - mask_slice)
        
        return (result,)


NODE_CLASS_MAPPINGS = {
    "BananaLocalCropPreprocess": BananaLocalCropPreprocess,
    "BananaLocalCropPaste": BananaLocalCropPaste,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "BananaLocalCropPreprocess": "心宝❤局部裁切预处理",
    "BananaLocalCropPaste": "心宝❤局部裁切贴回",
}


try:
    from server import PromptServer
except ImportError:  # pragma: no cover - 测试或非 ComfyUI 环境
    class _DummyPromptServer:
        instance = None

    PromptServer = _DummyPromptServer()

_PREVIEW_ROUTE_REGISTERED = False
_PREVIEW_ROUTE_TIMER: threading.Timer | None = None


def _ensure_preview_route(prompt_server_provider):
    global _PREVIEW_ROUTE_REGISTERED, _PREVIEW_ROUTE_TIMER
    if _PREVIEW_ROUTE_REGISTERED:
        return
    prompt_server = prompt_server_provider()
    if prompt_server is None:
        if _PREVIEW_ROUTE_TIMER is None or not _PREVIEW_ROUTE_TIMER.is_alive():
            timer = threading.Timer(1.0, lambda: _ensure_preview_route(prompt_server_provider))
            timer.daemon = True
            _PREVIEW_ROUTE_TIMER = timer
            timer.start()
        return

    @prompt_server.routes.post("/banana/local_crop_preview")
    async def handle_local_crop_preview(request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "message": "无效的 JSON 请求"}, status=400)

        node_id = str(payload.get("node_id") or "")
        if not node_id:
            return web.json_response({"success": False, "message": "缺少节点标识"}, status=400)

        try:
            padding_slider = float(payload.get("padding_slider", 1.0))
            blend_slider = float(payload.get("blend_slider", 1.0))
            expand_slider = float(payload.get("expand_slider", 0.3))
            scale_to_length = int(payload.get("scale_to_length", 2048))
            target_size = int(payload.get("target_size", 1536))
            round_to_multiple = int(payload.get("round_to_multiple", 8))
            overlay_color = str(payload.get("overlay_color", "#7f7f7f"))
        except Exception:
            return web.json_response({"success": False, "message": "参数解析失败"}, status=400)

        with _PREVIEW_CACHE_LOCK:
            cached = _PREVIEW_CACHE.get(node_id)
        if cached is None:
            return web.json_response(
                {"success": False, "message": "未找到缓存输入，请先运行一次节点"},
                status=400,
            )

        try:
            blended_patch, _, _ = _run_preprocess_core(
                cached["image"].clone(),
                cached["mask"].clone(),
                cached["ref_image"].clone() if cached["ref_image"] is not None else None,
                padding_slider,
                blend_slider,
                expand_slider,
                scale_to_length,
                target_size,
                round_to_multiple,
                overlay_color,
                skip_binding_validation=True,
            )
            image_b64 = _tensor_to_base64_png(blended_patch)
            return web.json_response({"success": True, "image_b64": image_b64})
        except Exception as exc:
            return web.json_response(
                {"success": False, "message": str(exc)},
                status=400,
            )

    _PREVIEW_ROUTE_REGISTERED = True


_ensure_preview_route(lambda: getattr(PromptServer, "instance", None))
