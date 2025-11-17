from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from logger import logger  # type: ignore


class GeminiApiClient:
    """封装与 Gemini 兼容图像接口交互的 HTTP 客户端。"""

    _DEFAULT_CONNECT_TIMEOUT = 10.0
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

    def __init__(self, config_manager, logger_instance=logger) -> None:
        self.config_manager = config_manager
        self.logger = logger_instance
        self._thread_local = threading.local()

    # --- request 构造逻辑 -------------------------------------------------
    def _normalize_aspect_ratio(self, aspect_ratio: Optional[str]) -> Optional[str]:
        if not aspect_ratio or aspect_ratio.lower() == "auto":
            return None
        normalized = aspect_ratio.strip()
        return self._ASPECT_RATIO_ALIASES.get(normalized, normalized)

    def create_request_data(
        self,
        prompt: str,
        seed: int,
        aspect_ratio: str,
        top_p: float,
        input_images_b64: Optional[List[str]] = None,
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

        # 修复：imageConfig 应该在 generationConfig 里面，而不是 imageGenerationConfig
        aspect = self._normalize_aspect_ratio(aspect_ratio)
        if aspect:
            generation_config["imageConfig"] = {"aspectRatio": aspect}

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

    def _build_headers(self, api_key: str) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-API-Key": api_key,
            "X-Banana-Client": "comfyui-banana-gemini",
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
        max_retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        sanitized_key = self.config_manager.sanitize_api_key(api_key)
        if not sanitized_key:
            raise ValueError("请填写有效的 API Key")

        url = self._build_generate_content_url(api_base_url, model_type)
        session = self._get_session(bypass_proxy)
        verify_ssl = self.config_manager.should_verify_ssl()
        connect_timeout, read_timeout_global = self._resolve_timeout(timeout)
        headers = self._build_headers(sanitized_key)

        payload = json.dumps(request_data, ensure_ascii=False)
        last_error: Optional[BaseException] = None

        effective_max_retries = (
            max_retries
            if isinstance(max_retries, int) and max_retries >= 1
            else self._MAX_RETRIES
        )

        # 采用“全局读取超时 + 每次连接 20s”语义：
        # - connect_timeout：单次连接阶段的超时时间（例如 20s），每次尝试独立计算
        # - read_timeout_global：从第一次尝试开始计时的全局读取超时（例如 90s 或 70s）
        #   后续重试只使用剩余的读取时间，确保总耗时不会超过全局读取超时
        global_start = time.time()

        for attempt in range(1, effective_max_retries + 1):
            # 计算本次尝试可用的剩余读取时间
            elapsed = time.time() - global_start
            remaining_read = read_timeout_global - elapsed
            if remaining_read <= 0:
                # 全局读取超时已耗尽，不再发起新的请求
                raise RuntimeError(
                    f"请求 {model_type} 超时：总耗时 {elapsed:.1f}s 已超过全局读取超时 {read_timeout_global:.1f}s"
                )

            start = time.time()
            try:
                response = session.post(
                    url,
                    data=payload,
                    headers=headers,
                    timeout=(connect_timeout, remaining_read),
                    verify=verify_ssl,
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
                self.logger.warning(
                    f"请求 {model_type} 超时（{duration:.1f}s），尝试 {attempt}/{effective_max_retries}"
                )
            except requests.HTTPError as exc:
                last_error = exc
                status = exc.response.status_code if exc.response else None
                body = exc.response.text if exc.response is not None else str(exc)
                truncated = body[:300]
                if status in self._RETRYABLE_STATUS and attempt < effective_max_retries:
                    self.logger.warning(
                        f"HTTP {status}，将重试：{truncated}"
                    )
                else:
                    raise RuntimeError(
                        f"远端返回异常（HTTP {status}）：{truncated}"
                    ) from exc
            except requests.RequestException as exc:
                last_error = exc
                raise RuntimeError(f"HTTP 请求失败：{exc}") from exc

            if attempt < effective_max_retries:
                time.sleep(attempt_delay)
                attempt_delay *= 1.5

        raise RuntimeError(f"连续 {effective_max_retries} 次请求失败：{last_error}")

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
    ) -> Dict[str, Any]:
        sanitized_key = self.config_manager.sanitize_api_key(api_key)
        if not sanitized_key:
            raise ValueError("请提供有效的 API Key 后再查询余额")

        session = self._get_session(bypass_proxy)
        verify_ssl = self.config_manager.should_verify_ssl()
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
                    internal_errors.append(f"{url} -> 404")
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("余额接口返回格式错误")
                return payload
            except ValueError as exc:
                internal_errors.append(f"{url}: {exc}")
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response else None
                internal_errors.append(f"{url}: HTTP {status} - {exc}")
            except requests.RequestException as exc:
                internal_errors.append(f"{url}: 网络错误 - {exc}")

        if internal_errors:
            # 在日志中保留详细信息，便于开发者排查
            self.logger.warning(
                "余额查询失败，尝试的地址与错误详情: " + "; ".join(internal_errors)
            )

        # 对前端只返回抽象错误，避免暴露真实源站地址
        raise RuntimeError("余额查询失败，请检查 API 服务与网络状态，或联系服务提供者")


__all__ = ["GeminiApiClient"]
