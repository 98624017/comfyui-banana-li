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

# 在非 ComfyUI 运行环境中,server 可能无法正常导入
# 这里做一个兼容处理:导入失败时提供一个占位 PromptServer,
# 仅用于避免测试脚本导入本模块时报错
try:
    from server import PromptServer
except ImportError:
    class _DummyPromptServer:
        instance = None
    PromptServer = _DummyPromptServer()

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
                       fast_fail_threshold=20.0):
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
        # 默认重试可恢复的错误（5xx、网络超时、连接中断、常见IO错误）
        retriable_exceptions = (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.HTTPError,
            requests.exceptions.ChunkedEncodingError,  # 响应分块传输中断
            requests.exceptions.RequestException,       # 兜底的网络异常
            ConnectionResetError,                       # 连接被重置
            BrokenPipeError,                            # 管道破裂
            TimeoutError,                               # Python 内置超时错误（例如写操作超时）
            OSError,                                    # 其他底层网络/IO错误
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
                    # 为日志输出构建一个脱敏后的错误信息,避免泄露源站/密钥等敏感URL
                    # 仅用于日志展示,不影响后续基于原始 error_str 的判定逻辑
                    sanitized_error_str = re.sub(
                        r"https?://[^\s'\"）)]+",
                        "[URL]",
                        error_str
                    )

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
                            logger.error(f"不可重试的错误: {sanitized_error_str[:200]}")
                        raise

                    # 打印重试信息
                    logger.warning(
                        f"{error_type} (尝试 {attempt + 1}/{mtries}, 耗时 {attempt_duration:.1f}s): "
                        f"{sanitized_error_str[:200]}"
                    )
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

    # API Base URL 编码相关常量（默认绑定到 https://api.aabao.top）
    # 为避免在代码中出现明文 URL，仅保存字符编码列表
    _ENC_KEY_PARTS = (3, 4)
    _DEFAULT_API_BASE_URL_CODEPOINTS = [104, 116, 116, 112, 115, 58, 47, 47, 97, 112, 105, 46, 97, 97, 98, 97, 111, 46, 116, 111, 112]
    _CONFIG_SECTION = "gemini"
    _CONFIG_KEY_API_BASE_URL_ENC = "api_base_url_enc"

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
    # 本地测试配置相关常量
    _TEST_CONFIG_FILE_NAME = "banana_gemini_test.local.ini"
    _TEST_CONFIG_SECTION = "gemini_test"
    _TEST_MODE_ENV_VAR = "BANANA_GEMINI_USE_LOCAL_TEST"

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "text")
    FUNCTION = "generate_images"
    OUTPUT_NODE = True
    CATEGORY = "image/ai_generation"

    @classmethod
    def _decode_api_base_url(cls, enc: str) -> str:
        """将编码后的 Base URL 还原为明文，仅在运行时使用"""
        raw = base64.b64decode(enc.encode("utf-8"))
        key = 0
        for part in cls._ENC_KEY_PARTS:
            key ^= part
        data = bytes((b ^ key) for b in raw)
        return data.decode("utf-8")

    @classmethod
    def _get_default_base_url(cls) -> str:
        """
        通过字符编码列表构造默认 Base URL，避免在代码中出现明文 URL
        """
        return "".join(chr(c) for c in cls._DEFAULT_API_BASE_URL_CODEPOINTS)

    @classmethod
    def _get_effective_api_base_url(cls) -> str:
        """
        统一计算当前生效的 API Base URL。

        优先级：
        1. 若开启测试模式且本地测试配置中存在 api_base_url_enc，则使用该值
        2. 若 config.ini 的 [gemini] 段中配置了 api_base_url_enc，则使用该值
        3. 否则回退到类内置的默认值
        """
        # 1. 测试模式优先（用于临时开发/调试）
        test_base_url = cls._load_test_base_url()
        if test_base_url:
            return test_base_url

        # 2. 正常配置文件中的永久 Base URL（编码形式）
        config_path = cls._get_config_path()
        parser = configparser.ConfigParser()
        if os.path.exists(config_path):
            try:
                parser.read(config_path, encoding="utf-8")
                if parser.has_section(cls._CONFIG_SECTION):
                    enc = parser.get(
                        cls._CONFIG_SECTION,
                        cls._CONFIG_KEY_API_BASE_URL_ENC,
                        fallback=""
                    ).strip()
                    if enc:
                        return cls._decode_api_base_url(enc)
            except Exception as e:
                logger.warning(
                    f"读取 config 中的 {cls._CONFIG_KEY_API_BASE_URL_ENC} 失败: {e}"
                )

        # 3. 默认值
        return cls._get_default_base_url()

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
        base_url = api_base_url or cls._get_effective_api_base_url()
        normalized_url = base_url.rstrip("/").lower()
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
            # 前端不再控制 Base URL，统一由后端隐藏管理
            base_url = cls._get_effective_api_base_url()
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

        # 使用短连接策略: 每个请求结束后主动关闭连接,避免在代理/不稳定网络下长连接悬挂
        # 注意: requests 仍会管理底层连接池,但通过 Connection: close 提示中间节点不要长期保持连接
        session.headers.update({
            'Connection': 'close',
        })

        setattr(cls._THREAD_LOCAL, "session", session)

        with cls._SESSION_INIT_LOCK:
            if not cls._SESSION_INITIALIZED:
                logger.info(f"HTTP 连接池已初始化: pool_size={pool_size}, connection=close")
                cls._SESSION_INITIALIZED = True

        return session

    @classmethod
    def fetch_token_usage(cls, api_base_url: str, api_key: str, timeout: int = 15) -> Dict[str, Any]:
        sanitized_key = cls._sanitize_api_key(api_key)
        if not sanitized_key:
            raise ValueError("未配置有效的 API Key")
        base_url = (api_base_url or cls._get_effective_api_base_url()).rstrip("/")
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
        cls._store_balance_snapshot(base_url, sanitized_key, payload)
        return payload

    @staticmethod
    def _get_config_path() -> str:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(current_dir, "config.ini")

    @classmethod
    def _get_test_config_path(cls) -> str:
        """获取本地测试配置文件路径（banana_gemini_test.local.ini）"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(current_dir, cls._TEST_CONFIG_FILE_NAME)

    @classmethod
    def _is_test_mode_enabled(cls) -> bool:
        """
        判断是否开启本地测试模式

        通过环境变量 BANANA_GEMINI_USE_LOCAL_TEST 控制：
        - 1/true/yes/on（大小写不敏感）视为开启
        """
        value = os.environ.get(cls._TEST_MODE_ENV_VAR, "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    @classmethod
    def _load_test_section(cls) -> Optional[Dict[str, str]]:
        """
        从本地测试配置文件中读取 [gemini_test] 段

        仅在测试模式开启时尝试读取；读取失败会记录日志但不中断正常流程。
        """
        if not cls._is_test_mode_enabled():
            return None

        test_config_path = cls._get_test_config_path()
        if not os.path.exists(test_config_path):
            return None

        parser = configparser.ConfigParser()
        try:
            parser.read(test_config_path, encoding="utf-8")
            if parser.has_section(cls._TEST_CONFIG_SECTION):
                section = parser[cls._TEST_CONFIG_SECTION]
                # 转成普通字典，避免把 ConfigParser 的细节泄露到外层
                return {k: v for k, v in section.items()}
        except Exception as e:
            logger.warning(f"读取本地测试配置失败: {e}")
        return None

    @classmethod
    def _load_test_api_key(cls) -> Optional[str]:
        """从本地测试配置中读取并清洗 API Key"""
        section = cls._load_test_section()
        if not section:
            return None
        api_key = section.get("api_key", "").strip()
        return cls._sanitize_api_key(api_key)

    @classmethod
    def _load_test_base_url(cls) -> Optional[str]:
        """从本地测试配置中读取并解码 Base URL（编码字段 api_base_url_enc）"""
        section = cls._load_test_section()
        if not section:
            return None
        enc = section.get("api_base_url_enc", "").strip()
        if not enc:
            return None
        try:
            return cls._decode_api_base_url(enc)
        except Exception as e:
            logger.warning(f"解码测试配置中的 api_base_url_enc 失败: {e}")
            return None

    @classmethod
    def _load_test_python_env(cls) -> Optional[str]:
        """
        从本地测试配置中读取 ComfyUI Python 环境路径

        目前仅作为调试/外部脚本调用时的参考，不在节点运行逻辑中自动使用。
        """
        section = cls._load_test_section()
        if not section:
            return None
        python_env = section.get("python_env", "").strip()
        return python_env or None

    @classmethod
    def load_network_workers_cap_from_config(cls) -> int:
        """
        从 config.ini 读取网络并发上限

        配置项:
        [gemini]
        network_workers_cap = 4

        仅用于限制同时发起的网络请求数量,避免在不稳定服务商上产生请求风暴。
        最终并发度会在 [1, 8] 范围内被夹紧。
        """
        config_path = cls._get_config_path()
        parser = configparser.ConfigParser()
        default_cap = 4

        if os.path.exists(config_path):
            try:
                parser.read(config_path, encoding="utf-8")
                if parser.has_section("gemini"):
                    value = parser.getint("gemini", "network_workers_cap", fallback=default_cap)
                    # 防止配置异常,对并发上限做合理约束
                    return max(1, min(value, 8))
            except Exception as e:
                logger.warning(f"读取 config 中的 network_workers_cap 失败: {e}")

        return default_cap

    @classmethod
    def load_config(cls):
        """从config.ini加载API key"""
        config_path = cls._get_config_path()

        config = configparser.ConfigParser()

        # 默认API key
        default_api_key = "your-api-key-here"

        # 若开启测试模式，优先从本地测试配置读取 API Key
        test_api_key = cls._load_test_api_key()
        if test_api_key:
            return test_api_key

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

    @retry_with_backoff(tries=2, delay=2, backoff=1, fast_fail_threshold=20.0)

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
        # 每次请求使用独立 Session,与初始版本行为保持一致
        session = requests.Session()
        session.headers.update(headers)
        try:
            response = session.post(url, json=request_data, timeout=timeout)
            request_time = time.time() - request_start
            try:
                if response.status_code != 200:
                    # 记录非200响应的耗时，帮助诊断
                    error_msg = f"API返回 {response.status_code}: {response.text[:200]}"
                    logger.warning(f"请求耗时 {request_time:.2f}s 后收到错误: {error_msg}")
                    # 这里仍然抛出通用异常,由重试装饰器根据错误信息决定是否重试
                    raise Exception(error_msg)
                logger.info(f"API请求成功，耗时 {request_time:.2f}s")
                return response.json()
            finally:
                # 确保响应内容被完全读取
                response.close()
        except requests.exceptions.Timeout as e:
            timeout_duration = time.time() - request_start
            # 保留 Timeout 异常类型,让重试装饰器能够识别为可重试错误
            msg = f"请求超时（设置{timeout}秒，实际等待{timeout_duration:.1f}秒）"
            logger.warning(msg)
            raise requests.exceptions.Timeout(msg) from e
        except requests.exceptions.RequestException as e:
            # 对于连接中断、写入超时、SSL EOF 等网络异常,
            # 保留原始 RequestException 类型,交由重试装饰器统一处理
            logger.warning(f"网络错误: {str(e)}")
            raise
        finally:
            # 独立 Session 使用完后立即关闭,避免在代理/不稳定网络下复用潜在坏连接
            session.close()

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
            effective_base_url = type(self)._get_effective_api_base_url()
            response_data = self.send_request(api_key, request_data, model_type, effective_base_url, timeout)
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

            # 更明显地区分“有图返回”和“未返回任何图片”的情况
            if decoded_count > 0:
                logger.success(f"批次 {i+1} 完成 - 生成 {decoded_count} 张图片")
            else:
                # 简化日志输出,尽可能给出用户能理解的原因说明
                reason = ""
                # 1. 检查 finishReason 信息
                try:
                    if isinstance(response_data, dict):
                        candidates = response_data.get("candidates") or []
                        if candidates and isinstance(candidates[0], dict):
                            finish_reason = candidates[0].get("finishReason") or ""
                            if finish_reason:
                                if finish_reason == "NO_IMAGE":
                                    reason = "模型未生成任何图片（finishReason=NO_IMAGE，一般表示当前提示或参考图不触发图像输出，可能是内容被过滤或未通过安全审查）"
                                else:
                                    reason = f"模型未生成图片（finishReason={finish_reason}）"
                except Exception:
                    # 如果解析 finishReason 失败,忽略即可
                    pass

                # 2. 如果有文本内容,补充展示一小段
                brief_text = (text_content or "").strip().replace("\n", " ")
                if brief_text:
                    if reason:
                        reason = f"{reason}；模型返回文本: {brief_text[:100]}"
                    else:
                        reason = f"模型仅返回文本: {brief_text[:100]}"

                # 3. 都没有就给一个通用说明
                if not reason:
                    reason = "模型未给出图片或说明文本，可能是服务端策略或参数设置导致本次未产出图片"

                logger.warning(f"批次 {i+1} 完成，但未返回任何图片。{reason}")

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

    def generate_images(self, prompt, api_key="", model_type="gemini-2.5-flash-image",
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

        # 统一使用内部隐藏的 Base URL（不接受前端传入）
        effective_base_url = type(self)._get_effective_api_base_url()

        cost_factor = self.load_cost_factor_from_config()
        balance_summary = self.get_cached_balance_text(effective_base_url, resolved_api_key, cost_factor)

        start_time = time.time()
        raw_input_images = [image_1, image_2, image_3, image_4, image_5]
        input_tensors = [img for img in raw_input_images if img is not None]
        encoded_input_images = self.prepare_input_images(input_tensors)

        # 固定配置
        concurrent_mode = True   # 总是开启并发
        # 为网络请求增加轻微交错延迟,减少瞬时请求尖峰
        stagger_delay = 0.2      # 每个批次相对前一个延迟 0.2 秒
        # 拆分网络超时：连接(20s) + 读取(90s)
        # 连接超时设置为20s，在代理/不稳定网络下更宽容
        # 读取超时保持90s，因为图像生成确实需要时间
        connect_timeout = 20
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
                          top_p, encoded_input_images, request_timeout, stagger_delay,
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
            # network_workers_cap 可通过 config.ini 配置,默认 4
            configured_network_cap = self.load_network_workers_cap_from_config()
            network_workers_cap = min(configured_workers, configured_network_cap)
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
            # 若实际生成数量少于请求数量，在日志中额外给出明显提示
            logger.warning(f"部分批次未返回图片：请求 {batch_size} 张，实际上只生成 {actual_count} 张，请查看上方各批次日志中的“未返回任何图片”提示")

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


