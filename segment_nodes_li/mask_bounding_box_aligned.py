"""
Mask Bounding Box with 8-pixel alignment
参考 MaskBoundingBox 逻辑，但输出尺寸对齐到8的倍数
"""

import torch
import torchvision.transforms.v2 as T
import comfy.utils
from banana_binding import (
    BINDING_TYPE,
    BindingError,
    build_missing_hint,
    enforce_front_budget,
)


class MaskBoundingBoxAligned:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "binding_context": (
                    BINDING_TYPE,
                    {"tooltip": "来自“心宝❤绑定生成/透传”的绑定上下文，缺失将拒绝执行"},
                ),
                "mask": ("MASK",),
                "padding": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 1}),
                "blur": ("INT", {"default": 0, "min": 0, "max": 256, "step": 1}),
            },
            "optional": {
                "image_optional": ("IMAGE",),
            },
        }

    RETURN_TYPES = ("MASK", "IMAGE", "INT", "INT", "INT", "INT")
    RETURN_NAMES = ("MASK", "IMAGE", "x", "y", "width", "height")
    FUNCTION = "execute"
    CATEGORY = "❤️‍🔥心宝专用/掩膜"

    def execute(self, binding_context, mask, padding, blur, image_optional=None):
        try:
            enforce_front_budget(binding_context)
        except BindingError as exc:
            raise RuntimeError(f"{exc}；{build_missing_hint()}") from exc
        except Exception as exc:  # pragma: no cover - 非预期错误
            raise RuntimeError(f"绑定校验失败，请重新连接绑定上下文。{exc}") from exc

        if mask.dim() == 2:
            mask = mask.unsqueeze(0)

        if image_optional is None:
            image_optional = mask.unsqueeze(3).repeat(1, 1, 1, 3)

        # resize the image if it's not the same size as the mask
        if image_optional.shape[1:] != mask.shape[1:]:
            image_optional = comfy.utils.common_upscale(
                image_optional.permute([0, 3, 1, 2]),
                mask.shape[2],
                mask.shape[1],
                upscale_method="bicubic",
                crop="center",
            ).permute([0, 2, 3, 1])

        # match batch size
        if image_optional.shape[0] < mask.shape[0]:
            image_optional = torch.cat(
                (
                    image_optional,
                    image_optional[-1]
                    .unsqueeze(0)
                    .repeat(mask.shape[0] - image_optional.shape[0], 1, 1, 1),
                ),
                dim=0,
            )
        elif image_optional.shape[0] > mask.shape[0]:
            image_optional = image_optional[: mask.shape[0]]

        # blur the mask
        if blur > 0:
            if blur % 2 == 0:
                blur += 1
            mask = T.functional.gaussian_blur(mask.unsqueeze(1), blur).squeeze(1)

        # 计算bounding box
        _, y, x = torch.where(mask)
        if len(x) == 0 or len(y) == 0:
            # 如果没有mask区域，返回整个图像，但尺寸对齐到8的倍数，且为正方形
            h, w = mask.shape[1], mask.shape[2]
            x1, y1 = 0, 0
            # 取较小的边作为正方形边长
            square_size = min(w, h)
            # 对齐到8的倍数（向下取整，确保不超出边界）
            square_size = (square_size // 8) * 8
            # 如果向下取整后为0，至少保证是8
            if square_size == 0:
                square_size = 8
            # 确保不超出边界
            square_size = min(square_size, w, h)
            x2 = x1 + square_size
            y2 = y1 + square_size
            final_square_size = square_size
        else:
            # 计算mask区域的bounding box（包含padding）
            bb_x1 = max(0, x.min().item() - padding)
            bb_x2 = min(mask.shape[2], x.max().item() + 1 + padding)
            bb_y1 = max(0, y.min().item() - padding)
            bb_y2 = min(mask.shape[1], y.max().item() + 1 + padding)

            # 计算bounding box的中心点
            bb_center_x = (bb_x1 + bb_x2) / 2.0
            bb_center_y = (bb_y1 + bb_y2) / 2.0

            # 计算bounding box的宽度和高度
            bb_width = bb_x2 - bb_x1
            bb_height = bb_y2 - bb_y1

            # 取较大的边作为正方形边长（确保包含所有mask内容）
            square_size = max(bb_width, bb_height)

            # 将正方形尺寸向上取整到8的倍数
            square_size_aligned = ((square_size + 7) // 8) * 8

            # 以中心点为基准，计算正方形的边界
            half_size = square_size_aligned / 2.0
            x1 = int(bb_center_x - half_size)
            y1 = int(bb_center_y - half_size)
            x2 = x1 + square_size_aligned
            y2 = y1 + square_size_aligned

            # 检查是否超出图像边界，如果超出则调整
            max_x2 = mask.shape[2]
            max_y2 = mask.shape[1]

            # 如果超出左边界，向右移动
            if x1 < 0:
                x2 = x2 - x1  # 增加右边界
                x1 = 0
            # 如果超出上边界，向下移动
            if y1 < 0:
                y2 = y2 - y1  # 增加下边界
                y1 = 0

            # 如果超出右边界，向左移动
            if x2 > max_x2:
                x1 = x1 - (x2 - max_x2)
                x2 = max_x2
                # 如果调整后左边界超出，则重新计算
                if x1 < 0:
                    x1 = 0
                    # 重新计算正方形尺寸（可能不再是8的倍数，但保证不超出边界）
                    square_size_aligned = x2 - x1
                    # 向下取整到8的倍数
                    square_size_aligned = (square_size_aligned // 8) * 8
                    if square_size_aligned < 8:
                        square_size_aligned = min(8, max_x2)
                    x2 = x1 + square_size_aligned

            # 如果超出下边界，向上移动
            if y2 > max_y2:
                y1 = y1 - (y2 - max_y2)
                y2 = max_y2
                # 如果调整后上边界超出，则重新计算
                if y1 < 0:
                    y1 = 0
                    # 重新计算正方形尺寸（可能不再是8的倍数，但保证不超出边界）
                    square_size_aligned = y2 - y1
                    # 向下取整到8的倍数
                    square_size_aligned = (square_size_aligned // 8) * 8
                    if square_size_aligned < 8:
                        square_size_aligned = min(8, max_y2)
                    y2 = y1 + square_size_aligned

            # 确保x和y方向的正方形尺寸一致（取较小的值，保证不超出边界）
            final_square_size = min(x2 - x1, y2 - y1)
            # 向下取整到8的倍数
            final_square_size = (final_square_size // 8) * 8
            if final_square_size < 8:
                final_square_size = min(8, min(max_x2 - x1, max_y2 - y1))

            # 重新以中心点为基准计算（确保mask区域居中）
            bb_center_x = (bb_x1 + bb_x2) / 2.0
            bb_center_y = (bb_y1 + bb_y2) / 2.0
            half_size = final_square_size / 2.0
            x1 = int(bb_center_x - half_size)
            y1 = int(bb_center_y - half_size)
            x2 = x1 + final_square_size
            y2 = y1 + final_square_size

            # 再次检查边界，确保不超出
            if x1 < 0:
                x2 = x2 - x1
                x1 = 0
            if y1 < 0:
                y2 = y2 - y1
                y1 = 0
            if x2 > max_x2:
                x1 = max_x2 - final_square_size
                x2 = max_x2
            if y2 > max_y2:
                y1 = max_y2 - final_square_size
                y2 = max_y2

            # 最终的正方形尺寸
            final_square_size = x2 - x1

        # crop the mask and image to square
        mask = mask[:, y1:y2, x1:x2]
        image_optional = image_optional[:, y1:y2, x1:x2, :]

        # 返回裁剪后的mask、image，以及坐标和尺寸信息（正方形）
        return (mask, image_optional, x1, y1, final_square_size, final_square_size)
