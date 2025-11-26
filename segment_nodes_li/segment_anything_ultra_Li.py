# layerstyle advance

from .imagefunc import *
from .segment_anything_func import *

NODE_NAME = "SegmentAnythingUltra Li"

# 全局模型缓存字典
global_sam_models = {}
global_dino_models = {}


class SegmentAnythingUltraLi:
    def __init__(self):
        self.SAM_MODEL = None
        self.DINO_MODEL = None
        self.previous_sam_model = ""
        self.previous_dino_model = ""
        pass

    @classmethod
    def INPUT_TYPES(cls):
        method_list = [
            "VITMatte",
            "VITMatte(local)",
            "PyMatting",
            "GuidedFilter",
        ]
        device_list = ["cuda", "cpu"]
        return {
            "required": {
                "image": ("IMAGE",),
                "sam_model": (list_sam_model(),),
                "grounding_dino_model": (list_groundingdino_model(),),
                "threshold": (
                    "FLOAT",
                    {"default": 0.3, "min": 0, "max": 1.0, "step": 0.01},
                ),
                "detail_method": (method_list,),
                "detail_erode": (
                    "INT",
                    {"default": 6, "min": 1, "max": 255, "step": 1},
                ),
                "detail_dilate": (
                    "INT",
                    {"default": 6, "min": 1, "max": 255, "step": 1},
                ),
                "black_point": (
                    "FLOAT",
                    {
                        "default": 0.15,
                        "min": 0.01,
                        "max": 0.98,
                        "step": 0.01,
                        "display": "slider",
                    },
                ),
                "white_point": (
                    "FLOAT",
                    {
                        "default": 0.99,
                        "min": 0.02,
                        "max": 0.99,
                        "step": 0.01,
                        "display": "slider",
                    },
                ),
                "process_detail": ("BOOLEAN", {"default": True}),
                "prompt": ("STRING", {"default": "subject"}),
                "device": (device_list,),
                "max_megapixels": (
                    "FLOAT",
                    {"default": 2.0, "min": 1, "max": 999, "step": 0.1},
                ),
                "cache_model": ("BOOLEAN", {"default": False}),
                "use_global_model": ("BOOLEAN", {"default": True}),
            },
            "optional": {},
        }

    RETURN_TYPES = (
        "IMAGE",
        "MASK",
        "MASK",
    )
    RETURN_NAMES = (
        "image",
        "mask_union",
        "mask_list",
    )
    FUNCTION = "segment_anything_ultra_li"
    CATEGORY = "❤️‍🔥心宝专用/分割"

    def _load_models(self, sam_model, grounding_dino_model, use_global_model):
        """加载模型（全局缓存或实例级缓存）"""
        global global_sam_models, global_dino_models

        if use_global_model:
            # 全局模型缓存
            if sam_model not in global_sam_models:
                global_sam_models[sam_model] = load_sam_model(sam_model)
            self.SAM_MODEL = global_sam_models[sam_model]

            if grounding_dino_model not in global_dino_models:
                global_dino_models[grounding_dino_model] = load_groundingdino_model(
                    grounding_dino_model
                )
            self.DINO_MODEL = global_dino_models[grounding_dino_model]
        else:
            # 实例级模型缓存
            if self.previous_sam_model != sam_model or self.SAM_MODEL is None:
                self.SAM_MODEL = load_sam_model(sam_model)
                self.previous_sam_model = sam_model
            if (
                self.previous_dino_model != grounding_dino_model
                or self.DINO_MODEL is None
            ):
                self.DINO_MODEL = load_groundingdino_model(grounding_dino_model)
                self.previous_dino_model = grounding_dino_model

    def _split_prompts(self, prompt):
        """分割 prompt，按逗号分割"""
        prompt_list = [p.strip() for p in prompt.split(",") if p.strip()]
        return prompt_list if len(prompt_list) > 0 else ["subject"]

    def _process_mask_detail(
        self,
        mask,
        img_tensor,
        image_pil,
        detail_method,
        detail_erode,
        detail_dilate,
        black_point,
        white_point,
        process_detail,
        local_files_only,
        device,
        max_megapixels,
    ):
        """处理 mask 细节"""
        detail_range = detail_erode + detail_dilate

        if detail_method == "GuidedFilter":
            mask = guided_filter_alpha(img_tensor, mask, detail_range // 6 + 1)
            mask = tensor2pil(histogram_remap(mask, black_point, white_point))
        elif detail_method == "PyMatting":
            mask = tensor2pil(
                mask_edge_detail(
                    img_tensor, mask, detail_range // 8 + 1, black_point, white_point
                )
            )
        else:  # VITMatte
            trimap = generate_VITMatte_trimap(mask, detail_erode, detail_dilate)
            mask = generate_VITMatte(
                image_pil,
                trimap,
                local_files_only=local_files_only,
                device=device,
                max_megapixels=max_megapixels,
            )
            mask = tensor2pil(
                histogram_remap(pil2tensor(mask), black_point, white_point)
            )
        return mask

    def _process_single_prompt(
        self,
        image_pil,
        img_tensor,
        single_prompt,
        threshold,
        detail_method,
        detail_erode,
        detail_dilate,
        black_point,
        white_point,
        process_detail,
        local_files_only,
        device,
        max_megapixels,
    ):
        """处理单个 prompt，返回 mask tensor 或 None"""
        boxes = groundingdino_predict(
            self.DINO_MODEL, image_pil, single_prompt, threshold
        )
        if boxes.shape[0] == 0:
            return None

        (_, _mask) = sam_segment(self.SAM_MODEL, image_pil, boxes)
        if _mask is None or len(_mask) == 0:
            return None

        _mask = _mask[0]
        if process_detail:
            _mask = self._process_mask_detail(
                _mask,
                img_tensor,
                image_pil,
                detail_method,
                detail_erode,
                detail_dilate,
                black_point,
                white_point,
                process_detail,
                local_files_only,
                device,
                max_megapixels,
            )
        else:
            _mask = mask2image(_mask)

        return image2mask(_mask)  # 返回 (1, H, W)

    def _calculate_mask_union(self, image_masks):
        """计算 mask 并集"""
        if len(image_masks) == 0:
            return None

        union_mask = image_masks[0].squeeze(0)  # (H, W)
        for mask in image_masks[1:]:
            mask_2d = mask.squeeze(0)  # (H, W)
            union_mask = torch.maximum(union_mask, mask_2d)
        return union_mask.unsqueeze(0)  # (1, H, W)

    def _process_single_image(
        self,
        img,
        prompt_list,
        threshold,
        detail_method,
        detail_erode,
        detail_dilate,
        black_point,
        white_point,
        process_detail,
        local_files_only,
        device,
        max_megapixels,
    ):
        """处理单张图片的所有 prompt"""
        img = torch.unsqueeze(img, 0)
        img = pil2tensor(tensor2pil(img).convert("RGB"))
        image_pil = tensor2pil(img).convert("RGBA")

        # 处理每个 prompt
        image_masks = []
        for single_prompt in prompt_list:
            mask_tensor = self._process_single_prompt(
                image_pil,
                img,
                single_prompt,
                threshold,
                detail_method,
                detail_erode,
                detail_dilate,
                black_point,
                white_point,
                process_detail,
                local_files_only,
                device,
                max_megapixels,
            )
            if mask_tensor is not None:
                image_masks.append(mask_tensor)

        # 计算并集和列表
        _, height, width, _ = img.size()
        empty_mask = torch.zeros((1, height, width), dtype=torch.float32, device="cpu")

        if len(image_masks) == 0:
            union_mask = empty_mask
            mask_list_tensor = empty_mask.unsqueeze(0)  # (1, 1, H, W)
        else:
            union_mask = self._calculate_mask_union(image_masks)  # (1, H, W)
            mask_list_tensor = torch.stack(image_masks, dim=0)  # (N, 1, H, W)

        # 创建最终图像
        final_mask_pil = mask2image(union_mask)
        final_image = RGB2RGBA(
            tensor2pil(img).convert("RGB"), final_mask_pil.convert("L")
        )

        return pil2tensor(final_image), union_mask, mask_list_tensor

    def _merge_mask_lists(self, ret_mask_list):
        """合并所有图片的 mask_list"""
        if not ret_mask_list:
            return None

        max_mask_count = max([mask_list.shape[0] for mask_list in ret_mask_list])

        padded_mask_lists = []
        for mask_list in ret_mask_list:
            mask_count = mask_list.shape[0]
            if len(mask_list.shape) == 4:
                _, _, H, W = mask_list.shape
            else:
                _, H, W = mask_list.shape
                mask_list = mask_list.unsqueeze(1)  # (N, 1, H, W)

            if mask_count < max_mask_count:
                empty_mask = torch.zeros((1, H, W), dtype=torch.float32, device="cpu")
                padding = empty_mask.unsqueeze(0).repeat(
                    max_mask_count - mask_count, 1, 1, 1
                )
                padded_mask_list = torch.cat([mask_list, padding], dim=0)
            else:
                padded_mask_list = mask_list
            padded_mask_lists.append(padded_mask_list)

        # 合并: (batch_size, max_mask_count, 1, H, W) -> (batch_size * max_mask_count, H, W)
        combined_mask_list = torch.stack(padded_mask_lists, dim=0)
        combined_mask_list = combined_mask_list.squeeze(
            2
        )  # (batch_size, max_mask_count, H, W)
        combined_mask_list = combined_mask_list.view(
            -1, combined_mask_list.shape[2], combined_mask_list.shape[3]
        )
        return combined_mask_list

    def _clear_model_cache(self, cache_model, use_global_model):
        """清理模型缓存"""
        if not cache_model:
            if not use_global_model:
                self.SAM_MODEL = None
                self.DINO_MODEL = None
                self.previous_sam_model = ""
                self.previous_dino_model = ""
            clear_memory()

    def segment_anything_ultra_li(
        self,
        image,
        sam_model,
        grounding_dino_model,
        threshold,
        detail_method,
        detail_erode,
        detail_dilate,
        black_point,
        white_point,
        process_detail,
        prompt,
        device,
        max_megapixels,
        cache_model,
        use_global_model,
    ):
        # 加载模型
        self._load_models(sam_model, grounding_dino_model, use_global_model)

        # 分割 prompt
        prompt_list = self._split_prompts(prompt)
        local_files_only = detail_method == "VITMatte(local)"

        # 处理每张图片
        ret_images = []
        ret_mask_union = []
        ret_mask_list = []

        for img in image:
            final_image, union_mask, mask_list_tensor = self._process_single_image(
                img,
                prompt_list,
                threshold,
                detail_method,
                detail_erode,
                detail_dilate,
                black_point,
                white_point,
                process_detail,
                local_files_only,
                device,
                max_megapixels,
            )
            ret_images.append(final_image)
            ret_mask_union.append(union_mask)
            ret_mask_list.append(mask_list_tensor)

        # 清理模型缓存
        self._clear_model_cache(cache_model, use_global_model)

        # 处理空结果
        if len(ret_mask_union) == 0:
            _, height, width, _ = image.size()
            empty_mask = torch.zeros(
                (1, height, width), dtype=torch.float32, device="cpu"
            )
            return (empty_mask, empty_mask, empty_mask)

        # 合并结果
        mask_union_tensor = torch.cat(ret_mask_union, dim=0)  # (batch_size, H, W)
        combined_mask_list = self._merge_mask_lists(ret_mask_list)

        log(
            f"{NODE_NAME} Processed {len(ret_mask_union)} image(s) with {len(prompt_list)} prompt(s).",
            message_type="finish",
        )
        return (torch.cat(ret_images, dim=0), mask_union_tensor, combined_mask_list)


NODE_CLASS_MAPPINGS = {
    "LayerMask: SegmentAnythingUltra Li": SegmentAnythingUltraLi,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LayerMask: SegmentAnythingUltra Li": "图像分割-联合❤️‍🔥心宝专用",
}
