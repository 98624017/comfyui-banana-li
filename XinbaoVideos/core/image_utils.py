"""
图像处理工具（从原 `Xinbao_Video_Generator.py` 迁移）。

保持签名与行为不变，用于参考图压缩与 tensor → PIL 转换。
"""

from __future__ import annotations

import io

import numpy as np
import torch
from PIL import Image

from .constants import MAX_IMAGE_BYTES


def _tensor_to_rgb_image(image_tensor: torch.Tensor) -> Image.Image:
    tensor = image_tensor
    if len(tensor.shape) == 4:
        tensor = tensor[0]
    array = tensor.detach().cpu().numpy()
    if array.max() <= 1.0:
        array = (array * 255.0).clip(0, 255)
    array = array.astype(np.uint8)
    if array.shape[-1] == 1:
        array = np.repeat(array, 3, axis=2)
    return Image.fromarray(array, mode="RGB")


def _compress_image(image: Image.Image, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
    quality_steps = [92, 85, 75, 65, 55, 45]
    resize_scales = [0.95, 0.9, 0.85, 0.75, 0.65, 0.55, 0.45]

    def _save(img: Image.Image, quality: int) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()

    initial = _save(image, quality_steps[0])
    if len(initial) <= max_bytes:
        return initial

    for q in quality_steps[1:]:
        data = _save(image, q)
        if len(data) <= max_bytes:
            return data

    width, height = image.size
    for scale in resize_scales:
        new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        resized = image.resize(new_size, Image.LANCZOS)
        for q in quality_steps[2:]:
            data = _save(resized, q)
            if len(data) <= max_bytes:
                return data

    return _save(image, quality_steps[-1])

