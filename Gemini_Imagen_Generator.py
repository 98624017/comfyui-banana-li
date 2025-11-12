import json
import requests
import base64
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
import numpy as np
import torch
from typing import List, Dict, Optional, Tuple, Any
import re
import random
import time
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
import threading
import os
import configparser
import asyncio
import hashlib
from datetime import datetime
from functools import partial
from collections import OrderedDict
from requests.adapters import HTTPAdapter
from aiohttp import web

from server import PromptServer
import comfy.utils
import comfy.model_management

# 导入新的日志系统
try:
    from .logger import logger
except ImportError:
    # 如果相对导入失败，尝试绝对导入
    import sys
    import os
    # 确保当前目录在 sys.path 中
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    from logger import logger



def retry_with_backoff(tries=3, delay=2, backoff=2, retriable_exceptions=None,
                       fast_fail_threshold=15.0):
    """
    智能重试装饰器，区分快速失败和慢速失败

    Args:
        tries: 最大重试次数（包括初次尝试）
        delay: 初始延迟时间（秒）
        backoff: 退避倍数
        retriable_exceptions: 可重试的异常类型列表，默认为网络相关异常
        fast_fail_threshold: 快速失败阈值（秒），超过此时间的失败不重试
    """
    if retriable_exceptions is None:
        # 默认重试可恢复的错误（5xx、网络超时、连接中断）
        retriable_exceptions = (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.HTTPError,
            requests.exceptions.ChunkedEncodingError,  # 响应分块传输中断
            requests.exceptions.RequestException,  # 兜底的网络异常
            ConnectionResetError,  # 连接被重置
            BrokenPipeError,  # 管道破裂
        )

    def decorator(func):
        from functools import wraps

        @wraps(func)
        def wrapper(*args, **kwargs):
            mtries, mdelay = tries, delay

            for attempt in range(mtries):
                attempt_start = time.time()
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    attempt_duration = time.time() - attempt_start
                    error_str = str(e)

                    # 检查是否是可重试的异常
                    is_retriable = False
                    error_type = "未知错误"

                    # 检查超时错误 - 如果耗时超过阈值,不重试
                    if "请求超时" in error_str or isinstance(e, requests.exceptions.Timeout):
                        if attempt_duration >= fast_fail_threshold:
                            logger.error(f"请求超时 ({attempt_duration:.1f}s)，耗时过长，不重试")
                            raise
                        is_retriable = True
                        error_type = "超时错误(快速)"

                    # 检查 5xx 服务器错误 - 只有快速返回的才重试
                    elif "API返回 5" in error_str:
                        if attempt_duration >= fast_fail_threshold:
                            logger.error(f"服务器错误 ({attempt_duration:.1f}s)，服务器处理时间过长，不重试")
                            raise
                        is_retriable = True
                        error_type = "5xx服务器错误"

                    # 检查 429 限流错误 - 值得重试
                    elif "API返回 429" in error_str or "rate limit" in error_str.lower():
                        is_retriable = True
                        error_type = "API限流"
                        mdelay = max(mdelay, 5)  # 限流时至少等5秒

                    # 检查 502/503/504 网关错误 - 临时性问题,值得重试
                    elif any(code in error_str for code in ["502", "503", "504"]):
                        if attempt_duration < fast_fail_threshold:
                            is_retriable = True
                            error_type = "网关错误"

                    # 检查连接中断相关错误
                    elif "IncompleteRead" in str(type(e)) or "IncompleteRead" in error_str:
                        is_retriable = True
                        error_type = "响应不完整"

                    # 检查响应过早结束
                    elif "Response ended prematurely" in error_str:
                        is_retriable = True
                        error_type = "响应中断"

                    # 检查连接错误
                    elif isinstance(e, requests.exceptions.ConnectionError):
                        if attempt_duration < fast_fail_threshold:
                            is_retriable = True
                            error_type = "连接错误"

                    # 检查是否是预定义的可重试异常
                    elif isinstance(e, retriable_exceptions):
                        if attempt_duration < fast_fail_threshold:
                            is_retriable = True
                            error_type = "网络异常"

                    # 最后一次尝试或不可重试的错误，直接抛出
                    if attempt == mtries - 1 or not is_retriable:
                        if not is_retriable:
                            logger.error(f"不可重试的错误: {error_str[:100]}")
                        raise

                    # 打印重试信息
                    logger.warning(f"{error_type} (尝试 {attempt + 1}/{mtries}, 耗时 {attempt_duration:.1f}s): {error_str[:80]}")
                    logger.info(f"等待 {mdelay:.1f}s 后重试...")

                    # 等待后重试
                    time.sleep(mdelay)
                    mdelay *= backoff  # 指数退避

        return wrapper
    return decorator


class BananaImageNode:
    """
    ComfyUI节点: NanoBanana图像生成，适配Gemini兼容端点
    支持从config.ini读取API Key
    """

    DEFAULT_API_BASE_URL = "https://xinbaoapi.feng1994.xin"
    TOKENS_PER_RATE = 100000
    CURRENCY_PER_RATE = 0.20
    BASE_COST_PER_TOKEN = CURRENCY_PER_RATE / TOKENS_PER_RATE
    _BALANCE_CACHE: Dict[str, Dict[str, Any]] = {}
    _BALANCE_CACHE_LOCK = threading.Lock()
    _BALANCE_ROUTE_REGISTERED = False
    _BALANCE_ROUTE_TIMER: Optional[threading.Timer] = None
    _BALANCE_CACHE_TTL = 60.0
    _IMAGE_B64_CACHE: "OrderedDict[str, str]" = OrderedDict()
    _IMAGE_CACHE_LOCK = threading.Lock()
    _IMAGE_CACHE_SIZE = 16
    _ERROR_FONT_CACHE: Dict[int, ImageFont.ImageFont] = {}
    _THREAD_LOCAL = threading.local()
    _SESSION_INIT_LOCK = threading.Lock()
    _SESSION_INITIALIZED = False
    _PLACEHOLDER_KEYS = {
        "your-api-key-here",
        "your_api_key_here",
        "yourapikeyhere"
    }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "text")
    FUNCTION = "generate_images"
    OUTPUT_NODE = True
    CATEGORY = "image/ai_generation"

    @classmethod
    def _sanitize_api_key(cls, api_key: Optional[str]) -> Optional[str]:
        if not api_key:
            return None
        cleaned = api_key.strip()
        if not cleaned:
            return None

        normalized = cleaned.lower()
        compact = re.sub(r"[\s_-]+", "", normalized)
        if normalized in cls._PLACEHOLDER_KEYS or compact in cls._PLACEHOLDER_KEYS:
            return None
        return cleaned

    @staticmethod
    def _clamp_cost_factor(cost_factor: Optional[float]) -> float:
        if cost_factor is None:
            return 1.0
        try:
            value = float(cost_factor)
        except (TypeError, ValueError):
            return 1.0
        return max(0.0001, min(value, 100.0))

    @classmethod
    def _balance_cache_key(cls, api_base_url: str, api_key: str) -> str:
        normalized_url = (api_base_url or cls.DEFAULT_API_BASE_URL).rstrip("/").lower()
        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        return f"{normalized_url}|{digest}"

    @classmethod
    def _tensor_cache_key(cls, tensor: Optional[torch.Tensor] = None,
                          np_data: Optional[np.ndarray] = None) -> Optional[str]:
        if tensor is None and np_data is None:
            return None
        try:
            target = np_data
            if target is None:
                target = tensor.detach().cpu().numpy()
            return hashlib.sha1(target.tobytes()).hexdigest()
        except Exception:
            return None

    @classmethod
    def _get_cached_image_b64(cls, cache_key: Optional[str]) -> Optional[str]:
        if not cache_key:
            return None
        with cls._IMAGE_CACHE_LOCK:
            value = cls._IMAGE_B64_CACHE.get(cache_key)
            if value is not None:
                cls._IMAGE_B64_CACHE.move_to_end(cache_key)
            return value

    @classmethod
    def _set_cached_image_b64(cls, cache_key: Optional[str], value: str) -> None:
        if not cache_key or not value:
            return
        with cls._IMAGE_CACHE_LOCK:
            cls._IMAGE_B64_CACHE[cache_key] = value
            cls._IMAGE_B64_CACHE.move_to_end(cache_key)
            while len(cls._IMAGE_B64_CACHE) > cls._IMAGE_CACHE_SIZE:
                cls._IMAGE_B64_CACHE.popitem(last=False)

    @classmethod
    def _store_balance_snapshot(cls, api_base_url: str, api_key: str, payload: Dict[str, Any]) -> None:
        sanitized_key = cls._sanitize_api_key(api_key)
        if not sanitized_key:
            return
        cache_key = cls._balance_cache_key(api_base_url, sanitized_key)
        snapshot = {
            "payload": payload,
            "fetched_at": time.time()
        }
        with cls._BALANCE_CACHE_LOCK:
            cls._BALANCE_CACHE[cache_key] = snapshot

    @classmethod
    def _get_balance_snapshot(cls, api_base_url: str, api_key: str) -> Optional[Dict[str, Any]]:
        sanitized_key = cls._sanitize_api_key(api_key)
        if not sanitized_key:
            return None
        cache_key = cls._balance_cache_key(api_base_url, sanitized_key)
        with cls._BALANCE_CACHE_LOCK:
            return cls._BALANCE_CACHE.get(cache_key)

    @classmethod
    def _snapshot_age_seconds(cls, snapshot: Optional[Dict[str, Any]]) -> Optional[float]:
        if not snapshot:
            return None
        fetched_at = snapshot.get("fetched_at")
        if not fetched_at:
            return None
        return max(0.0, time.time() - fetched_at)

    @classmethod
    def _is_balance_snapshot_stale(cls, snapshot: Optional[Dict[str, Any]]) -> bool:
        age = cls._snapshot_age_seconds(snapshot)
        if age is None:
            return True
        return age > cls._BALANCE_CACHE_TTL

    @classmethod
    def _schedule_route_registration(cls):
        if cls._BALANCE_ROUTE_TIMER is not None and cls._BALANCE_ROUTE_TIMER.is_alive():
            return

        def _retry():
            cls._BALANCE_ROUTE_TIMER = None
            cls.ensure_balance_route()

        timer = threading.Timer(1.0, _retry)
        timer.daemon = True
        cls._BALANCE_ROUTE_TIMER = timer
        timer.start()

    @staticmethod
    def _parse_bool(value: Optional[str]) -> bool:
        if value is None:
            return False
        return value.lower() in {"1", "true", "yes", "on"}

    @classmethod
    def ensure_balance_route(cls):
        if cls._BALANCE_ROUTE_REGISTERED:
            return
        prompt_server = getattr(PromptServer, "instance", None)
        if prompt_server is None:
            cls._schedule_route_registration()
            return

        @prompt_server.routes.get("/banana/token_usage")
        async def handle_token_usage(request):
            base_url = request.rel_url.query.get("base_url", cls.DEFAULT_API_BASE_URL)
            refresh = cls._parse_bool(request.rel_url.query.get("refresh"))
            # 优先使用前端传递的API Key,如果没有则使用配置文件中的Key
            api_key_from_request = request.rel_url.query.get("api_key", "").strip()
            api_key = cls._sanitize_api_key(api_key_from_request) or cls._sanitize_api_key(cls.load_config())
            cost_factor = cls.load_cost_factor_from_config()
            # 运行于 aiohttp handler 上下文,优先使用运行中的 loop
            loop = asyncio.get_running_loop()

            if not refresh:
                snapshot = cls._get_balance_snapshot(base_url, api_key)
                if snapshot is None:
                    return web.json_response({
                        "success": False,
                        "message": "暂无余额缓存，请点击“查询余额”按钮刷新",
                        "cached": False,
                        "stale": True
                    })

                summary = cls.format_balance_summary(snapshot, cost_factor, include_stale_hint=True)
                return web.json_response({
                    "success": True,
                    "data": snapshot.get("payload", {}).get("data"),
                    "raw": snapshot.get("payload"),
                    "summary": summary,
                    "cost_factor": cost_factor,
                    "cached": True,
                    "stale": cls._is_balance_snapshot_stale(snapshot)
                })

            try:
                await loop.run_in_executor(
                    None,
                    partial(cls.fetch_token_usage, base_url, api_key)
                )
                snapshot = cls._get_balance_snapshot(base_url, api_key)
                if snapshot is None:
                    raise RuntimeError("余额缓存更新失败")
                summary = cls.format_balance_summary(snapshot, cost_factor)
                return web.json_response({
                    "success": True,
                    "data": snapshot.get("payload", {}).get("data"),
                    "raw": snapshot.get("payload"),
                    "summary": summary,
                    "cost_factor": cost_factor,
                    "cached": False,
                    "stale": False
                })
            except Exception as exc:
                return web.json_response(
                    {"success": False, "message": str(exc)},
                    status=400
                )

        cls._BALANCE_ROUTE_REGISTERED = True

    @staticmethod
    def _format_number(value: Optional[float]) -> str:
        if value is None:
            return "-"
        if isinstance(value, (int, float)):
            return f"{value:,.0f}"
        return str(value)

    @classmethod
    def _format_cost(cls, tokens: Optional[float], cost_factor: float) -> str:
        if tokens is None:
            return "-"
        try:
            tokens_value = float(tokens)
        except (TypeError, ValueError):
            return "-"
        # 使用 cost_factor 的倒数: 当配置为 1.67 时,实际除以 1.67
        yuan = tokens_value * cls.BASE_COST_PER_TOKEN / cost_factor
        return f"¥{yuan:.4f}"

    @classmethod
    def _format_expiry(cls, timestamp: Optional[int]) -> str:
        if not timestamp or timestamp <= 0:
            return "不过期"
        try:
            dt = datetime.fromtimestamp(timestamp)
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(timestamp)

    @classmethod
    def format_balance_summary(cls, snapshot: Dict[str, Any], cost_factor: float = 1.0,
                               include_stale_hint: bool = False) -> str:
        cost_factor = cls._clamp_cost_factor(cost_factor)
        data = snapshot.get("payload", {}).get("data", {})
        available = cls._format_number(data.get("total_available"))
        used = cls._format_number(data.get("total_used"))
        granted = cls._format_number(data.get("total_granted"))
        unlimited = "是" if data.get("unlimited_quota") else "否"
        expires = cls._format_expiry(data.get("expires_at"))
        available_cost = cls._format_cost(data.get("total_available"), cost_factor)
        used_cost = cls._format_cost(data.get("total_used"), cost_factor)
        fetched_at = snapshot.get("fetched_at")
        if fetched_at:
            fetched_text = datetime.fromtimestamp(fetched_at).strftime("%H:%M")
        else:
            fetched_text = datetime.now().strftime("%H:%M")
        summary_lines = [
            f"🔑 查询时间 {fetched_text}",
            f"估算费用: 可用 {available_cost} / 已用 {used_cost} (仅参考)",
            f"到期: {expires}"
        ]
        if include_stale_hint and cls._is_balance_snapshot_stale(snapshot):
            age = cls._snapshot_age_seconds(snapshot)
            if age is not None:
                summary_lines.append(
                    f"⚠️ 余额信息已 {int(age)}s 未刷新，点击节点按钮获取最新数据"
                )
        return "\n".join(summary_lines)

    @classmethod
    def get_cached_balance_text(cls, api_base_url: str, api_key: str, cost_factor: float = 1.0) -> Optional[str]:
        snapshot = cls._get_balance_snapshot(api_base_url, api_key)
        if not snapshot:
            return None
        try:
            return cls.format_balance_summary(snapshot, cost_factor, include_stale_hint=True)
        except Exception:
            return None

    @classmethod
    def _get_thread_session(cls) -> requests.Session:
        """
        获取线程专属的 HTTP Session，避免 requests.Session 在线程间复用导致的竞态
        """
        session = getattr(cls._THREAD_LOCAL, "session", None)
        if session is not None:
            return session

        pool_size = max(4, cls.load_max_workers_from_config())
        session = requests.Session()

        adapter = HTTPAdapter(
            pool_connections=pool_size,
            pool_maxsize=pool_size,
            pool_block=False
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        keepalive_timeout = cls._get_keepalive_timeout()
        session.headers.update({
            'Connection': 'keep-alive',
            'Keep-Alive': f'timeout={keepalive_timeout}, max=100'
        })

        setattr(cls._THREAD_LOCAL, "session", session)

        with cls._SESSION_INIT_LOCK:
            if not cls._SESSION_INITIALIZED:
                logger.info(f"HTTP 连接池已初始化: pool_size={pool_size}, keepalive_timeout={keepalive_timeout}s")
                cls._SESSION_INITIALIZED = True

        return session

    @classmethod
    def fetch_token_usage(cls, api_base_url: str, api_key: str, timeout: int = 15) -> Dict[str, Any]:
        sanitized_key = cls._sanitize_api_key(api_key)
        if not sanitized_key:
            raise ValueError("未配置有效的 API Key")
        base_url = (api_base_url or cls.DEFAULT_API_BASE_URL).rstrip("/")
        url = f"{base_url}/api/usage/token"
        headers = {"Authorization": f"Bearer {sanitized_key}"}
        session = cls._get_thread_session()
        # 直接发送请求,Session会自动管理连接池
        response = session.get(url, headers=headers, timeout=timeout)
        try:
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                raise RuntimeError(f"余额查询失败: HTTP {response.status_code}") from exc
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise RuntimeError("余额查询失败: 响应非 JSON") from exc
        finally:
            # 确保响应内容被完全读取,连接才能被复用
            response.close()
        cls._store_balance_snapshot(api_base_url, sanitized_key, payload)
        return payload

    @staticmethod
    def _get_config_path() -> str:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(current_dir, "config.ini")

    @classmethod
    def load_config(cls):
        """从config.ini加载API key"""
        config_path = cls._get_config_path()
        
        config = configparser.ConfigParser()
        
        # 默认API key
        default_api_key = "your-api-key-here"
        
        # 尝试读取配置文件
        if os.path.exists(config_path):
            try:
                config.read(config_path, encoding='utf-8')
                if config.has_section('gemini'):
                    return config.get('gemini', 'api_key', fallback=default_api_key)
            except Exception as e:
                logger.warning(f"读取配置文件失败: {e}")
        else:
            # 创建示例配置文件
            try:
                cpu_limit = max(1, os.cpu_count() or 4)
                default_workers = min(8, cpu_limit)
                config['gemini'] = {
                    'api_key': 'your-api-key-here',
                    'balance_cost_factor': '0.6',
                    'max_workers': str(default_workers)
                }
                with open(config_path, 'w', encoding='utf-8') as f:
                    config.write(f)
                logger.success(f"已创建示例配置文件: {config_path}")
                logger.info(f"请编辑文件并填入你的 API Key")
            except Exception as e:
                logger.warning(f"创建配置文件失败: {e}")
        
        return default_api_key

    @classmethod
    def load_cost_factor_from_config(cls) -> float:
        config_path = cls._get_config_path()
        config = configparser.ConfigParser()
        if os.path.exists(config_path):
            try:
                config.read(config_path, encoding="utf-8")
                if config.has_section('gemini'):
                    value = config.getfloat('gemini', 'balance_cost_factor', fallback=0.6)
                    return cls._clamp_cost_factor(value)
            except Exception as e:
                logger.warning(f"读取 config 中的 balance_cost_factor 失败: {e}")
        return 0.6

    @classmethod
    def load_max_workers_from_config(cls) -> int:
        cpu_limit = max(1, os.cpu_count() or 1)
        default_workers = min(8, cpu_limit)
        config_path = cls._get_config_path()
        config = configparser.ConfigParser()
        if os.path.exists(config_path):
            try:
                config.read(config_path, encoding="utf-8")
                if config.has_section('gemini'):
                    value = config.getint('gemini', 'max_workers', fallback=default_workers)
                    return max(1, min(value, cpu_limit))
            except Exception as e:
                logger.warning(f"读取 config 中的 max_workers 失败: {e}")
        return default_workers

    @classmethod
    def _get_keepalive_timeout(cls) -> int:
        """
        从 config.ini 读取 Keep-Alive 超时时间
        默认 30 秒，兼容大多数防火墙/NAT 环境
        """
        config_path = cls._get_config_path()
        config = configparser.ConfigParser()

        if os.path.exists(config_path):
            try:
                config.read(config_path, encoding="utf-8")
                if config.has_section('gemini'):
                    timeout = config.getint('gemini', 'keepalive_timeout', fallback=30)
                    # 限制在合理范围：10-120 秒
                    return max(10, min(timeout, 120))
            except Exception as e:
                logger.warning(f"读取 keepalive_timeout 失败: {e}")

        return 30  # 默认 30 秒

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {
                    "multiline": True,
                    "default": "Peace and love"
                }),
                "api_base_url": ("STRING", {
                    "default": "https://xinbaoapi.feng1994.xin"
                }),
                "api_key": ("STRING", {
                    "default": "",
                    "multiline": False
                }),
                "model_type": ("STRING", {
                    "default": "gemini-2.5-flash-image"
                }),
                "batch_size": ("INT", {
                    "default": 1, "min": 1, "max": 8
                }),
                "aspect_ratio": (["Auto", "1:1", "9:16", "16:9", "21:9", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4"], {
                    "default": "Auto"
                }),
            },
            "optional": {
                "seed": ("INT", {
                    "default": -1,
                    "min": -1,
                    "max": 102400,
                    "control_after_generate": True
                }),
                "top_p": ("FLOAT", {
                    "default": 0.95, "min": 0.0, "max": 1.0, "step": 0.01
                }),
                "image_1": ("IMAGE",),
                "image_2": ("IMAGE",),
                "image_3": ("IMAGE",),
                "image_4": ("IMAGE",),
                "image_5": ("IMAGE",),
            }
        }
    
    def _extract_numpy_images(self, tensor: torch.Tensor) -> List[np.ndarray]:
        """将 Comfy 图像张量转换为按批次展开的 numpy 图像列表"""
        images: List[np.ndarray] = []
        if tensor is None:
            return images
        try:
            np_data = tensor.detach().cpu().numpy()
        except Exception as exc:
            logger.error(f"输入图像转换失败: {exc}")
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

    def tensor_to_base64(self, tensor: Optional[torch.Tensor] = None,
                         np_image: Optional[np.ndarray] = None) -> str:
        """将 tensor 或 numpy 图像转换为 base64"""
        if np_image is None:
            if tensor is None:
                raise ValueError("必须提供 tensor 或 numpy 图像数据用于编码")
            samples = self._extract_numpy_images(tensor)
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
        """将输入tensor预编码为Base64并复用缓存（支持批量图片）"""
        if not tensors:
            return []
        encoded_images: List[str] = []
        for tensor in tensors:
            if tensor is None:
                continue
            for sample in self._extract_numpy_images(tensor):
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
        """将单个base64转换为numpy数组"""
        try:
            img_data = base64.b64decode(b64_str)
            img = Image.open(BytesIO(img_data)).convert('RGB')
            img_array = np.array(img).astype(np.float32) / 255.0
            return img_array
        except Exception as e:
            logger.error(f"图片解码失败: {str(e)}")
            # 返回一个小的错误占位图
            return np.zeros((64, 64, 3), dtype=np.float32)

    def base64_to_tensor_parallel(self, base64_strings: List[str],
                                  log_prefix: Optional[str] = None,
                                  max_workers: Optional[int] = None) -> torch.Tensor:
        """并发解码多张图片,可选自定义日志前缀"""
        # 安全的列表空值检查,避免tensor布尔值歧义
        if not isinstance(base64_strings, list) or len(base64_strings) == 0:
            return torch.zeros((1, 64, 64, 3), dtype=torch.float32)
        
        decode_start = time.time()
        images = []
        worker_cap = max_workers if max_workers is not None else max(4, os.cpu_count() or 1)
        worker_cap = max(1, worker_cap)
        effective_workers = min(worker_cap, len(base64_strings))

        # 使用线程池并发解码
        self._ensure_not_interrupted()
        executor = ThreadPoolExecutor(max_workers=effective_workers)
        try:
            future_to_index = {executor.submit(self.base64_to_tensor_single, b64): i 
                             for i, b64 in enumerate(base64_strings)}
            
            # 按顺序收集结果
            results = [None] * len(base64_strings)
            try:
                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    try:
                        self._ensure_not_interrupted()
                        results[index] = future.result()
                    except comfy.model_management.InterruptProcessingException:
                        # 检测到中断，立即取消所有未完成的任务
                        for pending in future_to_index:
                            pending.cancel()
                        raise
                    except Exception as e:
                        logger.error(f"图片{index+1}解码异常: {str(e)}")
                        results[index] = np.zeros((64, 64, 3), dtype=np.float32)

                images = [r for r in results if r is not None]
            except comfy.model_management.InterruptProcessingException:
                # 确保在中断时关闭线程池
                executor.shutdown(wait=False, cancel_futures=True)
                raise
        finally:
            # 确保线程池被关闭
            if not executor._shutdown:
                executor.shutdown(wait=False, cancel_futures=True)

        decode_time = time.time() - decode_start
        logger.success(f"并发解码 {len(images)} 张图片完成，耗时: {decode_time:.2f}s")

        return torch.from_numpy(np.stack(images))

    def _build_preview_tuple(self, tensor: Optional[torch.Tensor], batch_index: int,
                              max_size: int = 512) -> Optional[Tuple[str, Image.Image, int]]:
        """将生成结果转换为 ComfyUI 所需的实时预览格式"""
        if tensor is None or tensor.shape[0] == 0:
            return None

        try:
            preview_tensor = tensor[0].detach().cpu()
            preview_tensor = torch.clamp(preview_tensor, 0.0, 1.0)
            preview_array = (preview_tensor.numpy() * 255).astype(np.uint8)

            # 兼容单通道/Alpha 通道输出
            if preview_array.ndim == 3 and preview_array.shape[2] == 1:
                preview_array = np.repeat(preview_array, 3, axis=2)
            elif preview_array.ndim == 2:
                preview_array = np.stack([preview_array] * 3, axis=2)

            preview_image = Image.fromarray(preview_array)
            return ("PNG", preview_image, max_size)
        except Exception as e:
            logger.error(f"实时预览生成失败: 批次 {batch_index + 1}: {str(e)[:80]}")
            return None

    @staticmethod
    def _ensure_not_interrupted():
        """统一的中断检查，复用 ComfyUI 原生取消机制"""
        comfy.model_management.throw_exception_if_processing_interrupted()

    def build_error_image_tensor(self, title: str, lines: List[str], size: Tuple[int, int] = (640, 640)) -> torch.Tensor:
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

        def line_height(font: ImageFont.ImageFont) -> int:
            if hasattr(font, "getmetrics"):
                ascent, descent = font.getmetrics()
                return ascent + descent + 4
            if hasattr(font, "size"):
                return font.size + 4
            return 20

        title_text = title.strip() if title else "错误提示"
        title_segments = self._wrap_text_segments(draw, title_text or "错误提示", font_title, max_text_width) or ["错误提示"]
        title_line_height = line_height(font_title)
        block_top = y - 6
        block_bottom = y + len(title_segments) * title_line_height + 6
        draw.rounded_rectangle([(margin - 10, block_top), (width - margin + 10, block_bottom)], radius=12, fill=accent)

        for segment in title_segments:
            draw.text((margin, y), segment, font=font_title, fill=title_color)
            y += title_line_height

        y += 6
        body_line_height = line_height(font_body)
        stop_render = False

        for line in lines:
            if stop_render:
                break
            segments = self._wrap_text_segments(draw, line, font_body, max_text_width) or [""]
            for segment in segments:
                if y > max_y:
                    stop_render = True
                    break
                draw.text((margin, y), segment, font=font_body, fill=text_color)
                y += body_line_height
            if stop_render:
                break
            y += 4

        arr = np.array(img).astype(np.float32) / 255.0
        return torch.from_numpy(arr).unsqueeze(0)

    def build_error_tensor_from_text(self, title: str, text: str) -> torch.Tensor:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in normalized.split("\n") if line.strip()]
        if not lines:
            lines = ["发生未知错误"]
        return self.build_error_image_tensor(title, lines)

    @classmethod
    def _get_error_font_paths(cls) -> List[str]:
        candidates = []
        windir = os.environ.get("WINDIR")
        if windir:
            for name in ("msyh.ttc", "msyh.ttf", "msjh.ttc", "simhei.ttf", "msmincho.ttc"):
                candidates.append(os.path.join(windir, "Fonts", name))
        candidates.append(os.path.join(os.path.dirname(__file__), "msyh.ttc"))
        return candidates

    @classmethod
    def _load_error_font(cls, size: int) -> ImageFont.ImageFont:
        cached = cls._ERROR_FONT_CACHE.get(size)
        if cached is not None:
            return cached
        for font_path in cls._get_error_font_paths():
            if font_path and os.path.exists(font_path):
                try:
                    font = ImageFont.truetype(font_path, size)
                    cls._ERROR_FONT_CACHE[size] = font
                    return font
                except Exception:
                    continue
        fallback = ImageFont.load_default()
        cls._ERROR_FONT_CACHE[size] = fallback
        return fallback

    def _wrap_text_segments(self, draw: ImageDraw.ImageDraw, text: str,
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

    def create_request_data(self, prompt: str, seed: int, aspect_ratio: str,
                          top_p: float = 0.65, input_images_b64: Optional[List[str]] = None) -> Dict:
        """构建请求数据"""
        if seed != -1:
            style_variations = [
                "detailed, high quality",
                "masterpiece, ultra detailed", 
                "photorealistic, stunning",
                "artistic, beautiful composition",
                "vibrant colors, sharp focus"
            ]
            style = style_variations[seed % len(style_variations)]
            final_prompt = f"{prompt}, {style}"
        else:
            final_prompt = prompt
            
        parts = [{"text": final_prompt}]
        
        if input_images_b64:
            for base64_image in input_images_b64:
                if base64_image:
                    parts.append({
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": base64_image
                        }
                    })
        
        generation_config = {
            "responseModalities": ["IMAGE"],
            "temperature": 0.8,
            "topP": top_p,  # 添加top_p参数
            "maxOutputTokens": 8192,
        }
        
        if aspect_ratio and aspect_ratio != "Auto":
            generation_config["imageConfig"] = {
                "aspectRatio": aspect_ratio
            }
        
        if seed != -1:
            generation_config["seed"] = seed
        
        return {
            "contents": [{
                "role": "user", 
                "parts": parts
            }],
            "generationConfig": generation_config
        }

    @retry_with_backoff(tries=3, delay=2, backoff=1, fast_fail_threshold=15.0)

    def send_request(self, api_key: str, request_data: Dict, model_type: str,
                    api_base_url: str, timeout = 180) -> Dict:
        """发送API请求"""
        endpoint = "generateContent"

        if "generativelanguage.googleapis.com" in api_base_url:
            url = f"{api_base_url.rstrip('/')}/v1beta/models/{model_type}:{endpoint}?key={api_key}"
            headers = {'Content-Type': 'application/json'}
        else:
            url = f"{api_base_url.rstrip('/')}/v1beta/models/{model_type}:{endpoint}"
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}',
            }

        headers['User-Agent'] = 'ComfyUI-Gemini-Node/2.1'

        request_start = time.time()
        try:
            session = self._get_thread_session()
            # 直接发送请求,Session会自动管理连接池
            response = session.post(url, json=request_data, headers=headers, timeout=timeout)
            request_time = time.time() - request_start
            try:
                if response.status_code != 200:
                    # 记录非200响应的耗时，帮助诊断
                    error_msg = f"API返回 {response.status_code}: {response.text[:200]}"
                    logger.warning(f"请求耗时 {request_time:.2f}s 后收到错误: {error_msg}")
                    raise Exception(error_msg)
                logger.info(f"API请求成功，耗时 {request_time:.2f}s")
                return response.json()
            finally:
                # 确保响应内容被完全读取,连接才能被复用
                response.close()
        except requests.exceptions.Timeout:
            timeout_duration = time.time() - request_start
            raise Exception(f"请求超时（设置{timeout}秒，实际等待{timeout_duration:.1f}秒）")
        except requests.exceptions.RequestException as e:
            raise Exception(f"网络错误: {str(e)}")

    def extract_content(self, response_data: Dict) -> Tuple[List[str], str]:
        """提取响应中的图像和文本"""
        base64_images = []
        text_content = ""
        
        candidates = response_data.get('candidates', [])
        if not candidates:
            raise ValueError("API响应中没有candidates字段")
        
        content = candidates[0].get('content', {})
        
        if content is None or content.get('parts') is None:
            return base64_images, text_content
        
        parts = content.get('parts', [])
        
        for part in parts:
            if 'text' in part:
                text_content += part['text']
            elif 'inlineData' in part and 'data' in part['inlineData']:
                base64_images.append(part['inlineData']['data'])
        
        if not base64_images and text_content:
            patterns = [
                r'data:image/[^;]+;base64,([A-Za-z0-9+/=]+)',
                r'!\[.*?\]\(data:image/[^;]+;base64,([A-Za-z0-9+/=]+)\)',
            ]
            for pattern in patterns:
                matches = re.findall(pattern, text_content)
                if matches:
                    base64_images.extend(matches)

        return base64_images, text_content.strip()

    def generate_single_image(self, args):
        """生成单张图片（用于并发）"""
        (
            i,
            current_seed,
            api_key,
            prompt,
            model_type,
            aspect_ratio,
            top_p,
            input_images_b64,
            api_base_url,
            timeout,
            stagger_delay,
            decode_workers,
        ) = args

        self._ensure_not_interrupted()
        if stagger_delay > 0:
            delay = i * stagger_delay
            if delay > 0:
                time.sleep(delay)

        thread_id = threading.current_thread().name
        logger.info(f"批次 {i+1} 开始请求...")

        try:
            self._ensure_not_interrupted()
            request_data = self.create_request_data(prompt, current_seed, aspect_ratio, top_p, input_images_b64)
            self._ensure_not_interrupted()
            response_data = self.send_request(api_key, request_data, model_type, api_base_url, timeout)
            self._ensure_not_interrupted()
            base64_images, text_content = self.extract_content(response_data)
            decoded_tensor = None
            decoded_count = 0
            if base64_images:
                self._ensure_not_interrupted()
                decoded_tensor = self.base64_to_tensor_parallel(
                    base64_images,
                    log_prefix=f"[{thread_id}] 批次 {i+1}",
                    max_workers=decode_workers
                )
                decoded_count = decoded_tensor.shape[0]

            logger.success(f"批次 {i+1} 完成 - 生成 {decoded_count} 张图片")

            return {
                'index': i,
                'success': True,
                'images': base64_images,
                'tensor': decoded_tensor,
                'image_count': decoded_count,
                'text': text_content,
                'seed': current_seed
            }
        except comfy.model_management.InterruptProcessingException:
            logger.warning(f"批次 {i+1} 已取消")
            raise
        except Exception as e:
            error_msg = str(e)[:200]
            logger.error(f"批次 {i+1} 失败")
            logger.error(f"错误: {error_msg}")
            return {
                'index': i,
                'success': False,
                'error': error_msg,
                'seed': current_seed,
                'tensor': None,
                'image_count': 0
            }

    def generate_images(self, prompt, api_base_url, api_key="", model_type="gemini-2.5-flash-image",
                       batch_size=1, aspect_ratio="Auto", seed=-1, top_p=0.95, max_workers=None,
                       image_1=None, image_2=None, image_3=None,
                       image_4=None, image_5=None):

        # 解析 API Key：优先使用节点输入，留空时回退 config
        sanitized_input_key = self._sanitize_api_key(api_key)
        resolved_api_key = sanitized_input_key or self._sanitize_api_key(self.load_config())

        # 验证API key
        if not resolved_api_key:
            error_msg = "请在 config.ini 中配置 API Key 或在节点中填写"
            logger.error(error_msg)
            error_tensor = self.build_error_tensor_from_text(
                "配置缺失",
                f"{error_msg}\n请在 config.ini 或节点输入中填写有效 API Key"
            )
            return (error_tensor, error_msg)

        cost_factor = self.load_cost_factor_from_config()
        balance_summary = self.get_cached_balance_text(api_base_url, resolved_api_key, cost_factor)

        start_time = time.time()
        raw_input_images = [image_1, image_2, image_3, image_4, image_5]
        input_tensors = [img for img in raw_input_images if img is not None]
        encoded_input_images = self.prepare_input_images(input_tensors)

        # 固定配置
        concurrent_mode = True  # 总是开启并发
        stagger_delay = 0.0     # 不使用交错延迟
        # 拆分网络超时：连接(10s) + 读取(90s)
        # 连接超时缩短到10s，如果10s都连不上说明网络有问题
        # 读取超时延长到90s，因为图像生成确实需要时间
        connect_timeout = 10
        read_timeout = 90
        request_timeout = (connect_timeout, read_timeout)
        continue_on_error = True  # 总是容错
        configured_workers = self.load_max_workers_from_config()
        decode_workers = max(1, configured_workers)

        if seed == -1:
            base_seed = random.randint(0, 102400)
        else:
            base_seed = seed

        decoded_tensors: List[torch.Tensor] = []
        total_generated_images = 0
        all_texts: List[str] = []
        results: List[Dict[str, Any]] = []
        tasks: List[Tuple[Any, ...]] = []

        for i in range(batch_size):
            current_seed = base_seed + i if seed != -1 else -1
            tasks.append((i, current_seed, resolved_api_key, prompt, model_type, aspect_ratio,
                          top_p, encoded_input_images, api_base_url, request_timeout, stagger_delay,
                          decode_workers))

        # 显示任务开始信息
        logger.header("🎨 Gemini 图像生成任务")
        logger.info(f"批次数量: {batch_size} 张")
        logger.info(f"图片比例: {aspect_ratio}")
        if seed != -1:
            logger.info(f"随机种子: {seed}")
        if top_p != 0.95:
            logger.info(f"Top-P 参数: {top_p}")
        logger.separator()

        # 创建 ComfyUI 进度条 - 会同时在 Web UI 和控制台显示
        pbar = comfy.utils.ProgressBar(batch_size)
        self._ensure_not_interrupted()

        used_concurrency = concurrent_mode and batch_size > 1
        completed = 0

        if used_concurrency:
            # 对网络并发做更保守限流，降低远端抖动时的连锁阻塞概率
            network_workers_cap = min(configured_workers, 4)
            actual_workers = min(network_workers_cap, batch_size)
            # 手动管理线程池，避免在超时场景下因 wait=True 阻塞退出
            executor = ThreadPoolExecutor(max_workers=actual_workers)
            try:
                future_to_index = {executor.submit(self.generate_single_image, task): task[0]
                                   for task in tasks}
                overall_timeout = connect_timeout + read_timeout + 20
                deadline = time.time() + overall_timeout
                pending_futures = set(future_to_index.keys())
                timed_out = False

                while pending_futures:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        timed_out = True
                        break

                    done, pending_futures = wait(
                        pending_futures,
                        timeout=max(0.1, remaining),
                        return_when=FIRST_COMPLETED
                    )

                    if not done:
                        continue

                    for future in done:
                        index = future_to_index.pop(future, -1)
                        try:
                            self._ensure_not_interrupted()
                            result = future.result()
                            results.append(result)
                            completed += 1

                            if result['success']:
                                logger.success(f"[{completed}/{batch_size}] 批次 {result['index']+1} 完成")
                            else:
                                logger.error(f"[{completed}/{batch_size}] 批次 {result['index']+1} 失败")

                            preview_tensor = result.get('tensor')
                            if result.get('success') and preview_tensor is not None:
                                preview_tuple = self._build_preview_tuple(preview_tensor, result['index'])
                                if preview_tuple is not None:
                                    pbar.update_absolute(completed, batch_size, preview_tuple)
                                else:
                                    pbar.update(1)
                            else:
                                pbar.update(1)
                        except comfy.model_management.InterruptProcessingException:
                            logger.warning("检测到中断信号，正在取消剩余任务...")
                            for pending in pending_futures:
                                pending.cancel()
                            for future_ref in future_to_index.keys():
                                future_ref.cancel()
                            raise
                        except Exception as e:
                            logger.error(f"批次 {index+1 if index>=0 else '?'} 异常: {str(e)}")
                            results.append({
                                'index': index,
                                'success': False,
                                'error': str(e),
                                'tensor': None,
                                'image_count': 0
                            })

                if timed_out and pending_futures:
                    logger.warning(f"整体超时！已完成 {completed}/{batch_size} 个任务")
                    for future in pending_futures:
                        future.cancel()
            except Exception:
                raise
            finally:
                # 关键：不等待线程结束以免卡住主线程；运行中的请求会在后台自行结束
                executor.shutdown(wait=False, cancel_futures=True)
        else:
            for task in tasks:
                self._ensure_not_interrupted()
                result = self.generate_single_image(task)
                results.append(result)
                # 显示批次完成
                if result['success']:
                    logger.success(f"[{task[0]+1}/{batch_size}] 批次 {task[0]+1} 完成")
                else:
                    logger.error(f"[{task[0]+1}/{batch_size}] 批次 {task[0]+1} 失败")

                # 更新 ComfyUI 进度条（实时预览）
                preview_tensor = result.get('tensor')
                if result.get('success') and preview_tensor is not None:
                    preview_tuple = self._build_preview_tuple(preview_tensor, task[0])
                    if preview_tuple is not None:
                        pbar.update_absolute(task[0]+1, batch_size, preview_tuple)
                    else:
                        pbar.update(1)
                else:
                    pbar.update(1)
                if not result['success'] and not continue_on_error:
                    logger.warning("遇到错误且未开启容错，停止处理")
                    break

        if not results:
            error_text = f"未生成任何图像\n总耗时: {time.time() - start_time:.2f}s"
            if balance_summary:
                error_text = f"{balance_summary}\n\n{error_text}"
            logger.error(error_text)
            error_tensor = self.build_error_tensor_from_text("生成失败", error_text)
            return (error_tensor, error_text)

        results.sort(key=lambda x: x['index'])

        for result in results:
            if result.get('success'):
                tensor = result.get('tensor')
                if tensor is not None:
                    decoded_tensors.append(tensor)
                    total_generated_images += result.get('image_count', tensor.shape[0])
                if result.get('text'):
                    all_texts.append(f"[批次 {result['index']+1}] {result['text']}")
            else:
                error_msg = f"[批次 {result['index']+1}] ❌ {result.get('error', '未知错误')}"
                all_texts.append(error_msg)
                if not continue_on_error:
                    break

        total_time = time.time() - start_time

        if not decoded_tensors or total_generated_images == 0:
            error_text = f"未生成任何图像\n总耗时: {total_time:.2f}s\n\n" + "\n".join(all_texts)
            if balance_summary:
                error_text = f"{balance_summary}\n\n{error_text}"
            logger.error(error_text)
            error_tensor = self.build_error_tensor_from_text("生成失败", error_text)
            return (error_tensor, error_text)

        if len(decoded_tensors) == 1:
            image_tensor = decoded_tensors[0]
        else:
            image_tensor = torch.cat(decoded_tensors, dim=0)

        actual_count = total_generated_images
        ratio_text = "自动" if aspect_ratio == "Auto" else aspect_ratio
        success_info = f"✅ 成功生成 {actual_count} 张图像（比例: {ratio_text}）"
        avg_time = total_time / actual_count if actual_count > 0 else 0
        time_info = f"总耗时: {total_time:.2f}s，平均 {avg_time:.2f}s/张"
        if actual_count != batch_size:
            time_info += f" ⚠️ 请求{batch_size}张，实际生成{actual_count}张"

        combined_text = f"{success_info}\n{time_info}"
        if all_texts:
            combined_text += "\n\n" + "\n".join(all_texts)
        if balance_summary:
            combined_text = f"{balance_summary}\n\n{combined_text}"

        # 显示完成统计
        logger.summary("任务完成", {
            "总批次": f"{batch_size} 个",
            "成功生成": f"{actual_count} 张",
            "总耗时": f"{total_time:.2f}s",
            "平均速度": f"{avg_time:.2f}s/张"
        })

        return (image_tensor, combined_text)

# 注册节点
NODE_CLASS_MAPPINGS = {"BananaImageNode": BananaImageNode}
NODE_DISPLAY_NAME_MAPPINGS = {"BananaImageNode": "心宝❤Banana"}

BananaImageNode.ensure_balance_route()
