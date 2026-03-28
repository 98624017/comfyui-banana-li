"""
心宝❤批量详情图保存节点

功能概览：
1) 替换 ComfyUI 内置 SaveImage：保存批量图片到 output 目录；
2) 缓存每张图的提示词/版本列表（仅内存，不落盘）；
3) 缓存参考图 URL（优先）与 base64（兜底），供局部重试复用；
4) 缓存 API Key（仅内存，不回传前端），用于“重试选中”并发生成；
5) 拼接图默认不自动保存：由前端按钮触发后端保存一张拼接图。

版本策略：
- 每个位置最多保留 10 个版本；
- 仅 UI/缓存层回收，不删除磁盘文件（避免危险操作）。
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import requests
import torch
from aiohttp import web
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import folder_paths
from comfy.cli_args import args

from api_client import GeminiApiClient
from banana_kv_auth import EdgeKVAuthClient
from config_manager import ConfigManager
from image_codec import ErrorCanvas, ImageCodec
from image_uploader import ImageUploader
from logger import logger


MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

_CONFIG_MANAGER = ConfigManager(MODULE_DIR)
_API_CLIENT = GeminiApiClient(
    _CONFIG_MANAGER,
    logger,
    interrupt_checker=None,  # 后端路由不绑定 ComfyUI cancel
)
_IMAGE_CODEC = ImageCodec(logger_instance=logger, ensure_not_interrupted=None)
_ERROR_CANVAS = ErrorCanvas(logger)
_KV_AUTH_CLIENT = EdgeKVAuthClient(_CONFIG_MANAGER, logger)

_SAVE_LOCK = threading.Lock()


def _now_ts() -> float:
    return time.time()


def _as_first(value: Any) -> Any:
    """INPUT_IS_LIST=True 时，ComfyUI 会把输入包成 list；这里统一取第一个值。"""
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value


def _flatten_sequence(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        flattened: List[Any] = []
        for item in value:
            if isinstance(item, (list, tuple)):
                flattened.extend(item)
            elif item is not None:
                flattened.append(item)
        return flattened
    return [value]


def _flatten_image_samples(value: Any) -> List[torch.Tensor]:
    """
    将 ComfyUI IMAGE 输入归一化为“按张拆开的 3D Tensor 列表”。
    支持：
    - 单张 (H,W,C)
    - batch (N,H,W,C)
    - list/tuple（以及 list[list]）
    """
    items = _flatten_sequence(value)
    samples: List[torch.Tensor] = []
    for item in items:
        if item is None or not isinstance(item, torch.Tensor):
            continue
        if item.ndim == 3:
            samples.append(item)
            continue
        if item.ndim == 4:
            for i in range(int(item.shape[0])):
                samples.append(item[i])
    return samples


def _normalize_prompt_batch(value: Any) -> List[str]:
    """
    支持：
    - list[str]：直接使用（包含 INPUT_IS_LIST 的 list[list]）
    - str：按换行拆分（去空行）
    """
    if value is None:
        return []
    if isinstance(value, str):
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        return [line.strip() for line in normalized.split("\n") if line.strip()]

    prompts: List[str] = []
    for item in _flatten_sequence(value):
        if item is None:
            continue
        if isinstance(item, str):
            prompts.append(item)
        else:
            prompts.append(str(item))
    return prompts


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _mask_key(value: Optional[str]) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return "未填写"
    if len(cleaned) <= 8:
        return cleaned
    return f"{cleaned[:3]}***{cleaned[-3:]}"


def _resolve_effective_base_url(route_choice: str) -> str:
    if route_choice == "心宝❤新渠道":
        # 与 BananaImageNode 保持一致（脱敏后固定）
        return base64.b64decode("aHR0cDovL3hpbmJhb2FwaS5kcGRucy5vcmc=").decode("utf-8")
    return _CONFIG_MANAGER.get_route_base_url(route_choice)


def _resolve_api_key(
    raw_input_key: str,
    route_choice: str,
) -> Tuple[Optional[str], str, str]:
    """
    解析 api_key（节点输入优先，其次 config.ini）。
    返回：(api_key_or_none, base_url, source_label)
    """
    candidate = (raw_input_key or "").strip()
    base_url = _resolve_effective_base_url(route_choice)

    lowered = candidate.lower()
    if lowered.startswith("fixsk-"):
        stripped = candidate[len("fix") :]
        hidden_key = _CONFIG_MANAGER.sanitize_api_key(stripped)
        if hidden_key:
            return hidden_key, _CONFIG_MANAGER.get_hidden_base_url(), "隐藏线路临时密钥"

    sanitized_input = _CONFIG_MANAGER.sanitize_api_key(candidate)
    if sanitized_input:
        return sanitized_input, base_url, "节点/全局输入"

    config_key = _CONFIG_MANAGER.sanitize_api_key(_CONFIG_MANAGER.load_api_key())
    if config_key:
        return config_key, base_url, "config.ini 兜底"

    return None, base_url, "缺少密钥"


def _is_pro_image_model(model_type: str) -> bool:
    normalized = (model_type or "").strip().lower()
    return normalized in {"gemini-3-pro-image-preview", "gemini-3-pro-image"}


def _build_request_timeout(model_type: str) -> Tuple[float, float]:
    connect_timeout = 15.0
    read_timeout = 420.0 if _is_pro_image_model(model_type) else 120.0
    return (connect_timeout, read_timeout)


def _extract_upstream_node_inputs(prompt_graph: Any, self_node_id: str) -> Dict[str, Any]:
    """
    从 PROMPT 图中，尝试找到本节点 images 输入对应的上游节点（通常是 BananaImageNodeV2）。
    若解析失败，返回空 dict。
    """
    if not isinstance(prompt_graph, dict) or not self_node_id:
        return {}
    self_entry = prompt_graph.get(str(self_node_id))
    if not isinstance(self_entry, dict):
        return {}
    inputs = self_entry.get("inputs") or {}
    image_link = inputs.get("images")
    # ComfyUI link 格式通常为 [node_id, output_index]
    if not isinstance(image_link, (list, tuple)) or len(image_link) < 1:
        return {}
    upstream_id = str(image_link[0])
    upstream_entry = prompt_graph.get(upstream_id)
    if not isinstance(upstream_entry, dict):
        return {}
    return upstream_entry.get("inputs") or {}


def _select_reference_sources(reference_urls: List[str], reference_b64: List[str]) -> List[str]:
    """
    优先使用 URL（符合用户选择的 A），若缺失则回退 base64（保证可用性）。
    """
    urls = [u.strip() for u in (reference_urls or []) if isinstance(u, str) and u.strip()]
    if urls:
        return urls
    b64s = [b.strip() for b in (reference_b64 or []) if isinstance(b, str) and b.strip()]
    return b64s


@dataclass
class _ImageFileRef:
    filename: str
    subfolder: str
    type: str

    def to_dict(self) -> Dict[str, str]:
        return {"filename": self.filename, "subfolder": self.subfolder, "type": self.type}


@dataclass
class _ImageVersion:
    version_id: str
    created_at: float
    prompt: str
    image: _ImageFileRef
    status: str  # success|failed
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_id": self.version_id,
            "created_at": self.created_at,
            "prompt": self.prompt,
            "image": self.image.to_dict(),
            "status": self.status,
            "error_message": self.error_message,
        }


@dataclass
class _ImageSlot:
    index: int
    active_version_id: str
    versions: List[_ImageVersion]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "active_version_id": self.active_version_id,
            "versions": [v.to_dict() for v in self.versions],
        }

    def get_active(self) -> Optional[_ImageVersion]:
        for v in self.versions:
            if v.version_id == self.active_version_id:
                return v
        return self.versions[-1] if self.versions else None


@dataclass
class _BatchDetailSession:
    node_id: str
    created_at: float
    filename_prefix: str
    slots: List[_ImageSlot]
    # 生成上下文（用于重试）
    route_choice: str
    model_type: str
    aspect_ratio: str
    image_size: str
    top_p: float
    enable_google_search: bool
    bypass_proxy: bool
    verify_ssl: bool
    limit_long_edge_str: str
    api_key: Optional[str]
    api_key_source: str
    base_url: str
    reference_urls: List[str]
    reference_b64: List[str]
    lock: threading.Lock

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "created_at": self.created_at,
            "filename_prefix": self.filename_prefix,
            "route_choice": self.route_choice,
            "model_type": self.model_type,
            "aspect_ratio": self.aspect_ratio,
            "image_size": self.image_size,
            "top_p": self.top_p,
            "enable_google_search": self.enable_google_search,
            "bypass_proxy": self.bypass_proxy,
            "verify_ssl": self.verify_ssl,
            "limit_long_edge_str": self.limit_long_edge_str,
            "api_key_masked": _mask_key(self.api_key),
            "api_key_source": self.api_key_source,
            "reference_urls": list(self.reference_urls or []),
            "slots": [s.to_dict() for s in self.slots],
        }


_SESSIONS_LOCK = threading.Lock()
_SESSIONS: Dict[str, _BatchDetailSession] = {}
_SESSION_LIMIT = 16
_VERSION_LIMIT = 10


def _prune_sessions() -> None:
    with _SESSIONS_LOCK:
        if len(_SESSIONS) <= _SESSION_LIMIT:
            return
        items = sorted(_SESSIONS.items(), key=lambda kv: kv[1].created_at)
        while len(items) > _SESSION_LIMIT:
            oldest_key, _ = items.pop(0)
            _SESSIONS.pop(oldest_key, None)


def _get_session(node_id: str) -> Optional[_BatchDetailSession]:
    if not node_id:
        return None
    with _SESSIONS_LOCK:
        return _SESSIONS.get(str(node_id))


def _set_session(session: _BatchDetailSession) -> None:
    with _SESSIONS_LOCK:
        _SESSIONS[str(session.node_id)] = session
    _prune_sessions()


def _build_png_metadata(prompt: Any, extra_pnginfo: Any) -> Optional[PngInfo]:
    if args.disable_metadata:
        return None
    metadata = PngInfo()
    if prompt is not None:
        try:
            metadata.add_text("prompt", json.dumps(prompt, ensure_ascii=False))
        except Exception:
            metadata.add_text("prompt", json.dumps(str(prompt), ensure_ascii=False))

    if isinstance(extra_pnginfo, dict):
        for key, val in extra_pnginfo.items():
            try:
                metadata.add_text(str(key), json.dumps(val, ensure_ascii=False))
            except Exception:
                metadata.add_text(str(key), json.dumps(str(val), ensure_ascii=False))
    return metadata


def _save_image_samples(
    image_samples: List[torch.Tensor],
    filename_prefix: str,
    prompt: Any,
    extra_pnginfo: Any,
) -> Tuple[List[_ImageFileRef], str]:
    if not image_samples:
        raise RuntimeError("未收到任何可保存的图片数据，请检查上游节点输出")

    first = image_samples[0]
    if not isinstance(first, torch.Tensor) or first.ndim != 3:
        raise RuntimeError("图片数据格式异常：期望单张图片为 (H,W,C) 张量")

    filename_prefix = (filename_prefix or "ComfyUI").strip() or "ComfyUI"
    height = int(first.shape[0])
    width = int(first.shape[1])

    with _SAVE_LOCK:
        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix,
            folder_paths.get_output_directory(),
            width,
            height,
        )
        metadata = _build_png_metadata(prompt, extra_pnginfo)

        results: List[_ImageFileRef] = []
        for batch_number, sample in enumerate(image_samples):
            arr = 255.0 * sample.detach().cpu().numpy()
            img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
            filename_with_batch_num = filename.replace("%batch_num%", str(batch_number))
            file = f"{filename_with_batch_num}_{counter:05}_.png"
            img.save(os.path.join(full_output_folder, file), pnginfo=metadata, compress_level=4)
            results.append(_ImageFileRef(filename=file, subfolder=subfolder, type="output"))
            counter += 1

    return results, full_output_folder


def _tensor_to_png_bytes(tensor_3d: torch.Tensor) -> bytes:
    if tensor_3d is None or not isinstance(tensor_3d, torch.Tensor) or tensor_3d.ndim != 3:
        raise ValueError("期望单张 (H,W,C) 图片张量用于编码")
    arr = 255.0 * tensor_3d.detach().cpu().numpy()
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _prepare_reference_payload(
    ref_tensors: List[torch.Tensor],
    resize_limit: Optional[int],
) -> Tuple[List[str], List[str]]:
    """
    生成参考图 URL（优先）与 base64（兜底）列表。
    返回：(urls, b64_list)
    """
    urls: List[str] = []
    b64_list: List[str] = []
    if not ref_tensors:
        return urls, b64_list

    uploader = ImageUploader(logger=logger, interrupt_checker=None)

    for tensor in ref_tensors:
        try:
            if tensor is None or not isinstance(tensor, torch.Tensor):
                continue
            sample = tensor[0] if tensor.ndim == 4 else tensor
            if not isinstance(sample, torch.Tensor) or sample.ndim != 3:
                continue
            png_bytes = _tensor_to_png_bytes(sample)
            b64_list.append(base64.b64encode(png_bytes).decode())
            try:
                url = uploader.upload(png_bytes, resize_limit=resize_limit)
                if isinstance(url, str) and url.strip().startswith("http"):
                    urls.append(url.strip())
            except Exception as exc:
                logger.warning(f"参考图上传失败（将回退 base64 方式用于重试）：{exc}")
        except Exception as exc:
            logger.warning(f"参考图处理失败：{exc}")

    return urls, b64_list


def _load_image_from_file_ref(file_ref: _ImageFileRef) -> Image.Image:
    output_dir = folder_paths.get_output_directory()
    subfolder = (file_ref.subfolder or "").strip().replace("\\", "/").strip("/")
    if subfolder:
        path = os.path.join(output_dir, subfolder, file_ref.filename)
    else:
        path = os.path.join(output_dir, file_ref.filename)
    # Windows 下避免文件句柄长期占用：加载后立刻 copy 并关闭文件
    with Image.open(path) as img:
        return img.copy()


def _stitch_vertical(
    images: Sequence[Image.Image],
    background: Tuple[int, int, int] = (255, 255, 255),
) -> Image.Image:
    if not images:
        raise ValueError("没有可拼接的图片")

    normalized: List[Image.Image] = []
    max_w = 0
    total_h = 0
    for img in images:
        if img.mode != "RGB":
            img = img.convert("RGB")
        normalized.append(img)
        w, h = img.size
        max_w = max(max_w, w)
        total_h += h

    stitched = Image.new("RGB", (max_w, total_h), color=background)
    y = 0
    for img in normalized:
        stitched.paste(img, (0, y))
        y += img.size[1]
    return stitched


def _save_single_tensor(
    tensor_3d: torch.Tensor,
    filename_prefix: str,
    prompt: Any = None,
    extra_pnginfo: Any = None,
) -> _ImageFileRef:
    if tensor_3d is None or not isinstance(tensor_3d, torch.Tensor) or tensor_3d.ndim != 3:
        raise ValueError("保存失败：需要单张 (H,W,C) 图片张量")

    filename_prefix = (filename_prefix or "ComfyUI").strip() or "ComfyUI"
    height = int(tensor_3d.shape[0])
    width = int(tensor_3d.shape[1])

    with _SAVE_LOCK:
        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix,
            folder_paths.get_output_directory(),
            width,
            height,
        )
        metadata = _build_png_metadata(prompt, extra_pnginfo)
        arr = 255.0 * tensor_3d.detach().cpu().numpy()
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        file = f"{filename}_{counter:05}_.png"
        img.save(os.path.join(full_output_folder, file), pnginfo=metadata, compress_level=4)
        return _ImageFileRef(filename=file, subfolder=subfolder, type="output")


def _append_version(slot: _ImageSlot, version: _ImageVersion, set_active: bool) -> None:
    slot.versions.append(version)
    if set_active:
        slot.active_version_id = version.version_id
    # 仅回收 UI/缓存，不删除磁盘文件
    while len(slot.versions) > _VERSION_LIMIT:
        slot.versions.pop(0)


def _find_slot(session: _BatchDetailSession, index: int) -> Optional[_ImageSlot]:
    for slot in session.slots:
        if slot.index == index:
            return slot
    return None


def _generate_one(
    session: _BatchDetailSession,
    prompt_text: str,
) -> Tuple[bool, Optional[torch.Tensor], str]:
    """
    调用 API 生成单张图片。
    返回：(success, tensor3d_or_none, message)
    """
    api_key = session.api_key
    if not api_key:
        return (
            False,
            None,
            "缺少 Banana API Key：请在节点或“心宝❤全局密钥管理”中填写，或在 config.ini 中配置后重试",
        )

    route_choice = session.route_choice
    model_type = session.model_type
    mapped_model_type = model_type
    is_test_route = route_choice == "心宝❤新渠道"

    if model_type == "gemini-3-pro-image-preview-vip(高成本)":
        if not is_test_route:
            return False, None, "该 VIP 模型仅支持在“心宝❤新渠道”下使用，请切换线路或更换模型。"
        mapped_model_type = "gemini-3-pro-image-preview-vip"

    # 测试渠道跳过 KV 验证（与 BananaImageNode 一致）
    if not is_test_route:
        kv_result = _KV_AUTH_CLIENT.check(
            api_key,
            verify_ssl=session.verify_ssl,
            disable_ssl_override=not session.verify_ssl,
        )
        if not kv_result.allowed:
            msg = kv_result.user_message or "服务暂不可用，请稍后重试或检查密钥配置"
            return False, None, msg

    timeout = _build_request_timeout(model_type)
    reference_payload = _select_reference_sources(session.reference_urls, session.reference_b64)

    request_data = _API_CLIENT.create_request_data(
        prompt=prompt_text,
        seed=-1,
        aspect_ratio=session.aspect_ratio,
        top_p=float(session.top_p),
        input_images_b64=reference_payload,
        model_type=model_type,
        image_size=session.image_size,
        enable_google_search=bool(session.enable_google_search),
    )

    if is_test_route:
        generation_config = request_data.setdefault("generationConfig", {})
        image_config = generation_config.setdefault("imageConfig", {})
        image_config["output"] = "url"

    try:
        response_data = _API_CLIENT.send_request(
            api_key,
            request_data,
            mapped_model_type,
            session.base_url,
            timeout,
            bypass_proxy=bool(session.bypass_proxy),
            verify_ssl=bool(session.verify_ssl),
            verbose_error=is_test_route,
        )
        images, text_content = _API_CLIENT.extract_content(response_data)
        if not images:
            reason = (text_content or "").strip() or "未返回任何图片"
            return False, None, reason

        decoded_items: List[Union[str, bytes]] = []
        for item in images:
            if isinstance(item, str) and item.startswith("http"):
                proxies = {} if bool(session.bypass_proxy) else None
                resp = requests.get(item, timeout=(15, 60), verify=bool(session.verify_ssl), proxies=proxies)
                resp.raise_for_status()
                decoded_items.append(resp.content)
            else:
                decoded_items.append(item)

        tensor_batch = _IMAGE_CODEC.base64_to_tensor_parallel(
            decoded_items,
            log_prefix="局部重试",
            max_workers=min(4, len(decoded_items)),
        )
        if not isinstance(tensor_batch, torch.Tensor) or tensor_batch.ndim != 4:
            return False, None, "图片解码失败：返回张量异常"
        if tensor_batch.shape[0] <= 0:
            return False, None, "图片解码失败：空结果"

        return True, tensor_batch[0], "生成成功"
    except Exception as exc:
        msg = str(exc or "").strip()
        if len(msg) > 400:
            msg = msg[:400] + "…"
        return False, None, msg or "生成失败（未知错误）"


def _json_ok(data: Any) -> web.Response:
    return web.json_response({"success": True, "data": data})


def _json_fail(message: str, status: int = 400) -> web.Response:
    return web.json_response({"success": False, "message": message}, status=status)


_ROUTE_REGISTERED = False
_ROUTE_TIMER: threading.Timer | None = None

try:
    from server import PromptServer
except Exception:  # pragma: no cover - 非 ComfyUI 环境兼容
    class _DummyPromptServer:
        instance = None

    PromptServer = _DummyPromptServer()


def _ensure_batch_detail_routes(prompt_server_provider) -> None:
    global _ROUTE_REGISTERED, _ROUTE_TIMER
    if _ROUTE_REGISTERED:
        return

    prompt_server = prompt_server_provider()
    if prompt_server is None:
        if _ROUTE_TIMER is None or not _ROUTE_TIMER.is_alive() or threading.current_thread() is _ROUTE_TIMER:
            timer = threading.Timer(1.0, lambda: _ensure_batch_detail_routes(prompt_server_provider))
            timer.daemon = True
            _ROUTE_TIMER = timer
            timer.start()
        return

    @prompt_server.routes.get("/banana/batch_detail/state")
    async def handle_state(request):
        node_id = str(request.rel_url.query.get("node_id") or "").strip()
        if not node_id:
            return _json_fail("缺少 node_id", status=400)
        session = _get_session(node_id)
        if session is None:
            return _json_fail("未找到缓存：请先运行一次该节点", status=404)
        with session.lock:
            snapshot = session.to_public_dict()
        return _json_ok(snapshot)

    @prompt_server.routes.post("/banana/batch_detail/select_version")
    async def handle_select_version(request):
        try:
            payload = await request.json()
        except Exception:
            return _json_fail("无效的 JSON 请求", status=400)

        node_id = str(payload.get("node_id") or "").strip()
        version_id = str(payload.get("version_id") or "").strip()
        index = payload.get("index")
        if not node_id or not version_id:
            return _json_fail("缺少 node_id 或 version_id", status=400)
        try:
            index_int = int(index)
        except Exception:
            return _json_fail("index 无效", status=400)

        session = _get_session(node_id)
        if session is None:
            return _json_fail("未找到缓存：请先运行一次该节点", status=404)

        with session.lock:
            slot = _find_slot(session, index_int)
            if slot is None:
                return _json_fail("index 超出范围", status=400)
            if not any(v.version_id == version_id for v in slot.versions):
                return _json_fail("version_id 不存在", status=400)
            slot.active_version_id = version_id
            return _json_ok(slot.to_dict())

    @prompt_server.routes.post("/banana/batch_detail/regenerate")
    async def handle_regenerate(request):
        try:
            payload = await request.json()
        except Exception:
            return _json_fail("无效的 JSON 请求", status=400)

        node_id = str(payload.get("node_id") or "").strip()
        items = payload.get("items") or []
        if not node_id:
            return _json_fail("缺少 node_id", status=400)
        if not isinstance(items, list) or not items:
            return _json_fail("items 不能为空", status=400)

        session = _get_session(node_id)
        if session is None:
            return _json_fail("未找到缓存：请先运行一次该节点", status=404)

        # 避免 aiohttp 事件循环被阻塞：把重试放到线程里
        import asyncio
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _run_regen_sync() -> Dict[str, Any]:
            normalized_items: List[Tuple[int, str]] = []
            seen = set()
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                try:
                    idx = int(raw.get("index"))
                except Exception:
                    continue
                if idx in seen:
                    continue
                seen.add(idx)
                prompt_text = str(raw.get("prompt") or "")
                normalized_items.append((idx, prompt_text))

            if not normalized_items:
                return {"success": False, "message": "items 无有效内容"}

            worker_cap = min(_CONFIG_MANAGER.load_network_workers_cap(), _CONFIG_MANAGER.load_max_workers())
            max_workers = max(1, min(worker_cap, len(normalized_items)))

            results: List[Tuple[int, str, bool, Optional[torch.Tensor], str]] = []
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = {
                    ex.submit(_generate_one, session, prompt_text): (idx, prompt_text)
                    for idx, prompt_text in normalized_items
                }
                for fut in as_completed(futures):
                    idx, prompt_text = futures[fut]
                    try:
                        ok, tensor3d, message = fut.result()
                        results.append((idx, prompt_text, ok, tensor3d, message))
                    except Exception as exc:
                        msg = str(exc or "").strip()
                        if len(msg) > 300:
                            msg = msg[:300] + "…"
                        results.append((idx, prompt_text, False, None, msg or "未知错误"))

            updated: List[Dict[str, Any]] = []
            with session.lock:
                for idx, prompt_text, ok, tensor3d, message in sorted(results, key=lambda r: r[0]):
                    slot = _find_slot(session, idx)
                    if slot is None:
                        updated.append({"index": idx, "status": "failed", "error_message": "index 超出范围"})
                        continue

                    version_id = uuid.uuid4().hex[:12]
                    if ok and tensor3d is not None:
                        file_ref = _save_single_tensor(tensor3d, session.filename_prefix)
                        version = _ImageVersion(
                            version_id=version_id,
                            created_at=_now_ts(),
                            prompt=prompt_text,
                            image=file_ref,
                            status="success",
                            error_message=None,
                        )
                        _append_version(slot, version, set_active=True)
                        updated.append(
                            {
                                "index": idx,
                                "status": "success",
                                "active_version_id": slot.active_version_id,
                                "image": file_ref.to_dict(),
                                "prompt": prompt_text,
                            }
                        )
                        continue

                    # 失败：不切换 active，保留旧图；仍记录失败版本（含错误图文件，便于追踪）
                    err_tensor = _ERROR_CANVAS.build_error_tensor_from_text("局部重试失败", message)
                    err_file = _save_single_tensor(err_tensor[0], session.filename_prefix)
                    version = _ImageVersion(
                        version_id=version_id,
                        created_at=_now_ts(),
                        prompt=prompt_text,
                        image=err_file,
                        status="failed",
                        error_message=message,
                    )
                    _append_version(slot, version, set_active=False)
                    updated.append(
                        {
                            "index": idx,
                            "status": "failed",
                            "active_version_id": slot.active_version_id,
                            "error_message": message,
                        }
                    )

            return {"success": True, "updated": updated}

        result = await asyncio.to_thread(_run_regen_sync)
        if not result.get("success"):
            return web.json_response(result, status=400)
        return web.json_response(result)

    @prompt_server.routes.post("/banana/batch_detail/save_stitch")
    async def handle_save_stitch(request):
        try:
            payload = await request.json()
        except Exception:
            return _json_fail("无效的 JSON 请求", status=400)

        node_id = str(payload.get("node_id") or "").strip()
        if not node_id:
            return _json_fail("缺少 node_id", status=400)
        session = _get_session(node_id)
        if session is None:
            return _json_fail("未找到缓存：请先运行一次该节点", status=404)

        import asyncio

        def _run_stitch_sync() -> Dict[str, Any]:
            with session.lock:
                active_versions = [slot.get_active() for slot in session.slots]
                file_refs = [v.image for v in active_versions if v is not None]

            images: List[Image.Image] = []
            for ref in file_refs:
                try:
                    images.append(_load_image_from_file_ref(ref))
                except Exception as exc:
                    logger.warning(f"拼接读取图片失败: {exc}")

            if not images:
                return {"success": False, "message": "没有可用于拼接的图片（可能还未运行或文件缺失）"}

            stitched = _stitch_vertical(images)
            prefix = f"{session.filename_prefix}_stitch"

            with _SAVE_LOCK:
                full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
                    prefix,
                    folder_paths.get_output_directory(),
                    stitched.size[0],
                    stitched.size[1],
                )
                file = f"{filename}_{counter:05}_.png"
                stitched.save(os.path.join(full_output_folder, file), compress_level=4)
                file_ref = _ImageFileRef(filename=file, subfolder=subfolder, type="output")

            return {"success": True, "image": file_ref.to_dict()}

        result = await asyncio.to_thread(_run_stitch_sync)
        status = 200 if result.get("success") else 400
        return web.json_response(result, status=status)

    _ROUTE_REGISTERED = True


_ensure_batch_detail_routes(lambda: getattr(PromptServer, "instance", None))


class XinbaoBatchDetailImageSaver:
    """
    批量保存图片，并为前端“详情图拼接/局部重试/多版本切换”提供缓存。

    注意：
    - 该节点本身不会自动保存拼接图；拼接图保存由前端按钮触发后端路由完成；
    - 版本回收仅在 UI/缓存层进行，不删除磁盘文件。
    """

    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        self.prefix_append = ""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "需要保存/展示的图片（支持 batch 或列表）", "forceInput": True}),
                "filename_prefix": (
                    "STRING",
                    {"default": "ComfyUI", "tooltip": "保存文件名前缀（与 SaveImage 一致）"},
                ),
            },
            "optional": {
                "prompt_batch": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "（可选）每张图对应的提示词批次；建议连接到 BananaV2 的提示词链路输出",
                        "forceInput": True,
                        "multiline": True,
                    },
                ),
                "banana_api_key": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": "（可选）用于局部重试的 Banana API Key；留空则回退 config.ini/全局密钥",
                    },
                ),
                "image_1": ("IMAGE", {"tooltip": "（可选）参考图 1（用于局部重试复用）"}),
                "image_2": ("IMAGE", {"tooltip": "（可选）参考图 2（用于局部重试复用）"}),
                "image_3": ("IMAGE", {"tooltip": "（可选）参考图 3（用于局部重试复用）"}),
                "image_4": ("IMAGE", {"tooltip": "（可选）参考图 4（用于局部重试复用）"}),
                "image_5": ("IMAGE", {"tooltip": "（可选）参考图 5（用于局部重试复用）"}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "detail_node_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ()
    OUTPUT_NODE = True
    CATEGORY = "❤️‍🔥心宝专用/详情页"
    FUNCTION = "execute"
    INPUT_IS_LIST = True

    def execute(
        self,
        images,
        filename_prefix: str = "ComfyUI",
        prompt_batch: Any = None,
        banana_api_key: str = "",
        image_1: Any = None,
        image_2: Any = None,
        image_3: Any = None,
        image_4: Any = None,
        image_5: Any = None,
        prompt: Any = None,
        extra_pnginfo: Any = None,
        detail_node_id: Any = None,
    ):
        node_id = str(_as_first(detail_node_id) or "").strip()
        if not node_id:
            node_id = uuid.uuid4().hex[:8]

        prompt_graph = _as_first(prompt)
        extra_info = _as_first(extra_pnginfo)

        image_samples = _flatten_image_samples(images)
        prompt_list = _normalize_prompt_batch(prompt_batch)

        if len(prompt_list) == 1 and len(image_samples) > 1:
            prompt_list = prompt_list * len(image_samples)
        if not prompt_list:
            prompt_list = [""] * len(image_samples)
        if len(prompt_list) != len(image_samples):
            logger.warning(
                f"[心宝❤批量详情图保存] 提示词数量({len(prompt_list)})与图片数量({len(image_samples)})不一致，将对齐补空"
            )
            min_len = min(len(prompt_list), len(image_samples))
            prompt_list = prompt_list[:min_len] + [""] * (len(image_samples) - min_len)

        # 1) 保存每张图（等价 SaveImage）
        file_refs, full_output_folder = _save_image_samples(
            image_samples=image_samples,
            filename_prefix=str(_as_first(filename_prefix) or "ComfyUI"),
            prompt=prompt_graph,
            extra_pnginfo=extra_info,
        )

        # 2) 从上游 BananaV2 解析静态配置（PROMPT 内可用）
        upstream_inputs = _extract_upstream_node_inputs(prompt_graph, node_id)
        route_choice = str(upstream_inputs.get("线路") or "心宝❤新渠道")
        # [兼容性处理] 旧渠道名称自动映射到新渠道（静默兼容旧工作流）
        _legacy_route_names = {
            "测试高速渠道(key不通用)",
            "心宝❤测试新渠道（Key不通用）",
            "心宝❤测试新渠道(Key不通用)",
            "心宝测试新渠道",
        }
        if route_choice in _legacy_route_names:
            route_choice = "心宝❤新渠道"
        model_type = str(upstream_inputs.get("model_type") or "gemini-3-pro-image-preview")
        aspect_ratio = str(upstream_inputs.get("aspect_ratio") or "Auto")
        image_size = str(upstream_inputs.get("image_size") or "2K")
        top_p = float(upstream_inputs.get("top_p") or 0.95)
        enable_google_search = _coerce_bool(upstream_inputs.get("联网搜索"))
        bypass_proxy = _coerce_bool(upstream_inputs.get("绕过代理"))
        disable_ssl = _coerce_bool(upstream_inputs.get("禁用SSL验证"))
        verify_ssl = not disable_ssl
        limit_long_edge_str = str(upstream_inputs.get("大于5M限制长边") or "禁用")

        resize_limit = None
        if limit_long_edge_str and limit_long_edge_str != "禁用":
            try:
                resize_limit = int(limit_long_edge_str)
            except Exception:
                resize_limit = None

        # 3) 参考图：尽量上传得到 URL（A），失败则保留 base64 兜底
        ref_tensors: List[torch.Tensor] = []
        for ref in (image_1, image_2, image_3, image_4, image_5):
            samples = _flatten_image_samples(ref)
            if samples:
                ref_tensors.append(samples[0])

        reference_urls, reference_b64 = _prepare_reference_payload(ref_tensors, resize_limit=resize_limit)

        # 4) 解析密钥（只缓存到内存，不回传前端）
        api_key_raw = str(_as_first(banana_api_key) or "")
        api_key, base_url, api_key_source = _resolve_api_key(api_key_raw, route_choice)

        # 5) 初始化版本与槽位
        slots: List[_ImageSlot] = []
        for idx, file_ref in enumerate(file_refs):
            version_id = uuid.uuid4().hex[:12]
            version = _ImageVersion(
                version_id=version_id,
                created_at=_now_ts(),
                prompt=prompt_list[idx] if idx < len(prompt_list) else "",
                image=file_ref,
                status="success",
                error_message=None,
            )
            slots.append(_ImageSlot(index=idx, active_version_id=version_id, versions=[version]))

        session = _BatchDetailSession(
            node_id=node_id,
            created_at=_now_ts(),
            filename_prefix=str(_as_first(filename_prefix) or "ComfyUI"),
            slots=slots,
            route_choice=route_choice,
            model_type=model_type,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            top_p=top_p,
            enable_google_search=enable_google_search,
            bypass_proxy=bypass_proxy,
            verify_ssl=verify_ssl,
            limit_long_edge_str=limit_long_edge_str,
            api_key=api_key,
            api_key_source=api_key_source,
            base_url=base_url,
            reference_urls=reference_urls,
            reference_b64=reference_b64,
            lock=threading.Lock(),
        )
        _set_session(session)

        status_lines = [
            "✅ 批量详情图保存完成",
            f"📦 图片数量: {len(file_refs)}",
            f"📂 保存目录: {full_output_folder}",
            f"🔑 API Key 来源: {api_key_source}（{_mask_key(api_key)}）",
            "🧩 提示：点击节点上的“打开详情面板”按钮进行拼接预览与局部重试",
        ]

        return {
            "ui": {
                "images": [r.to_dict() for r in file_refs],
                "text": ["\n".join(status_lines)],
                "xinbao_batch_detail": {"node_id": node_id, "count": len(file_refs)},
            }
        }


NODE_CLASS_MAPPINGS = {"XinbaoBatchDetailImageSaver": XinbaoBatchDetailImageSaver}
NODE_DISPLAY_NAME_MAPPINGS = {"XinbaoBatchDetailImageSaver": "心宝❤批量详情图保存"}
