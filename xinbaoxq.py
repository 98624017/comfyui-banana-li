import hashlib
import torch
import torch.nn.functional as F
import os
import urllib.request
import time
import folder_paths
from PIL import Image, ImageOps
import numpy as np

# ==========================================
# 配置：心宝❤详情页选项
# ==========================================
TEXT_ASSEMBLER_CONFIG_FILE = "xinbao_text_assembler.toml"
TEXT_ASSEMBLER_CONFIG_PATH = os.path.join(os.path.dirname(__file__), TEXT_ASSEMBLER_CONFIG_FILE)
TEXT_ASSEMBLER_CONFIG_URL = (
    "https://gist.githubusercontent.com/98624017/d3bb8fbd42de37c8cd9eb7fac3f1e59f/raw/"
    "xinbao_text_assembler.toml"
)
TEXT_ASSEMBLER_CONFIG_PROXY_URL = (
    "https://gh-proxy.com/https://gist.githubusercontent.com/98624017/d3bb8fbd42de37c8cd9eb7fac3f1e59f/raw/"
    "xinbao_text_assembler.toml"
)

DEFAULT_USAGE_OPTIONS = ["电商详情页", "电商主图", "亚马逊详情页", "亚马逊主图"]

def _coerce_int(value, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    """
    将任意输入尽量转换为 int，并进行范围钳制。

    说明：ComfyUI 工作流中历史值可能以字符串形式保存（例如 "8"）。
    """
    try:
        # bool 也是 int 的子类，避免 True/False 被误当成 1/0
        if isinstance(value, bool):
            raise ValueError("bool is not a valid int")
        coerced = int(value)
    except Exception:
        coerced = int(default)

    if min_value is not None and coerced < min_value:
        coerced = min_value
    if max_value is not None and coerced > max_value:
        coerced = max_value
    return coerced

def _parse_toml_text(text):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        import tomllib
        return tomllib.loads(text)
    except Exception:
        pass
    try:
        import tomli
        return tomli.loads(text)
    except Exception:
        pass
    try:
        import toml
        return toml.loads(text)
    except Exception:
        return None

def _build_cache_busted_url(url):
    # 通过时间戳绕过缓存，确保拉取最新内容
    ts = int(time.time())
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}_ts={ts}"

def _fetch_text_from_url(url, timeout=8):
    try:
        request = urllib.request.Request(
            _build_cache_busted_url(url),
            headers={"User-Agent": "ComfyUI-xinbao/1.0"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
        return data.decode("utf-8")
    except Exception:
        return None

def _sync_text_assembler_config():
    # 启动时尝试从云端拉取最新版配置，失败则安静降级到本地文件
    text = _fetch_text_from_url(TEXT_ASSEMBLER_CONFIG_URL)
    if text and _parse_toml_text(text):
        try:
            with open(TEXT_ASSEMBLER_CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        return
    for _ in range(3):
        text = _fetch_text_from_url(TEXT_ASSEMBLER_CONFIG_PROXY_URL)
        if not text or not _parse_toml_text(text):
            continue
        try:
            with open(TEXT_ASSEMBLER_CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        break

_sync_text_assembler_config()

def _load_toml_file(path):
    if not os.path.exists(path):
        return None
    try:
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        pass
    try:
        import tomli
        with open(path, "rb") as f:
            return tomli.load(f)
    except Exception:
        pass
    try:
        import toml
        with open(path, "r", encoding="utf-8") as f:
            return toml.load(f)
    except Exception:
        return None

def _load_text_assembler_config():
    config = _load_toml_file(TEXT_ASSEMBLER_CONFIG_PATH) or {}
    usage_options = DEFAULT_USAGE_OPTIONS
    usage_section = config.get("usage_options")
    if isinstance(usage_section, dict):
        items = usage_section.get("items")
        if isinstance(items, list) and items and all(isinstance(item, str) and item.strip() for item in items):
            usage_options = items
    prompts_section = config.get("usage_prompts")
    if not isinstance(prompts_section, dict):
        prompts_section = {}
    return usage_options, prompts_section

def _get_prompt_value(prompt_map, key):
    value = prompt_map.get(key, "")
    return value if isinstance(value, str) else ""

def _get_usage_prompts(usage):
    _, prompts_section = _load_text_assembler_config()
    usage_prompts = prompts_section.get(usage, {})
    if not isinstance(usage_prompts, dict):
        usage_prompts = {}
    return (
        _get_prompt_value(usage_prompts, "model_id"),
        _get_prompt_value(usage_prompts, "append_prompt"),
    )

# 前端扩展目录（用于注入 web 修复逻辑）
WEB_DIRECTORY = "web"

# ==========================================
# 【公共逻辑】自动生成透明占位图
# ==========================================
GHOST_FILE_NAME = "⛔_点击这里清空图片_⛔.png"

try:
    input_dir = folder_paths.get_input_directory()
    ghost_path = os.path.join(input_dir, GHOST_FILE_NAME)
    if not os.path.exists(ghost_path):
        dummy_img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        dummy_img.save(ghost_path)
except Exception:
    pass

# ==========================================
# 节点 1: 心宝❤详情拼合 (报错图自动过滤版)
# ==========================================
class XinbaoVerticalStitch:
    def __init__(self): pass
    @classmethod
    def INPUT_TYPES(s): return {"required": {"images": ("IMAGE",)}}
    INPUT_IS_LIST = True 
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "stitch_images"
    CATEGORY = "Xinbao/Image"
    
    def stitch_images(self, images):
        if not images: return (None,)
        
        flat_image_list = []
        for img in images:
            if img is None: continue
            for i in range(img.shape[0]):
                single_img = img[i]
                h, w, c = single_img.shape
                # 1. 过滤 640x640 的报错图
                if w == 640 and h == 640 and torch.mean(single_img) > 0.9:
                    continue
                # 2. 过滤加载节点产生的 64x64 占位图
                if w == 64 and h == 64:
                    continue
                flat_image_list.append(single_img)
        
        if len(flat_image_list) == 0:
            return (torch.zeros((1, 64, 64, 3), dtype=torch.float32),)

        target_width = max(img.shape[1] for img in flat_image_list)
        
        processed_list = []
        for img in flat_image_list:
            h, w, c = img.shape
            if w != target_width:
                scale_factor = target_width / w
                new_h = int(h * scale_factor)
                img_permuted = img.permute(2, 0, 1).unsqueeze(0)
                img_permuted = F.interpolate(img_permuted, size=(new_h, target_width), mode="bilinear", align_corners=False)
                img = img_permuted.squeeze(0).permute(1, 2, 0)
            processed_list.append(img)

        long_image = torch.cat(processed_list, dim=0)
        return (long_image.unsqueeze(0),)

# ==========================================
# 节点 2: 心宝❤智能拼图
# ==========================================
class XinbaoSmartGrid:
    def __init__(self): pass
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {}, "optional": {"image_1": ("IMAGE",), "image_2": ("IMAGE",), "image_3": ("IMAGE",), "image_4": ("IMAGE",)}}
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "smart_merge"
    CATEGORY = "Xinbao/Image"
    
    def resize_width(self, img, target_width):
        h, w, c = img.shape
        if w == target_width: return img
        scale = target_width / w
        new_h = int(h * scale)
        img = img.permute(2, 0, 1).unsqueeze(0)
        img = F.interpolate(img, size=(new_h, target_width), mode="bilinear", align_corners=False)
        return img.squeeze(0).permute(1, 2, 0)

    def resize_height(self, img, target_height):
        h, w, c = img.shape
        if h == target_height: return img
        scale = target_height / h
        new_w = int(w * scale)
        img = img.permute(2, 0, 1).unsqueeze(0)
        img = F.interpolate(img, size=(target_height, new_w), mode="bilinear", align_corners=False)
        return img.squeeze(0).permute(1, 2, 0)

    def smart_merge(self, image_1=None, image_2=None, image_3=None, image_4=None):
        raw_inputs = [image_1, image_2, image_3, image_4]
        valid_images = []
        for img in raw_inputs:
            if img is not None:
                for i in range(img.shape[0]):
                    if img[i].shape[1] > 64: # 过滤占位小图
                        valid_images.append(img[i])
        count = len(valid_images)
        if count == 0: return (torch.zeros((1, 64, 64, 3), dtype=torch.float32),)
        if count == 1: return (valid_images[0].unsqueeze(0),)
        if count == 2 or count == 3:
            max_w = max(img.shape[1] for img in valid_images)
            processed = [self.resize_width(img, max_w) for img in valid_images]
            return (torch.cat(processed, dim=0).unsqueeze(0),)
        if count >= 4:
            imgs = valid_images[:4]
            img1, img2 = imgs[0], self.resize_height(imgs[1], imgs[0].shape[0])
            row1 = torch.cat([img1, img2], dim=1)
            img3, img4 = imgs[2], self.resize_height(imgs[3], imgs[2].shape[0])
            row2 = torch.cat([img3, img4], dim=1)
            row2 = self.resize_width(row2, row1.shape[1])
            return (torch.cat([row1, row2], dim=0).unsqueeze(0),)
        return (None,)

# ==========================================
# 节点 3: 心宝❤图片加载 (占位刷新优化版)
# ==========================================
class XinbaoLoadImageClean:
    def __init__(self): pass
    @classmethod
    def INPUT_TYPES(s):
        input_dir = folder_paths.get_input_directory()
        files = sorted([f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f))])
        if GHOST_FILE_NAME in files: files.remove(GHOST_FILE_NAME)
        return {"required": {"image": ([GHOST_FILE_NAME] + files, {"image_upload": True})}}
    
    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "load_image"
    CATEGORY = "Xinbao/Image"

    def load_image(self, image):
        if image == GHOST_FILE_NAME:
            return (torch.zeros((1, 64, 64, 3), dtype=torch.float32), torch.zeros((64, 64), dtype=torch.float32))
        try:
            image_path = folder_paths.get_annotated_filepath(image)
            i = ImageOps.exif_transpose(Image.open(image_path))
            image_out = torch.from_numpy(np.array(i.convert("RGB")).astype(np.float32) / 255.0)[None,]
            mask_out = 1.0 - torch.from_numpy(np.array(i.getchannel('A')).astype(np.float32) / 255.0) if 'A' in i.getbands() else torch.zeros((64, 64), dtype=torch.float32)
            return (image_out, mask_out)
        except Exception:
            return (torch.zeros((1, 64, 64, 3), dtype=torch.float32), torch.zeros((64, 64), dtype=torch.float32))

    @classmethod
    def IS_CHANGED(s, image):
        if image == GHOST_FILE_NAME:
            return ""
        image_path = folder_paths.get_annotated_filepath(image)
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        return m.digest().hex()

    @classmethod
    def VALIDATE_INPUTS(s, image):
        if image == GHOST_FILE_NAME:
            return True
        if not folder_paths.exists_annotated_filepath(image):
            return "Invalid image file: {}".format(image)
        return True

# ==========================================
# 节点 4: 心宝❤详情页选项 (特殊特征版)
# ==========================================
class XinbaoTextAssembler:
    @classmethod
    def INPUT_TYPES(cls):
        usage_options, _ = _load_text_assembler_config()
        return {
            "required": {
                "产品名": ("STRING", {"default": "", "multiline": False, "placeholder": "例：女士睡衣"}),
                # 改为自定义输入：允许用户输入任意语言描述（如 中文/英文/日文/en-US 等）
                "语言": ("STRING", {"default": "中文", "multiline": False, "placeholder": "例：中文 / 英文 / 日文"}),
                # 改为整数输入：限制 1-25，默认 8
                "数量": ("INT", {"default": 8, "min": 1, "max": 25, "step": 1, "display": "数量(1-25)"}),
                "用途": (usage_options,),
                "比例": (["Auto", "1:1", "9:16", "16:9", "21:9", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4"], {"default": "9:16"}),
                "特殊特征": ("STRING", {"default": "", "multiline": True, "placeholder": "在这里输入特殊特征..."}),
                "卖点文案": ("STRING", {"default": "", "multiline": True, "placeholder": "卖点文案"}),
            }
        }
    RETURN_TYPES = ("STRING", "STRING", "*", "STRING")
    RETURN_NAMES = ("user_prompt", "aspect_ratio", "model_id", "追加提示词")
    FUNCTION = "assemble"
    CATEGORY = "Xinbao/Text"

    def assemble(self, 产品名, 语言, 数量, 用途, 比例, 特殊特征, 卖点文案):
        language_value = str(语言 or "").strip() or "中文"
        count_value = _coerce_int(数量, default=8, min_value=1, max_value=25)

        line2 = f"帮我设计一个图1的【{产品名}】的提示词" if 产品名.strip() else "帮我设计一个图1产品的提示词"
        lines = [
            "图1是我的产品的不同角度的图，图2是我需要指定的配色风格以及字体",
            line2,
            f"生成【{count_value}】张图片，输出【{language_value}】，用途【{用途}】，比例【{比例}】",
            f"【特殊特征】{特殊特征}",
            f"【卖点】{卖点文案}"
        ]
        model_id, append_prompt = _get_usage_prompts(用途)
        return ("\n".join(lines), 比例, str(model_id), append_prompt)

# ==========================================
# 节点 5: 心宝❤图片拆分
# ==========================================
class XinbaoImageSplitter:
    def __init__(self): pass
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"image": ("IMAGE",), "split_mode": (["四方格 (2x2)", "九宫格 (3x3)"], {"default": "四方格 (2x2)"})}}
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "split_image"
    CATEGORY = "Xinbao/Image"
    def split_image(self, image, split_mode):
        if image.shape[1] <= 64: return (image,) # 忽略占位图
        if image.shape[0] > 1: image = image[0].unsqueeze(0)
        b, h, w, c = image.shape
        rows, cols = (2, 2) if "2x2" in split_mode else (3, 3)
        h_s, w_s = h // rows, w // cols
        crops = [image[:, r*h_s:(r+1)*h_s, c*w_s:(c+1)*w_s, :] for r in range(rows) for c in range(cols)]
        return (torch.cat(crops, dim=0),)

# ==========================================
# 注册映射
# ==========================================
NODE_CLASS_MAPPINGS = {
    "XinbaoVerticalStitch": XinbaoVerticalStitch,
    "XinbaoSmartGrid": XinbaoSmartGrid,
    "XinbaoLoadImageClean": XinbaoLoadImageClean,
    "TextAssembler": XinbaoTextAssembler,
    "XinbaoImageSplitter": XinbaoImageSplitter
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "XinbaoVerticalStitch": "心宝❤详情拼合",
    "XinbaoSmartGrid": "心宝❤智能拼图",
    "XinbaoLoadImageClean": "心宝❤图片加载",
    "TextAssembler": "心宝❤详情页选项",
    "XinbaoImageSplitter": "心宝❤图片拆分"
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
