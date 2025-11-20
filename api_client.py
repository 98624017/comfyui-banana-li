from __future__ import annotations

import json
import os
import sys
import threading
import time
import warnings
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
import urllib3
from urllib3.exceptions import InsecureRequestWarning

try:
    from requests.packages import urllib3 as requests_urllib3  # type: ignore
except Exception:  # pragma: no cover - 兼容精简 Python 发行版
    requests_urllib3 = None

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from logger import logger  # type: ignore


class GeminiApiClient:
    """封装与 Gemini 兼容图像接口交互的 HTTP 客户端。"""

    _DEFAULT_CONNECT_TIMEOUT = 15.0
    _DEFAULT_READ_TIMEOUT = 90.0
    _MAX_RETRIES = 2
    _BASE_BACKOFF = 2.0
    _RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
    _ASPECT_RATIO_ALIASES: Dict[str, str] = {
        "1:1": "1:1",
        "2:3": "2:3",
        "3:2": "3:2",
        "3:4": "3:4",
        "4:3": "4:3",
        "4:5": "4:5",
        "5:4": "5:4",
        "9:16": "9:16",
        "16:9": "16:9",
        "21:9": "21:9",
    }
    _INSECURE_WARNING_SUPPRESSED = False

    def __init__(self, config_manager, logger_instance=logger, interrupt_checker=None) -> None:
        self.config_manager = config_manager
        self.logger = logger_instance
        self.interrupt_checker = interrupt_checker
        self._thread_local = threading.local()

    # --- request 构造逻辑 -------------------------------------------------
    def _normalize_aspect_ratio(self, aspect_ratio: Optional[str]) -> Optional[str]:
        if not aspect_ratio or aspect_ratio.lower() == "auto":
            return None
        normalized = aspect_ratio.strip()
        return self._ASPECT_RATIO_ALIASES.get(normalized, normalized)

    def _normalize_model_id(self, model_type: Optional[str]) -> str:
        model = (model_type or "").strip()
        if "/models/" in model:
            model = model.split("/models/", 1)[1]
        for prefix in ("models/", "v1beta/"):
            if model.startswith(prefix):
                model = model.split("/", 1)[1]
        return model

    def create_request_data(
        self,
        prompt: str,
        seed: int,
        aspect_ratio: str,
        top_p: float,
        input_images_b64: Optional[List[str]] = None,
        model_type: Optional[str] = None,
        image_size: Optional[str] = None,
    ) -> Dict[str, Any]:
        prompt_text = (prompt or "").strip()
        if not prompt_text and not input_images_b64:
            raise ValueError("请输入提示词或提供至少一张参考图像")

        parts: List[Dict[str, Any]] = []
        if prompt_text:
            parts.append({"text": prompt_text})

        for encoded in input_images_b64 or []:
            if not encoded:
                continue
            parts.append({
                "inlineData": {
                    "mimeType": "image/png",
                    "data": encoded,
                }
            })

        content = {"role": "user", "parts": parts}
        generation_config: Dict[str, Any] = {
            "topP": float(top_p),
            "maxOutputTokens": 8192,
            "responseModalities": ["IMAGE"],
        }
        if isinstance(seed, int) and seed >= 0:
            generation_config["seed"] = seed

        image_config: Dict[str, Any] = {}
        aspect = self._normalize_aspect_ratio(aspect_ratio)
        if aspect:
            image_config["aspectRatio"] = aspect

        normalized_model = self._normalize_model_id(model_type)
        if normalized_model.startswith("gemini-3-pro-image-preview") or normalized_model.startswith("gemini-3-pro-image"):
            normalized_size = (image_size or "2K").strip().upper()
            valid_sizes = {"1K", "2K", "4K"}
            if normalized_size not in valid_sizes:
                normalized_size = "2K"
            image_config["image_size"] = normalized_size

        if image_config:
            generation_config["imageConfig"] = image_config

        request_body: Dict[str, Any] = {
            "contents": [content],
            "generationConfig": generation_config,
        }

        return request_body

    # --- HTTP 发送逻辑 ----------------------------------------------------
    def _get_session(self, bypass_proxy: bool = False) -> requests.Session:
        attr_name = "session_no_proxy" if bypass_proxy else "session"
        session = getattr(self._thread_local, attr_name, None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            if bypass_proxy:
                session.trust_env = False
                session.proxies = {}
            setattr(self._thread_local, attr_name, session)
        return session

    def _ensure_not_interrupted(self) -> None:
        if self.interrupt_checker is not None:
            self.interrupt_checker()

    def _interruptible_post(
        self,
        session: requests.Session,
        url: str,
        payload: bytes,
        headers: Dict[str, str],
        timeout: Tuple[float, float],
        verify: bool,
        bypass_proxy: bool,
    ) -> requests.Response:
        """
        让网络请求在长耗时阶段也能响应 ComfyUI 的中断。
        使用后台线程发起请求，主线程轮询中断标志，必要时关闭 session 终止阻塞。
        """
        if self.interrupt_checker is None:
            return session.post(
                url,
                data=payload,
                headers=headers,
                timeout=timeout,
                verify=verify,
            )

        done_event = threading.Event()
        resp_holder: Dict[str, Any] = {}
        exc_holder: Dict[str, BaseException] = {}

        def _do_request() -> None:
            try:
                resp_holder["resp"] = session.post(
                    url,
                    data=payload,
                    headers=headers,
                    timeout=timeout,
                    verify=verify,
                )
            except BaseException as exc:  # pragma: no cover - 直接回传给主线程
                exc_holder["exc"] = exc
            finally:
                done_event.set()

        thread = threading.Thread(target=_do_request, daemon=True)
        thread.start()

        poll_interval = 0.25
        attr_name = "session_no_proxy" if bypass_proxy else "session"
        try:
            while not done_event.wait(timeout=poll_interval):
                self._ensure_not_interrupted()
            self._ensure_not_interrupted()
        except BaseException:
            # 强制关闭当前 session，尽快打断正在阻塞的 request
            try:
                session.close()
            finally:
                setattr(self._thread_local, attr_name, None)
            raise

        if "exc" in exc_holder:
            raise exc_holder["exc"]

        resp = resp_holder.get("resp")
        if resp is None:
            # 极端情况下 session.post 未返回但线程结束，视为连接失败
            raise RuntimeError("请求被中断或未获得响应")
        return resp

    @classmethod
    def _suppress_insecure_warning(cls, verify_ssl: bool) -> None:
        if verify_ssl or cls._INSECURE_WARNING_SUPPRESSED:
            return
        # urllib3 的 InsecureRequestWarning 会在关闭 SSL 验证时提示真实域名。
        # 当用户显式关闭验证时,统一在客户端级别关闭该告警,避免源站泄露。
        warnings.filterwarnings("ignore", category=InsecureRequestWarning)
        urllib3.disable_warnings(InsecureRequestWarning)
        if requests_urllib3 is not None:
            try:
                requests_urllib3.disable_warnings(InsecureRequestWarning)
            except Exception:
                pass
        cls._INSECURE_WARNING_SUPPRESSED = True

    def _build_headers(self, api_key: str) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-API-Key": api_key,
            "X-Banana-Client": "comfyui-banana-li",
        }

    def _resolve_timeout(self, timeout: Optional[Any]) -> Tuple[float, float]:
        if isinstance(timeout, (tuple, list)) and len(timeout) == 2:
            connect = float(timeout[0]) if timeout[0] else self._DEFAULT_CONNECT_TIMEOUT
            read = float(timeout[1]) if timeout[1] else self._DEFAULT_READ_TIMEOUT
        elif isinstance(timeout, (int, float)) and timeout > 0:
            connect = read = float(timeout)
        else:
            connect = self._DEFAULT_CONNECT_TIMEOUT
            read = self._DEFAULT_READ_TIMEOUT
        return (max(1.0, connect), max(5.0, read))

    def _build_generate_content_url(self, base_url: str, model_type: str) -> str:
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("未配置有效的 API Base URL")
        model = (model_type or "").strip()
        if not model:
            raise ValueError("未指定模型类型")

        if model.startswith("models/"):
            model = model.split("/", 1)[1]
        if model.startswith("v1beta/"):
            model = model.split("/", 1)[1]

        if base.endswith(":generateContent"):
            return base
        if ":generate" in base:
            return base
        if base.endswith(f"/{model}:generateContent"):
            return base
        if base.endswith(f"/{model}"):
            return f"{base}:generateContent"
        if "/models/" in base:
            return f"{base.rstrip('/')}:generateContent"
        return f"{base}/v1beta/models/{model}:generateContent"

    def send_request(
        self,
        api_key: str,
        request_data: Dict[str, Any],
        model_type: str,
        api_base_url: str,
        timeout: Optional[Any] = None,
        bypass_proxy: bool = False,
        verify_ssl: bool = True,
        max_retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        sanitized_key = self.config_manager.sanitize_api_key(api_key)
        if not sanitized_key:
            raise ValueError("请填写有效的 API Key")

        url = self._build_generate_content_url(api_base_url, model_type)
        session = self._get_session(bypass_proxy)
        self._suppress_insecure_warning(verify_ssl)
        connect_timeout, read_timeout_global = self._resolve_timeout(timeout)
        headers = self._build_headers(sanitized_key)

        # 注意：requests 在 data 为 str 时会将其直接传递给 http.client，
        # 后者默认使用 latin-1 编码字符串，这会在请求体包含中文等非 latin-1 字符时
        # 触发 "Body (...) is not valid Latin-1" 错误。
        # 这里显式将 JSON 序列化结果编码为 UTF-8 bytes，避免依赖 http.client 的默认编码。
        payload = json.dumps(request_data, ensure_ascii=False).encode("utf-8")
        last_error: Optional[BaseException] = None
        last_error_phase: Optional[str] = None  # connect/read
        last_error_hint: Optional[str] = None

        effective_max_retries = (
            max_retries
            if isinstance(max_retries, int) and max_retries >= 1
            else self._MAX_RETRIES
        )

        # 采用“全局读取超时 + 每次连接 15s”语义：
        # - connect_timeout：单次连接阶段的超时时间（例如 15s），每次尝试独立计算
        # - read_timeout_global：从第一次尝试开始计时的全局读取超时（例如 90s 或 70s）
        #   后续重试只使用剩余的读取时间，确保总耗时不会超过全局读取超时
        global_start = time.time()
        attempt_delay = self._BASE_BACKOFF  # 初始重试间隔（秒）

        for attempt in range(1, effective_max_retries + 1):
            self._ensure_not_interrupted()
            # 计算本次尝试可用的剩余读取时间
            elapsed = time.time() - global_start
            remaining_read = read_timeout_global - elapsed
            if remaining_read <= 0:
                # 全局读取超时已耗尽，不再发起新的请求
                raise RuntimeError(
                    f"模型 {model_type} 响应超时：总耗时 {elapsed:.1f}s 已超过读取上限 {read_timeout_global:.1f}s"
                )

            start = time.time()
            try:
                response = self._interruptible_post(
                    session,
                    url,
                    payload,
                    headers,
                    (connect_timeout, remaining_read),
                    verify_ssl,
                    bypass_proxy,
                )
                if (
                    response.status_code in self._RETRYABLE_STATUS
                    and attempt < effective_max_retries
                ):
                    raise requests.HTTPError(
                        f"HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                return response.json()
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                duration = time.time() - start
                # 连接阶段失败：可以安全重试，不会产生计费
                if isinstance(exc, requests.ConnectTimeout) or isinstance(
                    exc, requests.ConnectionError
                ):
                    last_error_phase = "connect"
                    hint = (
                        "连接阶段耗时过长或无法建立，请检查代理、DNS 或 Base URL 域名是否可达"
                    )
                    self.logger.warning(
                        f"连接阶段失败：{model_type}（耗时 {duration:.1f}s，尝试 {attempt}/{effective_max_retries}）"
                    )
                    last_error_hint = hint
                else:
                    # 读取阶段超时：请求已送达且可能已计费，避免自动重试导致双计费
                    last_error_phase = "read"
                    hint = (
                        f"服务器在 {duration:.1f}s 内未返回数据，可能已开始生成；为避免重复计费，已停止自动重试"
                    )
                    last_error_hint = hint
                    raise RuntimeError(
                        f"模型 {model_type} 响应超时（可能已计费）：{hint}"
                    )
            except requests.HTTPError as exc:
                last_error = exc
                status = exc.response.status_code if exc.response else None
                # 避免泄露源站域名：仅使用服务器响应内容，不使用异常字符串
                body = exc.response.text if exc.response is not None else "无响应内容"
                truncated = body[:300]
                if status in self._RETRYABLE_STATUS and attempt < effective_max_retries:
                    self.logger.warning(
                        f"HTTP {status}，将重试：{truncated}"
                    )
                else:
                    raise RuntimeError(
                        f"远端返回异常（HTTP {status}）：{truncated}"
                    )
            except requests.RequestException as exc:
                last_error = exc
                error_type = type(exc).__name__
                raise RuntimeError(
                    f"HTTP 请求失败（{error_type}），请检查网络连接、代理或证书配置"
                )

            if attempt < effective_max_retries:
                time.sleep(attempt_delay)
                attempt_delay *= 1.5

        # 对最终用户仅暴露抽象错误类型，避免泄露真实源站地址或 URL 细节
        error_label = type(last_error).__name__ if last_error is not None else "未知错误"
        if last_error_phase == "connect":
            hint = last_error_hint or "请检查网络、代理或 API Base URL 配置"
            raise RuntimeError(
                f"连接 {model_type} 失败（{error_label}）：{hint}"
            )
        if last_error_phase == "read":
            hint = last_error_hint or "模型响应过慢，超过读取时间上限"
            raise RuntimeError(
                f"模型 {model_type} 响应超时（{error_label}）：{hint}"
            )
        raise RuntimeError(
            f"连续 {effective_max_retries} 次请求失败（错误类型：{error_label}），"
            f"请检查网络环境或服务状态"
        )

    # --- 响应解析 --------------------------------------------------------
    def extract_content(self, response_data: Dict[str, Any]) -> Tuple[List[str], str]:
        if not isinstance(response_data, dict):
            raise ValueError("接口返回数据格式异常")

        images: List[str] = []
        texts: List[str] = []
        candidates = response_data.get("candidates") or []

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            parts = content.get("parts") or []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                inline = part.get("inlineData")
                if inline and isinstance(inline, dict):
                    data = inline.get("data")
                    mime = inline.get("mimeType", "")
                    if data and isinstance(data, str) and mime.startswith("image/"):
                        images.append(data)
                        continue
                text_value = part.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    texts.append(text_value.strip())

        combined_text = "\n".join(texts).strip()
        return images, combined_text

    # --- 余额查询 --------------------------------------------------------
    def _build_balance_urls(self, base_url: str) -> List[str]:
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("未配置 Balance API 地址")
        # 仅使用 new-api 文档中的标准用量查询端点
        return [f"{base}/api/usage/token"]

    def fetch_token_usage(
        self,
        api_base_url: str,
        api_key: str,
        timeout: int = 15,
        bypass_proxy: bool = False,
        verify_ssl: bool = True,
    ) -> Dict[str, Any]:
        sanitized_key = self.config_manager.sanitize_api_key(api_key)
        if not sanitized_key:
            raise ValueError("请提供有效的 API Key 后再查询余额")

        session = self._get_session(bypass_proxy)
        self._suppress_insecure_warning(verify_ssl)
        timeout_tuple = self._resolve_timeout(timeout)
        # 内部错误详情仅写入日志，不直接暴露真实源站给前端用户
        internal_errors: List[str] = []

        for url in self._build_balance_urls(api_base_url):
            try:
                response = session.get(
                    url,
                    headers=self._build_headers(sanitized_key),
                    timeout=timeout_tuple,
                    verify=verify_ssl,
                )
                if response.status_code == 404:
                    internal_errors.append("404 未找到余额查询端点")
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("余额接口返回格式错误")
                return payload
            except ValueError as exc:
                internal_errors.append(f"数据格式错误: {exc}")
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response else None
                internal_errors.append(f"HTTP {status} 错误")
            except requests.RequestException as exc:
                error_type = type(exc).__name__
                internal_errors.append(f"网络错误 ({error_type})")

        if internal_errors:
            # 记录错误摘要，不包含敏感的源站地址信息
            self.logger.warning(
                "余额查询失败，错误摘要: " + "; ".join(internal_errors)
            )

        # 对前端只返回抽象错误，避免暴露真实源站地址
        raise RuntimeError("余额查询失败，请检查 API 服务与网络状态，或联系服务提供者")


__all__ = ["GeminiApiClient"]
