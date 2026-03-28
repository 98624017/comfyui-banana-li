from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import threading
import time
from io import BytesIO
from typing import List, Mapping, Optional, Sequence

import numpy as np
import requests
import torch
from PIL import Image

import comfy.utils
import comfy.model_management

from config_manager import ConfigManager
from logger import logger
from image_codec import ImageCodec
from task_runner import BatchGenerationRunner
from banana_kv_auth import EdgeKVAuthClient
from image_uploader import ImageUploader, ImageUploadError


BASE_URL = "https://api-inference.modelscope.cn"
IMAGE_GENERATION_URL = f"{BASE_URL}/v1/images/generations"
TASK_STATUS_URL = f"{BASE_URL}/v1/tasks/{{task_id}}"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
# Obfuscated: https://ai.kefan.cn/api/upload/local
UPLOAD_URL = "lacol/daolpu/ipa/nc.nafek.ia//:sptth"[::-1]

MAX_BATCH_SIZE = 15
CONNECT_TIMEOUT = 25.0
READ_TIMEOUT = 150.0
POLL_INTERVAL_SECONDS = 2.5
POLL_MAX_SECONDS = 420.0
DOWNLOAD_TIMEOUT = 60.0

# Caption 图片上传缓存: {md5_hex: (url, timestamp)}
_CAPTION_UPLOAD_CACHE: dict[str, tuple[str, float]] = {}
_CAPTION_CACHE_TTL = 9000.0  # 2.5 小时
_CAPTION_CACHE_MAX_SIZE = 256
_CAPTION_CACHE_LOCK = threading.Lock()

_CONFIG_MANAGER = ConfigManager()
_KV_AUTH_CLIENT = EdgeKVAuthClient(_CONFIG_MANAGER, logger)

CHANNEL_BANANA = "香蕉同款渠道(旧渠道)"
CHANNEL_BANANA_LEGACY = "香蕉同款渠道"
CHANNEL_MODAO = "魔搭社区"
CHANNEL_XINBAO_TEST = "心宝❤新渠道"
CHANNEL_XINBAO_TEST_LEGACY = "心宝测试测试渠道"
CHANNEL_XINBAO_TEST_LEGACY_ALT = "心宝测试新渠道"
BANANA_CAPTION_MODELS_RAW = [
    "gemini-3-pro-preview-c",
    "gemini-3-flash-c",
    "gpt-5.1-high-c",
    "Qwen/Qwen3.5-397B-A17B",
]
BANANA_CAPTION_MODELS_DISPLAY = [
    "gemini-3-pro-preview-c（较耗时）",
    "gemini-3-flash-c",
    "gpt-5.1-high-c",
    "Qwen/Qwen3.5-397B-A17B",
]
BANANA_DISPLAY_TO_RAW = {
    "gemini-3-pro-preview-c（较耗时）": "gemini-3-pro-preview-c",
}
MODAO_CAPTION_MODELS = [
    "Qwen/Qwen3-VL-8B-Instruct",
    "Qwen/Qwen3-VL-235B-A22B-Instruct",
    "Qwen/Qwen3-VL-8B-Thinking",
    "Qwen/Qwen3.5-397B-A17B",
    "moonshotai/Kimi-K2.5",
]
CAPTION_MODELS = list(dict.fromkeys(BANANA_CAPTION_MODELS_DISPLAY + MODAO_CAPTION_MODELS))
BANANA_ROUTE_ORDER = ["香港专线", "直连美区", "CF专线"]
XINBAO_TEST_BASE_URL = _CONFIG_MANAGER.get_xinbao_test_base_url()


def _mask_key(api_key: str) -> str:
    cleaned = (api_key or "").strip()
    if len(cleaned) <= 6:
        return "***"
    return f"{cleaned[:3]}***{cleaned[-3:]}"


def _sanitize_api_key(value: Optional[str]) -> Optional[str]:
    return _CONFIG_MANAGER.sanitize_api_key(value)


def _resolve_api_key(api_key: str, *, fallback_loader=None, message: str = "请填写 API Key") -> str:
    candidate = _sanitize_api_key(api_key)
    if candidate:
        return candidate

    fallback_value = None
    if fallback_loader:
        try:
            fallback_value = fallback_loader()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取兜底密钥失败: {exc}")
    fallback_candidate = _sanitize_api_key(fallback_value)
    if fallback_candidate:
        return fallback_candidate

    raise Exception(message)


def _resolve_banana_caption_key(raw_key: str) -> tuple[str, List[str], str]:
    candidate = (raw_key or "").strip()
    lowered = candidate.lower()
    if lowered.startswith("fixsk-"):
        stripped_key = candidate[len("fix") :]
        hidden_key = _sanitize_api_key(stripped_key)
        if hidden_key:
            return hidden_key, [_CONFIG_MANAGER.get_hidden_base_url()], "隐藏线路临时密钥"

    sanitized_input = _sanitize_api_key(candidate)
    if sanitized_input:
        return sanitized_input, [_CONFIG_MANAGER.get_route_base_url(route) for route in BANANA_ROUTE_ORDER], "节点/全局输入"

    config_key = _sanitize_api_key(_CONFIG_MANAGER.load_api_key())
    if config_key:
        return config_key, [_CONFIG_MANAGER.get_route_base_url(route) for route in BANANA_ROUTE_ORDER], "config.ini 兜底"

    raise Exception("缺少香蕉渠道 API Key：请在节点或“心宝❤全局密钥管理”中填写，或在 config.ini 中配置 banana_api_key")


def _resolve_xinbao_test_caption_key(raw_key: str) -> tuple[str, List[str], str]:
    sanitized_input = _sanitize_api_key((raw_key or "").strip())
    if sanitized_input:
        return sanitized_input, [XINBAO_TEST_BASE_URL], "节点/全局输入"

    config_key = _sanitize_api_key(_CONFIG_MANAGER.load_api_key())
    if config_key:
        return config_key, [XINBAO_TEST_BASE_URL], "config.ini 兜底"

    raise Exception("缺少心宝❤新渠道 API Key：请在节点或“心宝❤全局密钥管理”中填写（该渠道 Key 与其他渠道不通用）")


def _resolve_modao_caption_key(raw_key: str) -> tuple[str, List[str], str]:
    sanitized_input = _sanitize_api_key(raw_key)
    if sanitized_input:
        return sanitized_input, [BASE_URL], "节点/全局输入"

    config_key = _sanitize_api_key(_CONFIG_MANAGER.load_modelscope_api_key())
    if config_key:
        return config_key, [BASE_URL], "config.ini 兜底"

    raise Exception("缺少魔搭社区 API Key：请在节点或“心宝❤全局密钥管理”中填写，或在 config.ini 中配置 modelscope_api_key")


def _normalize_batch(batch_size: int) -> int:
    if batch_size is None:
        return 1
    value = int(batch_size)
    if value < 1:
        return 1
    if value > MAX_BATCH_SIZE:
        raise Exception("批量上限为 8，请调整后重试")
    return value


def _normalize_seed(seed: int) -> int:
    if isinstance(seed, int) and seed >= 0:
        return seed
    return random.randint(0, 2_147_483_647)


def _ensure_not_interrupted() -> None:
    """统一的中断检查，复用 ComfyUI 原生取消机制。"""
    comfy.model_management.throw_exception_if_processing_interrupted()


def _normalize_concurrency(max_concurrency: int, batch: int) -> int:
    try:
        value = int(max_concurrency)
    except Exception:
        value = 1
    value = max(1, value)
    value = min(4, value)
    return min(value, batch if batch > 0 else 1)


def _create_session(bypass_proxy: bool, disable_ssl_verify: bool) -> requests.Session:
    session = requests.Session()
    if bypass_proxy:
        session.trust_env = False
        session.proxies = {}
    session.verify = not disable_ssl_verify
    return session


def _build_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-ModelScope-Async-Mode": "true",
    }


def _normalize_channel(channel: str) -> str:
    candidate = (channel or "").strip()
    if candidate == CHANNEL_MODAO:
        return CHANNEL_MODAO
    if candidate == CHANNEL_XINBAO_TEST:
        return CHANNEL_XINBAO_TEST
    # [兼容性处理] 旧渠道名称静默映射到新渠道
    if candidate in {CHANNEL_XINBAO_TEST_LEGACY, CHANNEL_XINBAO_TEST_LEGACY_ALT}:
        return CHANNEL_XINBAO_TEST
    if candidate == CHANNEL_BANANA_LEGACY:
        return CHANNEL_BANANA
    return CHANNEL_BANANA


def _normalize_model_for_channel(channel: str, model: str) -> str:
    candidate = (model or "").strip()
    if channel in {CHANNEL_BANANA, CHANNEL_XINBAO_TEST}:
        # 尝试将显示名称转换为原始模型 ID
        raw = BANANA_DISPLAY_TO_RAW.get(candidate)
        if raw:
            return raw
    
    # 如果不是显示名称（即可能是原始 ID 或用户自定义模型），直接返回
    # 不再强制校验白名单，允许外部通过输入框传入任意模型 ID
    return candidate


def _build_chat_url(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    return f"{base}{CHAT_COMPLETIONS_PATH}"


_REQUEST_ID_PATTERN = re.compile(r"request(?:\s+|_|-)?id\s*[:：=]\s*([A-Za-z0-9_-]{6,})", re.IGNORECASE)


def _shorten_text(text: str, limit: int = 260) -> str:
    compact = " ".join((text or "").strip().split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}...(已截断)"


def _summarize_payload(payload: object, limit: int = 320) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = str(payload)
    return _shorten_text(text, limit=limit)


def _extract_request_id_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    match = _REQUEST_ID_PATTERN.search(text)
    if match:
        return match.group(1)
    return None


def _extract_request_id_from_headers(headers: Mapping[str, str] | None) -> Optional[str]:
    if not headers:
        return None
    for key in ("x-request-id", "request-id", "request_id", "trace-id", "trace_id"):
        value = headers.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _find_request_id_in_payload(payload: object, depth: int = 0) -> Optional[str]:
    if depth > 5:
        return None

    if isinstance(payload, dict):
        for key in ("request_id", "requestId", "req_id", "request-id", "trace_id", "traceId", "trace-id"):
            value = payload.get(key)
            if value is not None:
                text = str(value).strip()
                if text:
                    return text
        for value in payload.values():
            request_id = _find_request_id_in_payload(value, depth + 1)
            if request_id:
                return request_id
        return None

    if isinstance(payload, list):
        for item in payload[:8]:
            request_id = _find_request_id_in_payload(item, depth + 1)
            if request_id:
                return request_id

    return None


def _extract_error_fields_from_payload(payload: object) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    code: Optional[str] = None
    error_type: Optional[str] = None
    message: Optional[str] = None
    request_id: Optional[str] = None

    if isinstance(payload, dict):
        error_obj = payload.get("error")
        if isinstance(error_obj, dict):
            if error_obj.get("code") is not None:
                code = str(error_obj.get("code"))
            if error_obj.get("type") is not None:
                error_type = str(error_obj.get("type"))
            for key in ("message", "error_message", "detail", "details"):
                value = error_obj.get(key)
                if isinstance(value, str) and value.strip():
                    message = value
                    break
        elif isinstance(error_obj, str) and error_obj.strip():
            message = error_obj.strip()

        if not code and payload.get("code") is not None:
            code = str(payload.get("code"))
        if not error_type and payload.get("type") is not None:
            error_type = str(payload.get("type"))
        if not message:
            for key in ("message", "msg", "error_message", "detail", "details"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    message = value
                    break

        request_id = _find_request_id_in_payload(payload)

    if not request_id and message:
        request_id = _extract_request_id_from_text(message)

    return code, error_type, message, request_id


def _summarize_payload_error(payload: object) -> Optional[str]:
    code, error_type, message, request_id = _extract_error_fields_from_payload(payload)
    if not any((code, error_type, message)):
        return None

    parts = []
    if code:
        parts.append(f"code={code}")
    if error_type:
        parts.append(f"type={error_type}")
    if request_id:
        parts.append(f"request_id={request_id}")
    if message:
        parts.append(f"message={_shorten_text(message)}")
    return " | ".join(parts)


def _build_upstream_error_detail(response: requests.Response) -> str:
    # 注意：requests.Response.headers 是 CaseInsensitiveDict，不能强制转成 dict，
    # 否则会丢失大小写不敏感特性，导致 request_id / 限额等 header 提取失败。
    headers = response.headers or {}
    content_type = str(headers.get("Content-Type") or "").strip()
    response_text = response.text or ""

    parsed_payload: object | None = None
    stripped = response_text.lstrip()
    if stripped and ("json" in content_type.lower() or stripped.startswith("{") or stripped.startswith("[")):
        try:
            parsed_payload = json.loads(response_text)
        except Exception:
            parsed_payload = None

    code: Optional[str] = None
    error_type: Optional[str] = None
    message: Optional[str] = None
    payload_request_id: Optional[str] = None
    if parsed_payload is not None:
        code, error_type, message, payload_request_id = _extract_error_fields_from_payload(parsed_payload)

    request_id = payload_request_id or _extract_request_id_from_headers(headers) or _extract_request_id_from_text(response_text)

    parts = [f"HTTP {response.status_code}"]
    if code:
        parts.append(f"code={code}")
    if error_type:
        parts.append(f"type={error_type}")
    if request_id:
        parts.append(f"request_id={request_id}")

    if message:
        parts.append(f"message={_shorten_text(message)}")
    else:
        if content_type:
            parts.append(f"content_type={content_type}")
        body_preview = _shorten_text(response_text)
        if body_preview:
            parts.append(f"body={body_preview}")

    return " | ".join(parts)


def _parse_json_response_or_raise(response: requests.Response, context: str) -> object:
    try:
        return response.json()
    except ValueError:
        content_type = str((response.headers or {}).get("Content-Type") or "").lower()
        body_text = (response.text or "").lstrip()
        detail = _build_upstream_error_detail(response)
        if "text/event-stream" in content_type or body_text.startswith("data:"):
            raise Exception(f"{context}: 上游返回流式响应，当前节点仅支持非流式 JSON。{detail}")
        raise Exception(f"{context}: 上游返回非 JSON 响应。{detail}")


def _send_caption_request_with_routes(
    session: requests.Session,
    payload: dict,
    headers: dict,
    base_urls: List[str],
    channel_label: str,
    model_name: str,
    ensure_not_interrupted: Optional[callable] = None,
) -> requests.Response:
    def _route_tag(idx: int, total: int, base_url: str) -> str:
        if channel_label == CHANNEL_MODAO:
            return f"{base_url}"
        return f"线路 {idx}/{total}"

    errors: List[str] = []
    for index, base_url in enumerate(base_urls, start=1):
        if ensure_not_interrupted:
            ensure_not_interrupted()
        chat_url = _build_chat_url(base_url)
        route_tag = _route_tag(index, len(base_urls), base_url)
        visible_target = chat_url if channel_label == CHANNEL_MODAO else CHAT_COMPLETIONS_PATH
        logger.info(f"{channel_label} {route_tag} 准备发起请求: {visible_target}")
        try:
            # 线程轮询模式以支持及时中断，支持 SSL 自动禁用重试
            def _run_request(verify_ssl: bool) -> dict:
                result: dict = {}

                def _thread():
                    try:
                        result["response"] = session.post(
                            chat_url,
                            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            headers=headers,
                            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                            verify=verify_ssl,
                        )
                    except Exception as e:
                        result["error"] = e

                t = threading.Thread(target=_thread, daemon=True)
                t.start()
                while t.is_alive():
                    t.join(timeout=0.1)
                    if ensure_not_interrupted:
                        ensure_not_interrupted()
                return result

            initial_verify = session.verify
            request_result = _run_request(initial_verify)

            # SSL 相关报错时自动禁用 SSL 验证重试
            if "error" in request_result and initial_verify:
                exc = request_result["error"]
                if isinstance(exc, requests.exceptions.SSLError) or "SSL" in type(exc).__name__:
                    logger.warning(
                        f"{channel_label} {route_tag} SSL 验证失败（{type(exc).__name__}），"
                        f"自动禁用 SSL 验证重试..."
                    )
                    if ensure_not_interrupted:
                        ensure_not_interrupted()
                    request_result = _run_request(False)

            if "error" in request_result:
                raise request_result["error"]

            response = request_result.get("response")
            if response is None:
                # 理论上不会发生，除非线程无声奔溃
                raise Exception("请求线程未返回结果")
        except requests.RequestException as exc:
            error_msg = str(exc)
            if channel_label != CHANNEL_MODAO:
                # 脱敏处理：只保留错误类型，避免泄露 URL
                error_msg = f"连接异常 ({type(exc).__name__})"

            logger.warning(f"{channel_label} {route_tag} 连接失败: {error_msg}")
            errors.append(f"{route_tag}: {error_msg}")
            continue
        if ensure_not_interrupted:
            ensure_not_interrupted()
        if response.status_code == 200:
            _log_ratelimit(response, context="ModelScope 图像描述", model_name=model_name)
            logger.info(f"{channel_label} {route_tag} 调用成功，模型={model_name}")
            if ensure_not_interrupted:
                ensure_not_interrupted()
            return response
        if response.status_code == 429 or response.status_code >= 500:
            detail = _build_upstream_error_detail(response)
            logger.warning(f"{channel_label} {route_tag} 服务异常: {detail}")
            errors.append(f"{route_tag}: {detail}")
            if ensure_not_interrupted:
                ensure_not_interrupted()
            continue
        if ensure_not_interrupted:
            ensure_not_interrupted()
        raise Exception(f"描述生成失败: {route_tag}: {_build_upstream_error_detail(response)}")

    if ensure_not_interrupted:
        ensure_not_interrupted()
    detail = "；".join(errors) if errors else "未知错误"
    raise Exception(f"{channel_label} 请求失败，已尝试全部线路：{detail}")


def _log_ratelimit(response: requests.Response, context: str = "ModelScope", model_name: str = "") -> None:
    try:
        # 注意：保留 CaseInsensitiveDict，避免上游 header 大小写变化导致读取失败。
        headers = response.headers or {}
        user_limit = headers.get("modelscope-ratelimit-requests-limit")
        user_remaining = headers.get("modelscope-ratelimit-requests-remaining")
        model_limit = headers.get("modelscope-ratelimit-model-requests-limit")
        model_remaining = headers.get("modelscope-ratelimit-model-requests-remaining")
        if not (user_limit or user_remaining or model_limit or model_remaining):
            return
        user_part = f"用户 {user_remaining or '?'} / {user_limit or '?'}"
        model_part = f"模型 {model_remaining or '?'} / {model_limit or '?'}"
        
        model_info = f" ({model_name})" if model_name else ""
        logger.info(f"{context}{model_info} 限额信息：{user_part}；{model_part}")
    except Exception:
        # 限额日志失败不影响主流程
        return


def _tensor_to_jpeg_bytes(image_tensor: torch.Tensor, max_size: int = 0) -> bytes:
    tensor = image_tensor
    if len(tensor.shape) == 4:
        tensor = tensor[0]
    array = tensor.detach().cpu().numpy()
    if array.max() <= 1.0:
        array = (array * 255.0).clip(0, 255)
    array = array.astype(np.uint8)
    image = Image.fromarray(array)
    
    if max_size > 0:
        width, height = image.size
        max_dim = max(width, height)
        if max_dim > max_size:
            scale = max_size / max_dim
            new_width = int(width * scale)
            new_height = int(height * scale)
            image = image.resize((new_width, new_height), Image.LANCZOS)
            # logger.info(f"Image resized from {width}x{height} to {new_width}x{new_height}")

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def _tensor_to_base64_data_url(image_tensor: torch.Tensor, max_size: int = 0) -> str:
    jpeg_bytes = _tensor_to_jpeg_bytes(image_tensor, max_size=max_size)
    encoded = base64.b64encode(jpeg_bytes).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"


def _tensor_to_optimized_data_url(image_tensor: torch.Tensor, max_size: int = 0) -> str:
    image_bytes, mime = _tensor_to_optimized_bytes(image_tensor, max_size=max_size)
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime};base64,{encoded}"


def _tensor_to_optimized_bytes(image_tensor: torch.Tensor, max_size: int = 0) -> tuple[bytes, str]:
    """将 Tensor 转为体积最优的图片字节 (WebP vs PNG 自动选择)。

    Returns:
        (image_bytes, mime_type) — e.g. (b"...", "image/webp")
    """
    tensor = image_tensor
    if len(tensor.shape) == 4:
        tensor = tensor[0]
    array = tensor.detach().cpu().numpy()
    if array.max() <= 1.0:
        array = (array * 255.0).clip(0, 255)
    array = array.astype(np.uint8)
    pil_image = Image.fromarray(array)

    # 缩放到最大边限制
    if max_size > 0:
        width, height = pil_image.size
        max_dim = max(width, height)
        if max_dim > max_size:
            scale = max_size / max_dim
            new_w, new_h = int(width * scale), int(height * scale)
            pil_image = pil_image.resize((new_w, new_h), Image.LANCZOS)
            logger.info(f"📐 Caption 图片缩放: {width}x{height} → {new_w}x{new_h}")

    # WebP (quality=95, method=6 最高压缩)
    buf_webp = BytesIO()
    pil_image.save(buf_webp, format="WEBP", quality=95, method=6)
    webp_bytes = buf_webp.getvalue()

    # PNG (compress_level=6)
    buf_png = BytesIO()
    pil_image.save(buf_png, format="PNG", compress_level=6)
    png_bytes = buf_png.getvalue()

    webp_kb = len(webp_bytes) / 1024
    png_kb = len(png_bytes) / 1024

    if len(webp_bytes) <= len(png_bytes):
        return webp_bytes, "image/webp"
    else:
        return png_bytes, "image/png"


def _decode_image_bytes(data: bytes, target_size: tuple[int, int] | None) -> torch.Tensor:
    image = Image.open(BytesIO(data)).convert("RGB")
    if target_size and target_size[0] > 0 and target_size[1] > 0:
        if image.size != target_size:
            image = image.resize(target_size, Image.LANCZOS)
    array = np.array(image).astype(np.float32) / 255.0
    tensor = torch.from_numpy(array)[None, ...]
    return tensor


def _download_image(
    session: requests.Session,
    url: str,
    target_size: tuple[int, int] | None,
    ensure_not_interrupted: Optional[callable] = None,
) -> torch.Tensor:
    if ensure_not_interrupted:
        ensure_not_interrupted()
    if url.startswith("data:image"):
        try:
            payload = url.split(",", 1)[1]
            image_bytes = base64.b64decode(payload)
            return _decode_image_bytes(image_bytes, target_size)
        except Exception as exc:
            raise Exception(f"解析内联图片失败: {exc}") from exc

    resp = session.get(url, timeout=DOWNLOAD_TIMEOUT)
    if resp.status_code != 200:
        raise Exception(f"图片下载失败: HTTP {resp.status_code}")
    if ensure_not_interrupted:
        ensure_not_interrupted()
    return _decode_image_bytes(resp.content, target_size)


def _stack_images(tensors: Sequence[torch.Tensor]) -> torch.Tensor:
    if not tensors:
        raise Exception("未获取到任何图片")
    return torch.cat(tensors, dim=0)


def _extract_image_urls(data: dict) -> List[str]:
    urls: List[str] = []
    if not isinstance(data, dict):
        return urls
    if "output_images" in data and isinstance(data["output_images"], list):
        urls.extend([str(item) for item in data["output_images"] if item])
    if "images" in data:
        images = data["images"]
        if isinstance(images, list):
            for item in images:
                if isinstance(item, dict) and item.get("url"):
                    urls.append(str(item["url"]))
                elif isinstance(item, dict) and item.get("b64_json"):
                    urls.append(f"data:image/jpeg;base64,{item['b64_json']}")
                elif isinstance(item, str):
                    urls.append(item)
    return urls


def _poll_task(
    session: requests.Session,
    task_id: str,
    headers: dict,
    expected: int,
    ensure_not_interrupted: Optional[callable] = None,
) -> List[str]:
    start = time.time()
    poll_headers = dict(headers)
    poll_headers["X-ModelScope-Task-Type"] = "image_generation"

    while True:
        if ensure_not_interrupted:
            ensure_not_interrupted()
        resp = session.get(
            TASK_STATUS_URL.format(task_id=task_id),
            headers=poll_headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        if resp.status_code != 200:
            raise Exception(f"任务查询失败: HTTP {resp.status_code} {resp.text}")
        payload = resp.json()
        status = payload.get("task_status") or payload.get("status")
        if status == "SUCCEED":
            urls = _extract_image_urls(payload)
            if not urls:
                raise Exception("任务成功但未返回图片链接")
            return urls[:expected]
        if status == "FAILED":
            error_message = payload.get("errors") or payload.get("error_message") or payload
            raise Exception(f"任务失败: {error_message}")
        if time.time() - start > POLL_MAX_SECONDS:
            raise Exception("任务轮询超时，请稍后重试或降低批量大小")
        if ensure_not_interrupted:
            ensure_not_interrupted()
        time.sleep(POLL_INTERVAL_SECONDS)


def _submit_and_fetch_images(
    session: requests.Session,
    headers: dict,
    payload: dict,
    target_size: tuple[int, int] | None,
    batch_size: int,
    ensure_not_interrupted: Optional[callable] = None,
) -> torch.Tensor:
    if ensure_not_interrupted:
        ensure_not_interrupted()
    response = session.post(
        IMAGE_GENERATION_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    _log_ratelimit(response, context="ModelScope 文生图请求", model_name=payload.get("model", ""))
    if response.status_code == 400:
        minimal_payload = {
            k: v
            for k, v in payload.items()
            if k
            in {
                "model",
                "prompt",
                "image",
                "image_url",
                "size",
                "steps",
                "guidance",
                "seed",
                "negative_prompt",
                "n",
            }
        }
        response = session.post(
            IMAGE_GENERATION_URL,
            data=json.dumps(minimal_payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        _log_ratelimit(response, context="ModelScope 文生图降级重试", model_name=minimal_payload.get("model", ""))
    if response.status_code != 200:
        raise Exception(f"API 请求失败: HTTP {response.status_code} {response.text}")

    data = response.json()
    urls = _extract_image_urls(data)
    if not urls and "task_id" in data:
        urls = _poll_task(session, str(data["task_id"]), headers, batch_size, ensure_not_interrupted)
    if not urls:
        raise Exception(f"未识别的返回内容: {data}")

    images = [
        _download_image(session, url, target_size, ensure_not_interrupted) for url in urls[:batch_size]
    ]
    return _stack_images(images)


def _upload_image_get_url(session: requests.Session, image_tensor: torch.Tensor) -> str | None:
    try:
        jpeg_bytes = _tensor_to_jpeg_bytes(image_tensor)
        files = {"file": ("image.jpg", BytesIO(jpeg_bytes), "image/jpeg")}
        resp = session.post(UPLOAD_URL, files=files, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, dict) and data.get("success") and data.get("data"):
            return str(data["data"])
    except Exception:
        return None
    return None


def _upload_caption_image(
    image_tensor: torch.Tensor,
    uploader: ImageUploader,
    max_size: int = 2048,
) -> str:
    """将 Tensor 转为优化格式，上传图床获取 URL；失败时降级为 base64 data URL。"""
    # 1. 转换为优化后的字节 (WebP vs PNG 自动选择)
    image_bytes, mime = _tensor_to_optimized_bytes(image_tensor, max_size=max_size)

    # 2. 检查本地缓存 (2.5h TTL，线程安全)
    cache_key = hashlib.md5(image_bytes).hexdigest()
    now = time.time()
    with _CAPTION_CACHE_LOCK:
        cached = _CAPTION_UPLOAD_CACHE.get(cache_key)
        if cached:
            url, ts = cached
            if now - ts < _CAPTION_CACHE_TTL:
                logger.info("♻️ 复用已缓存图床链接")
                return url
            else:
                _CAPTION_UPLOAD_CACHE.pop(cache_key, None)  # 过期清理

    # 3. 上传图床（在锁外执行，避免阻塞其他线程）
    try:
        mime_to_ext = {
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }
        upload_ext = mime_to_ext.get((mime or "").lower(), "bin")
        url = uploader.upload(
            image_bytes,
            filename=f"caption.{upload_ext}",
            content_type=mime,
        )
        with _CAPTION_CACHE_LOCK:
            _CAPTION_UPLOAD_CACHE[cache_key] = (url, now)
            # 驱逐最旧条目，防止无界增长
            if len(_CAPTION_UPLOAD_CACHE) > _CAPTION_CACHE_MAX_SIZE:
                oldest_key = min(_CAPTION_UPLOAD_CACHE, key=lambda k: _CAPTION_UPLOAD_CACHE[k][1])
                _CAPTION_UPLOAD_CACHE.pop(oldest_key, None)
        logger.info(f"✅ Caption 图片已上传图床: {mime}, {len(image_bytes)/1024:.1f}KB")
        return url
    except ImageUploadError as e:
        # 4. 上传失败 → 降级为 base64 data URL
        logger.warning(f"⚠️ 图床上传失败 ({e})，降级为 base64 传输")
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{mime};base64,{encoded}"
    except Exception as e:
        # 其他意外错误也降级，保证节点不会因图床问题崩溃
        logger.warning(f"⚠️ 图床上传遭遇意外错误 ({e})，降级为 base64 传输")
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{mime};base64,{encoded}"


class XinbaoModelScopeImageGenerate:
    def __init__(self):
        self.image_codec = ImageCodec(logger, ensure_not_interrupted=_ensure_not_interrupted)
        self.task_runner = BatchGenerationRunner(
            logger,
            _ensure_not_interrupted,
            lambda total: comfy.utils.ProgressBar(total),
        )

    @classmethod
    def INPUT_TYPES(cls):
        bypass_default = _CONFIG_MANAGER.should_bypass_proxy()
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "A beautiful landscape"}),
                "modelscope_api_key": ("STRING", {"default": "", "multiline": False, "tooltip": "ModelScope API Key"}),
            },
            "optional": {
                "model": (["Tongyi-MAI/Z-Image-Turbo"], {"default": "Tongyi-MAI/Z-Image-Turbo"}),
                "custom_model": ("STRING", {"default": "", "multiline": False, "tooltip": "自定义模型名称（留空按上方选择模型）"}),
                "negative_prompt": ("STRING", {"multiline": True, "default": ""}),
                "width": ("INT", {"default": 1280, "min": 64, "max": 2048, "step": 64}),
                "height": ("INT", {"default": 1280, "min": 64, "max": 2048, "step": 64}),
                "steps": ("INT", {"default": 10, "min": 1, "max": 100}),
                "guidance": ("FLOAT", {"default": 1.5, "min": 1.0, "max": 20.0, "step": 0.1}),
                "seed": ("INT", {"default": -1, "min": -1, "max": 2_147_483_647}),
                "batch_size": ("INT", {"default": 1, "min": 1, "max": MAX_BATCH_SIZE}),
                "max_concurrency": ("INT", {"default": 4, "min": 1, "max": 10, "step": 1, "display": "并发上限(1-10)"}),
                "绕过代理": ("BOOLEAN", {"default": bypass_default}),
                "禁用SSL验证": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "generate"
    CATEGORY = "❤️‍🔥心宝专用"

    def generate(
        self,
        prompt: str,
        modelscope_api_key: str,
        model: str = "Tongyi-MAI/Z-Image-Turbo",
        custom_model: str = "",
        negative_prompt: str = "",
        width: int = 1280,
        height: int = 1280,
        steps: int = 9,
        guidance: float = 1.5,
        seed: int = -1,
        batch_size: int = 1,
        max_concurrency: int = 4,
        绕过代理: bool = False,
        禁用SSL验证: bool = False,
    ):
        key = _resolve_api_key(
            modelscope_api_key,
            fallback_loader=_CONFIG_MANAGER.load_modelscope_api_key,
            message="缺少魔搭 API Key：请在节点或“心宝❤全局密钥管理”中填写，或在 config.ini 中配置 modelscope_api_key",
        )
        logger.info("已按 节点/全局 → config.ini 顺序解析魔搭密钥")
        batch = _normalize_batch(batch_size)
        concurrency = _normalize_concurrency(max_concurrency, batch)

        headers = _build_headers(key)

        selected_model = (custom_model or "").strip() or model

        base_payload = {
            "model": selected_model,
            "prompt": prompt,
            "size": f"{width}x{height}",
            "steps": steps,
            "guidance": guidance,
        }
        if negative_prompt.strip():
            base_payload["negative_prompt"] = negative_prompt.strip()

        base_seed = _normalize_seed(seed)

        logger.info(
            f"准备调用 ModelScope 文生图: 模型={selected_model}, 批量={batch}, 并发={concurrency}, 种子={base_seed}, Key={_mask_key(key)}"
        )

        tasks: List[tuple[int, int, dict]] = []
        for i in range(batch):
            current_seed = base_seed + i if seed >= 0 else _normalize_seed(-1)
            single_payload = dict(base_payload)
            single_payload["n"] = 1
            single_payload["seed"] = current_seed
            logger.info(f"批次 {i + 1}/{batch}，种子={current_seed}")
            tasks.append((i, current_seed, single_payload))

        def worker(task: tuple[int, int, dict]) -> dict:
            index, current_seed, payload = task
            _ensure_not_interrupted()
            try:
                local_session = _create_session(绕过代理, 禁用SSL验证)
                tensor = _submit_and_fetch_images(
                    local_session,
                    headers,
                    payload,
                    target_size=(width, height),
                    batch_size=1,
                    ensure_not_interrupted=_ensure_not_interrupted,
                )
                return {"index": index, "seed": current_seed, "success": True, "tensor": tensor}
            except comfy.model_management.InterruptProcessingException:
                logger.warning(f"批次 {index + 1} 已取消")
                raise
            except Exception as exc:  # noqa: BLE001
                err_text = str(exc)
                logger.error(f"批次 {index + 1} 失败: {err_text[:120]}")
                return {"index": index, "seed": current_seed, "success": False, "error": err_text[:200], "tensor": None}

        def progress_callback(result: dict, completed: int, total: int, progress_bar: object):
            batch_label = result.get("index", -1) + 1
            if result.get("success"):
                logger.success(f"[{completed}/{total}] 批次 {batch_label} 完成")
            else:
                logger.error(f"[{completed}/{total}] 批次 {batch_label} 失败")

            preview_tensor = result.get("tensor")
            if result.get("success") and preview_tensor is not None:
                preview_tuple = self.image_codec.build_preview_tuple(preview_tensor, batch_label - 1)
                if preview_tuple is not None:
                    progress_bar.update_absolute(completed, total, preview_tuple)
                    return
            progress_bar.update(1)

        results = self.task_runner.run(
            tasks,
            worker,
            batch,
            concurrency,
            continue_on_error=False,
            progress_callback=progress_callback,
        )

        if not results:
            raise Exception("未生成任何结果")

        failures = [r for r in results if not r.get("success")]
        if failures:
            first_error = failures[0].get("error") or "生成失败"
            raise Exception(first_error)

        results.sort(key=lambda x: x["index"])
        tensors = [r["tensor"] for r in results if r.get("tensor") is not None]
        stacked = _stack_images(tensors)
        logger.success(f"文生图完成，获得 {stacked.shape[0]} 张图片")
        return (stacked,)


class XinbaoModelScopeImageEdit:
    @classmethod
    def INPUT_TYPES(cls):
        bypass_default = _CONFIG_MANAGER.should_bypass_proxy()
        return {
            "required": {
                "image": ("IMAGE",),
                "prompt": ("STRING", {"multiline": True, "default": "修改图片中的细节"}),
                "api_key": ("STRING", {"default": "", "multiline": False}),
            },
            "optional": {
                "negative_prompt": ("STRING", {"multiline": True, "default": ""}),
                "width": ("INT", {"default": 1024, "min": 64, "max": 2048, "step": 64}),
                "height": ("INT", {"default": 1024, "min": 64, "max": 2048, "step": 64}),
                "steps": ("INT", {"default": 30, "min": 1, "max": 100}),
                "guidance": ("FLOAT", {"default": 5.0, "min": 1.0, "max": 20.0, "step": 0.1}),
                "seed": ("INT", {"default": -1, "min": -1, "max": 2_147_483_647}),
                "batch_size": ("INT", {"default": 1, "min": 1, "max": MAX_BATCH_SIZE}),
                "绕过代理": ("BOOLEAN", {"default": bypass_default}),
                "禁用SSL验证": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "edit"
    CATEGORY = "❤️‍🔥心宝专用"

    def edit(
        self,
        image: torch.Tensor,
        prompt: str,
        api_key: str,
        negative_prompt: str = "",
        width: int = 1024,
        height: int = 1024,
        steps: int = 30,
        guidance: float = 5.0,
        seed: int = -1,
        batch_size: int = 1,
        绕过代理: bool = False,
        禁用SSL验证: bool = False,
    ):
        key = _resolve_api_key(
            api_key,
            fallback_loader=_CONFIG_MANAGER.load_modelscope_api_key,
            message="缺少魔搭 API Key：请在节点或“心宝❤全局密钥管理”中填写，或在 config.ini 中配置 modelscope_api_key",
        )
        logger.info("已按 节点/全局 → config.ini 顺序解析魔搭密钥")
        batch = _normalize_batch(batch_size)
        session = _create_session(绕过代理, 禁用SSL验证)
        headers = _build_headers(key)

        image_url = _upload_image_get_url(session, image)
        if image_url:
            logger.info("已上传参考图像，使用远端 URL 进行编辑")
            image_field = {"image_url": image_url}
        else:
            logger.warning("图像上传失败，回退为 base64 传输")
            image_field = {"image": _tensor_to_base64_data_url(image)}

        base_payload = {
            "model": "Qwen/Qwen-Image",
            "prompt": prompt,
            "steps": steps,
            "guidance": guidance,
            "size": f"{width}x{height}",
            **image_field,
        }
        if negative_prompt.strip():
            base_payload["negative_prompt"] = negative_prompt.strip()

        images: List[torch.Tensor] = []
        base_seed = _normalize_seed(seed)

        logger.info(f"准备调用 ModelScope 图像编辑: 批量={batch}, 种子={base_seed}, Key={_mask_key(key)}")

        for i in range(batch):
            _ensure_not_interrupted()
            current_seed = base_seed + i if seed >= 0 else _normalize_seed(-1)
            single_payload = dict(base_payload)
            single_payload["n"] = 1
            single_payload["seed"] = current_seed
            logger.info(f"批次 {i + 1}/{batch}，种子={current_seed}")
            single_image = _submit_and_fetch_images(
                session,
                headers,
                single_payload,
                target_size=(width, height),
                batch_size=1,
                ensure_not_interrupted=_ensure_not_interrupted,
            )
            images.append(single_image)

        stacked = _stack_images(images)
        logger.success(f"图像编辑完成，获得 {stacked.shape[0]} 张图片")
        return (stacked,)


class XinbaoModelScopeCaption:
    @classmethod
    def INPUT_TYPES(cls):
        bypass_default = _CONFIG_MANAGER.should_bypass_proxy()
        return {
            "required": {
                "image": ("IMAGE",),
                "channel": (
                    [CHANNEL_XINBAO_TEST, CHANNEL_BANANA, CHANNEL_MODAO],
                    {"default": CHANNEL_XINBAO_TEST, "display": "渠道"},
                ),
                "banana_api_key": (
                    "STRING",
                    {"default": "", "multiline": False, "display": "香蕉渠道 API Key"},
                ),
                "modelscope_api_key": (
                    "STRING",
                    {"default": "", "multiline": False, "display": "魔搭社区 API Key"},
                ),
            },
            "optional": {
                "image_2": (
                    "IMAGE",
                    {
                        "display": "附加图像 2（可选）",
                    },
                ),
                "image_3": (
                    "IMAGE",
                    {
                        "display": "附加图像 3（可选）",
                    },
                ),
                "system_prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "你是一名专业的图像描述助手，请根据用户输入详细描述图片中的主体、背景和风格",
                        "display": "系统提示词",
                    },
                ),
                "user_prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "详细描述图片中的主体、背景和风格",
                        "display": "用户提示词",
                    },
                ),
                "model": (CAPTION_MODELS, {
                    "default": "gemini-3-flash-c",
                    "banana_models": BANANA_CAPTION_MODELS_DISPLAY,
                    "modao_models": MODAO_CAPTION_MODELS,
                }),
                "max_tokens": ("INT", {"default": 8192, "min": 100, "max": 8192}),
                "temperature": ("FLOAT", {"default": 0.7, "min": 0.1, "max": 2.0, "step": 0.1}),
                "seed": (
                    "INT",
                    {
                        "default": -1,
                        "min": -1,
                        "max": 2_147_483_647,
                        "control_after_generate": True,
                        "display": "随机种子",
                    },
                ),
                "禁用SSL验证": ("BOOLEAN", {"default": False}),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, channel=None, input_types=None, **kwargs):
        # [兼容性] 接受旧工作流中残留的旧渠道名称
        if channel is not None and channel in {
            CHANNEL_XINBAO_TEST_LEGACY,
            CHANNEL_XINBAO_TEST_LEGACY_ALT,
            CHANNEL_BANANA_LEGACY,
        }:
            return True
        return True

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("description",)
    FUNCTION = "caption"
    CATEGORY = "❤️‍🔥心宝专用"

    def caption(
        self,
        image: torch.Tensor,
        channel: str = CHANNEL_BANANA,
        banana_api_key: str = "",
        modelscope_api_key: str = "",
        image_2: Optional[torch.Tensor] = None,
        image_3: Optional[torch.Tensor] = None,
        system_prompt: str = "你是一名专业的图像描述助手，请根据用户输入详细描述图片中的主体、背景和风格",
        user_prompt: str = "详细描述图片中的主体、背景和风格",
        model: str = "gemini-3-flash-c",
        max_tokens: int = 8192,
        temperature: float = 0.7,
        seed: int = -1,
        绕过代理: bool = False,
        禁用SSL验证: bool = False,
    ):
        try:
            _ensure_not_interrupted()
            logger.info(f"多模态图像描述调用，seed={seed}")
            selected_channel = _normalize_channel(channel)
            if selected_channel == CHANNEL_MODAO:
                key, base_urls, key_source = _resolve_modao_caption_key(modelscope_api_key)
                verify_ssl_flag = not 禁用SSL验证
            elif selected_channel == CHANNEL_XINBAO_TEST:
                key, base_urls, key_source = _resolve_xinbao_test_caption_key(banana_api_key)
                verify_ssl_flag = not 禁用SSL验证
            else:
                key, base_urls, key_source = _resolve_banana_caption_key(banana_api_key)
                verify_ssl_flag = not 禁用SSL验证
            base_urls = [url.strip() for url in base_urls if url]
            if not base_urls:
                raise Exception("未获取到有效请求线路，请检查配置")
            logger.info(f"API Key 已解析（来源: {key_source}）")

            if selected_channel == CHANNEL_BANANA:
                _ensure_not_interrupted()
                kv_result = _KV_AUTH_CLIENT.check(
                    key,
                    verify_ssl=verify_ssl_flag,
                    disable_ssl_override=禁用SSL验证,
                )
                if not kv_result.allowed:
                    raise Exception(kv_result.user_message or "访问受限，请稍后重试")
                if kv_result.fail_open:
                    logger.warning("[KVAuth] 远端异常已按 fail-open 放行，请关注服务状态")
                _ensure_not_interrupted()

            model_name = _normalize_model_for_channel(selected_channel, model)
            session = _create_session(绕过代理, 禁用SSL验证)
            headers = _build_headers(key)

            images = [tensor for tensor in (image, image_2, image_3) if tensor is not None]
            _ensure_not_interrupted()
            uploader = ImageUploader(
                session=session,
                logger=logger,
                interrupt_checker=_ensure_not_interrupted,
            )
            image_data_urls = [
                _upload_caption_image(tensor, uploader, max_size=2048)
                for tensor in images
            ]
            _ensure_not_interrupted()
            system_text = (system_prompt or "").strip()
            user_text = (user_prompt or "").strip() or "详细描述图片中的主体、背景和风格"

            messages = []
            if system_text:
                messages.append(
                    {
                        "role": "system",
                        "content": [{"type": "text", "text": system_text}],
                    }
                )
            user_content = [{"type": "text", "text": user_text}]
            for data_url in image_data_urls:
                user_content.append({"type": "image_url", "image_url": {"url": data_url}})
            messages.append({"role": "user", "content": user_content})

            base_seed = _normalize_seed(seed)

            payload = {
                "model": model_name,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "seed": base_seed,
            }

            # JSON 结构化输出：model_id 以 "json" 结尾时注入 response_format
            # 命名约定：TOML 中 JSON 变体的 model_id 统一以 "json" 结尾（如 xxx-dsxqjson）
            if model_name.endswith("json"):
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "prompts_output",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "prompts": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                }
                            },
                            "required": ["prompts"],
                            "additionalProperties": False,
                        },
                    },
                }
                logger.info("检测到 JSON 模型后缀，已注入 response_format 约束")

            logger.info(
                f"准备调用 ModelScope 图像描述: 渠道={selected_channel}, 模型={model_name}, Key={_mask_key(key)}"
            )
            response = _send_caption_request_with_routes(
                session,
                payload,
                headers,
                base_urls,
                selected_channel,
                model_name,
                ensure_not_interrupted=_ensure_not_interrupted,
            )
            _ensure_not_interrupted()

            data = _parse_json_response_or_raise(response, "描述生成失败")
            _ensure_not_interrupted()
            choices = data.get("choices") or []
            if not choices or not isinstance(choices, list):
                payload_error = _summarize_payload_error(data)
                if payload_error:
                    raise Exception(f"描述生成失败: {payload_error}")
                raise Exception(f"描述生成失败: 未返回有效结果（choices 为空）。响应摘要: {_summarize_payload(data)}")
            message = choices[0].get("message", {})
            content = message.get("content")
            if isinstance(content, str):
                description = content
            elif isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                description = "".join(parts)
            else:
                description = ""

            if not description:
                payload_error = _summarize_payload_error(data)
                if payload_error:
                    raise Exception(f"描述生成失败: {payload_error}")
                raise Exception(f"描述生成失败: 返回内容中未提取到文本。响应摘要: {_summarize_payload(data)}")

            _ensure_not_interrupted()
            logger.success("图像描述生成完成")
            return (description,)
        except comfy.model_management.InterruptProcessingException:
            logger.warning("图像描述任务已取消")
            raise


NODE_CLASS_MAPPINGS = {
    "XinbaoModelScopeImageGenerate": XinbaoModelScopeImageGenerate,
    "XinbaoModelScopeCaption": XinbaoModelScopeCaption,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "XinbaoModelScopeImageGenerate": "心宝❤魔搭文生图",
    "XinbaoModelScopeCaption": "心宝❤多模态LLM反推",
}

# 暂时下线图像编辑节点，后续需要恢复时可将其重新放回映射
