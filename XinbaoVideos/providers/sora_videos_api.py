"""
Sora 视频接口封装（从原 `Xinbao_Video_Generator.py` 迁移）。

仅承载 Sora `/v1/videos` 的 create/status/content 协议细节与错误归一化，
不依赖 ComfyUI 节点字段，避免与 node 层耦合。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import requests

from logger import logger

from ..core.constants import (
    CONNECT_TIMEOUT,
    HANDSHAKE_RETRIES,
    SORA_CREATE_READ_TIMEOUT,
    SORA_POLL_READ_TIMEOUT,
)
from ..core.interrupt import _ensure_not_interrupted
from ..core.masking import _extract_error_from_html, _mask_text


class SoraVideoClient:
    def __init__(self, session: requests.Session, api_key: str, base_url: str) -> None:
        self._session = session
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or "").strip().rstrip("/")

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

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
            # HTML 错误页面（如 Cloudflare 524）：提取关键信息而非截取原始 HTML
            raw_text = response.text or ""
            if "<html" in raw_text.lower() or "<!doctype" in raw_text.lower():
                detail = _extract_error_from_html(raw_text, response.status_code)
            else:
                detail = raw_text[:200]

        raw_detail = (detail or "").strip()
        masked_detail = _mask_text(raw_detail)
        if "prompt is required" in raw_detail.lower():
            masked_detail = "prompt 为必填参数，请填写提示词后重试"
        if response.status_code == 401:
            if masked_detail:
                raise RuntimeError(f"密钥不可用或已失效，请检查后再试：{masked_detail}")
            raise RuntimeError("密钥不可用或已失效，请检查后再试")

        raise RuntimeError(f"Sora 视频接口异常：HTTP {response.status_code} {masked_detail}".strip())

    def _request_with_retries(self, method: str, url: str, **kwargs) -> requests.Response:
        timeout = kwargs.pop("timeout", (CONNECT_TIMEOUT, SORA_POLL_READ_TIMEOUT))
        last_exc: Optional[BaseException] = None
        for attempt in range(HANDSHAKE_RETRIES + 1):
            _ensure_not_interrupted()
            try:
                return self._session.request(
                    method,
                    url,
                    timeout=timeout,
                    **kwargs,
                )
            except requests.exceptions.ConnectTimeout as exc:
                last_exc = exc
                if attempt < HANDSHAKE_RETRIES:
                    logger.warning("Sora 接口连接超时，正在重试...")
                    continue
                raise
            except requests.ConnectionError as exc:
                last_exc = exc
                if attempt < HANDSHAKE_RETRIES:
                    logger.warning("Sora 接口连接异常，正在重试...")
                    continue
                raise
            except BaseException as exc:
                last_exc = exc
                raise

        if last_exc:
            raise RuntimeError(f"Sora 视频请求失败：{last_exc}") from last_exc
        raise RuntimeError("Sora 视频请求失败：未知错误")

    def create(
        self,
        model: str,
        prompt: Optional[str] = None,
        file_payload: Optional[Tuple[str, str, bytes, str]] = None,
        url_payload: Optional[Tuple[str, str]] = None,
        extra_payload: Optional[Dict[str, object]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, object]:
        url = f"{self._base_url}/v1/videos"
        headers = self._auth_headers()
        if isinstance(idempotency_key, str) and idempotency_key.strip():
            headers["Idempotency-Key"] = idempotency_key.strip()

        if file_payload and url_payload:
            raise ValueError("file_payload 与 url_payload 不可同时提供")

        if file_payload:
            field_name, filename, raw_bytes, content_type = file_payload
            files = {field_name: (filename, raw_bytes, content_type)}
            data: Dict[str, object] = {"model": model}
            if prompt is not None:
                data["prompt"] = prompt
            if extra_payload:
                for key, value in extra_payload.items():
                    if key in ("model", "prompt"):
                        continue
                    data[key] = value
            response = self._request_with_retries(
                "post",
                url,
                headers=headers,
                data=data,
                files=files,
                timeout=(CONNECT_TIMEOUT, SORA_CREATE_READ_TIMEOUT),
            )
        else:
            payload: Dict[str, object] = {"model": model}
            if prompt is not None:
                payload["prompt"] = prompt
            if url_payload:
                url_field, url_value = url_payload
                payload[url_field] = url_value
            if extra_payload:
                for key, value in extra_payload.items():
                    if key in ("model", "prompt"):
                        continue
                    payload[key] = value
            response = self._request_with_retries(
                "post",
                url,
                headers={**headers, "Content-Type": "application/json"},
                # 显式使用 json=，确保 requests 以 UTF-8 JSON 编码发送（避免 latin-1 问题）
                json=payload,
                timeout=(CONNECT_TIMEOUT, SORA_CREATE_READ_TIMEOUT),
            )

        response.encoding = "utf-8"
        self._raise_for_http_error(response)
        return response.json()

    def status(self, video_id: str) -> Dict[str, object]:
        url = f"{self._base_url}/v1/videos/{video_id}"
        response = self._request_with_retries("get", url, headers=self._auth_headers())
        response.encoding = "utf-8"
        self._raise_for_http_error(response)
        return response.json()

    def content(self, video_id: str, variant: str = "video") -> requests.Response:
        url = f"{self._base_url}/v1/videos/{video_id}/content"
        response = self._request_with_retries(
            "get",
            url,
            headers=self._auth_headers(),
            params={"variant": variant},
            allow_redirects=False,
        )
        response.encoding = "utf-8"
        return response
