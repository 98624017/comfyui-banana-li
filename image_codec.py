import base64
import hashlib
import os
import sys
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from typing import Callable, Dict, List, Optional, Tuple

import time

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont

import comfy.model_management

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from logger import logger


class ImageCodec:
    """负责 Tensor/Base64 转换、缓存与实时预览构建。"""

    def __init__(
        self,
        logger_instance=logger,
        ensure_not_interrupted: Optional[Callable[[], None]] = None,
        cache_size: int = 16,
    ):
        self.logger = logger_instance
        self.ensure_not_interrupted = ensure_not_interrupted
        self._image_cache: "OrderedDict[str, str]" = OrderedDict()
        self._cache_lock = threading.Lock()
        self._cache_size = max(1, cache_size)

    def _maybe_interrupt(self):
        if self.ensure_not_interrupted:
            self.ensure_not_interrupted()

    def _tensor_cache_key(
        self,
        tensor: Optional[torch.Tensor] = None,
        np_data: Optional[np.ndarray] = None,
    ) -> Optional[str]:
        if tensor is None and np_data is None:
            return None
        try:
            target = np_data
            if target is None:
                target = tensor.detach().cpu().numpy()
            return hashlib.sha1(target.tobytes()).hexdigest()
        except Exception:
            return None

    def _get_cached_image_b64(self, cache_key: Optional[str]) -> Optional[str]:
        if not cache_key:
            return None
        with self._cache_lock:
            value = self._image_cache.get(cache_key)
            if value is not None:
                self._image_cache.move_to_end(cache_key)
            return value

    def _set_cached_image_b64(self, cache_key: Optional[str], value: str) -> None:
        if not cache_key or not value:
            return
        with self._cache_lock:
            self._image_cache[cache_key] = value
            self._image_cache.move_to_end(cache_key)
            while len(self._image_cache) > self._cache_size:
                self._image_cache.popitem(last=False)

    def extract_numpy_images(self, tensor: torch.Tensor) -> List[np.ndarray]:
        images: List[np.ndarray] = []
        if tensor is None:
            return images
        try:
            np_data = tensor.detach().cpu().numpy()
        except Exception as exc:
            self.logger.error(f"输入图像转换失败: {exc}")
            return images

        if np_data.ndim == 3:
            np_data = np_data[np.newaxis, ...]
        np_data = np.clip(np_data, 0.0, 1.0)

        for sample in np_data:
            if sample.ndim == 2:
                sample = np.expand_dims(sample, axis=-1)
            if sample.shape[-1] == 1:
                sample = np.repeat(sample, 3, axis=-1)
            images.append(np.ascontiguousarray(sample))
        return images

    def tensor_to_base64(
        self,
        tensor: Optional[torch.Tensor] = None,
        np_image: Optional[np.ndarray] = None,
    ) -> str:
        if np_image is None:
            if tensor is None:
                raise ValueError("必须提供 tensor 或 numpy 图像数据用于编码")
            samples = self.extract_numpy_images(tensor)
            if not samples:
                raise ValueError("无法从 tensor 中提取有效图像数据")
            np_image = samples[0]

        img_array = np.clip(np_image, 0.0, 1.0)
        img_uint8 = (img_array * 255).astype(np.uint8)
        img = Image.fromarray(img_uint8)
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode()

    def prepare_input_images(self, tensors: List[torch.Tensor]) -> List[str]:
        if not tensors:
            return []
        encoded_images: List[str] = []
        for tensor in tensors:
            if tensor is None:
                continue
            for sample in self.extract_numpy_images(tensor):
                cache_key = self._tensor_cache_key(np_data=sample)
                cached_value = self._get_cached_image_b64(cache_key)
                if cached_value is None:
                    base64_value = self.tensor_to_base64(np_image=sample)
                    self._set_cached_image_b64(cache_key, base64_value)
                else:
                    base64_value = cached_value
                encoded_images.append(base64_value)
        return encoded_images

    def base64_to_tensor_single(self, b64_str: str) -> np.ndarray:
        try:
            img_data = base64.b64decode(b64_str)
            img = Image.open(BytesIO(img_data)).convert('RGB')
            img_array = np.array(img).astype(np.float32) / 255.0
            return img_array
        except Exception as exc:
            self.logger.error(f"图片解码失败: {exc}")
            return np.zeros((64, 64, 3), dtype=np.float32)

    def base64_to_tensor_parallel(
        self,
        base64_strings: List[str],
        log_prefix: Optional[str] = None,
        max_workers: Optional[int] = None,
    ) -> torch.Tensor:
        if not isinstance(base64_strings, list) or len(base64_strings) == 0:
            return torch.zeros((1, 64, 64, 3), dtype=torch.float32)

        decode_start = time.time()
        images = []
        worker_cap = max_workers if max_workers is not None else max(4, os.cpu_count() or 1)
        worker_cap = max(1, worker_cap)
        effective_workers = min(worker_cap, len(base64_strings))

        self._maybe_interrupt()
        executor = ThreadPoolExecutor(max_workers=effective_workers)
        try:
            future_to_index = {executor.submit(self.base64_to_tensor_single, b64): i
                               for i, b64 in enumerate(base64_strings)}

            results = [None] * len(base64_strings)
            try:
                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    try:
                        self._maybe_interrupt()
                        results[index] = future.result()
                    except comfy.model_management.InterruptProcessingException:
                        for pending in future_to_index:
                            pending.cancel()
                        raise
                    except Exception as exc:
                        self.logger.error(f"图片{index+1}解码异常: {exc}")
                        results[index] = np.zeros((64, 64, 3), dtype=np.float32)

                images = [r for r in results if r is not None]
            except comfy.model_management.InterruptProcessingException:
                executor.shutdown(wait=False, cancel_futures=True)
                raise
        finally:
            if not executor._shutdown:
                executor.shutdown(wait=False, cancel_futures=True)

        decode_time = time.time() - decode_start
        prefix = log_prefix or ""
        prefix = f"{prefix} " if prefix else ""
        self.logger.success(f"{prefix}并发解码 {len(images)} 张图片完成，耗时: {decode_time:.2f}s")

        return torch.from_numpy(np.stack(images))

    def build_preview_tuple(
        self,
        tensor: Optional[torch.Tensor],
        batch_index: int,
        max_size: int = 512,
    ) -> Optional[Tuple[str, Image.Image, int]]:
        if tensor is None or tensor.shape[0] == 0:
            return None

        try:
            preview_tensor = tensor[0].detach().cpu()
            preview_tensor = torch.clamp(preview_tensor, 0.0, 1.0)
            preview_array = (preview_tensor.numpy() * 255).astype(np.uint8)

            if preview_array.ndim == 3 and preview_array.shape[2] == 1:
                preview_array = np.repeat(preview_array, 3, axis=2)
            elif preview_array.ndim == 2:
                preview_array = np.stack([preview_array] * 3, axis=2)

            preview_image = Image.fromarray(preview_array)
            return ("PNG", preview_image, max_size)
        except Exception as exc:
            self.logger.error(f"实时预览生成失败: 批次 {batch_index + 1}: {str(exc)[:80]}")
            return None


class ErrorCanvas:
    """统一的错误画布与字体管理。"""

    def __init__(self, logger_instance=logger):
        self.logger = logger_instance
        self._font_cache: Dict[int, ImageFont.ImageFont] = {}

    def _get_error_font_paths(self) -> List[str]:
        candidates = []
        windir = os.environ.get("WINDIR")
        if windir:
            for name in ("msyh.ttc", "msyh.ttf", "msjh.ttc", "simhei.ttf", "msmincho.ttc"):
                candidates.append(os.path.join(windir, "Fonts", name))
        candidates.append(os.path.join(os.path.dirname(__file__), "msyh.ttc"))
        return candidates

    def _load_error_font(self, size: int) -> ImageFont.ImageFont:
        cached = self._font_cache.get(size)
        if cached is not None:
            return cached
        for font_path in self._get_error_font_paths():
            if font_path and os.path.exists(font_path):
                try:
                    font = ImageFont.truetype(font_path, size)
                    self._font_cache[size] = font
                    return font
                except Exception:
                    continue
        fallback = ImageFont.load_default()
        self._font_cache[size] = fallback
        return fallback

    @staticmethod
    def _wrap_text_segments(draw: ImageDraw.ImageDraw, text: str,
                            font: ImageFont.ImageFont, max_width: int) -> List[str]:
        if not text:
            return [""]
        segments: List[str] = []
        current = ""
        for ch in text:
            tentative = current + ch
            if draw.textlength(tentative, font=font) <= max_width or not current:
                current = tentative
            else:
                segments.append(current)
                current = ch
        if current:
            segments.append(current)
        return segments

    def build_error_image_tensor(
        self,
        title: str,
        lines: List[str],
        size: Tuple[int, int] = (640, 640),
    ) -> torch.Tensor:
        lines = [line.strip() for line in lines if line and line.strip()]
        if not lines:
            lines = ["发生未知错误"]

        width, height = size
        background = (248, 248, 248)
        accent = (255, 235, 235)
        title_color = (180, 30, 30)
        text_color = (45, 45, 45)

        img = Image.new("RGB", (width, height), background)
        draw = ImageDraw.Draw(img)
        font_title = self._load_error_font(26)
        font_body = self._load_error_font(18)

        margin = 32
        y = margin
        max_text_width = max(10, width - 2 * margin)
        max_y = height - margin

        draw.rectangle([margin - 6, margin - 6, width - margin + 6, y + 40], fill=accent)
        draw.text((margin, y), title, fill=title_color, font=font_title)
        y += font_title.getbbox(title)[3] - font_title.getbbox(title)[1] + 16

        for line in lines:
            wrapped = self._wrap_text_segments(draw, line, font_body, max_text_width)
            for seg in wrapped:
                bbox = font_body.getbbox(seg)
                line_height = bbox[3] - bbox[1] + 6
                if y + line_height > max_y:
                    break
                draw.text((margin, y), seg, fill=text_color, font=font_body)
                y += line_height

        arr = np.array(img).astype(np.float32) / 255.0
        return torch.from_numpy(arr).unsqueeze(0)

    def build_error_tensor_from_text(self, title: str, text: str) -> torch.Tensor:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in normalized.split("\n") if line.strip()]
        if not lines:
            lines = ["发生未知错误"]
        return self.build_error_image_tensor(title, lines)
