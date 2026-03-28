"""
Veo 视频接口封装。

接口形态采用 `/v1/videos`：create（异步）+ status 轮询。
本模块仅承载 HTTP 协议细节与错误归一化，不耦合 ComfyUI 节点字段。
"""

from __future__ import annotations

import time
from typing import Dict, Optional

import requests

from logger import logger

from ..core.constants import CONNECT_TIMEOUT, HANDSHAKE_RETRIES, SORA_CREATE_READ_TIMEOUT, SORA_POLL_READ_TIMEOUT
from ..core.interrupt import _ensure_not_interrupted
from ..core.masking import _extract_error_from_html, _mask_text


class VeoVideoClient:
    def __init__(self, session: requests.Session, api_key: str, base_url: str) -> None:
        self._session = session
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or "").strip().rstrip("/")

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _request_with_retries(self, method: str, url: str, **kwargs) -> requests.Response:
        timeout = kwargs.pop("timeout", (CONNECT_TIMEOUT, SORA_POLL_READ_TIMEOUT))
        last_exc: Optional[BaseException] = None
        for attempt in range(HANDSHAKE_RETRIES + 1):
            _ensure_not_interrupted()
            try:
                return self._session.request(method, url, timeout=timeout, **kwargs)
            except requests.exceptions.ConnectTimeout as exc:
                last_exc = exc
                if attempt < HANDSHAKE_RETRIES:
                    logger.warning("Veo 视频接口连接超时，正在重试...")
                    continue
                raise
            except requests.ConnectionError as exc:
                last_exc = exc
                if attempt < HANDSHAKE_RETRIES:
                    logger.warning("Veo 视频接口连接异常，正在重试...")
                    continue
                raise
            except BaseException as exc:
                last_exc = exc
                raise

        if last_exc:
            raise RuntimeError(f"Veo 视频请求失败：{last_exc}") from last_exc
        raise RuntimeError("Veo 视频请求失败：未知错误")

    def _raise_for_http_error(self, response: requests.Response) -> None:
        if response.status_code < 400:
            return

        detail = ""
        try:
            data = response.json()
            if isinstance(data, dict):
                error_obj = data.get("error")
                if isinstance(error_obj, dict):
                    detail = error_obj.get("message") or str(error_obj)
                elif isinstance(error_obj, str):
                    detail = error_obj
                elif data.get("message"):
                    detail = str(data.get("message"))
        except Exception:
            raw_text = response.text or ""
            if "<html" in raw_text.lower() or "<!doctype" in raw_text.lower():
                detail = _extract_error_from_html(raw_text, response.status_code)
            else:
                detail = raw_text[:200]

        masked_detail = _mask_text((detail or "").strip())
        if response.status_code == 401:
            if masked_detail:
                raise RuntimeError(f"密钥不可用或已失效，请检查后再试：{masked_detail}")
            raise RuntimeError("密钥不可用或已失效，请检查后再试")

        raise RuntimeError(f"Veo 视频接口异常：HTTP {response.status_code} {masked_detail}".strip())

    def create(self, payload: Dict[str, object]) -> Dict[str, object]:
        url = f"{self._base_url}/v1/videos"
        response = self._request_with_retries(
            "post",
            url,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            # 显式使用 json=，确保 requests 以 UTF-8 JSON 编码发送（避免 latin-1 问题）
            json=payload,
            timeout=(CONNECT_TIMEOUT, SORA_CREATE_READ_TIMEOUT),
        )
        response.encoding = "utf-8"
        self._raise_for_http_error(response)
        return response.json()

    def status(self, task_id: str) -> Dict[str, object]:
        url = f"{self._base_url}/v1/videos/{task_id}"
        headers = {
            **self._auth_headers(),
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        }
        # 部分中间层/网关可能对 GET 结果有缓存策略；追加 cache-busting 参数确保每次轮询取最新状态
        params = {"_ts": str(time.time_ns())}
        response = self._request_with_retries("get", url, headers=headers, params=params)
        response.encoding = "utf-8"
        self._raise_for_http_error(response)
        return response.json()
