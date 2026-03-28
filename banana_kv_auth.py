import threading
import time
import base64
from dataclasses import dataclass
from typing import Optional

import requests
from requests import Response

from config_manager import ConfigManager, KVAuthConfig
from logger import logger


@dataclass
class KVAuthCheckResult:
    """封装 Edge KV 鉴权的结果。"""

    allowed: bool
    from_cache: bool = False
    fail_open: bool = False
    user_message: str = ""
    error_detail: Optional[str] = None


class EdgeKVAuthClient:
    """Edge KV 预检客户端 + 正向缓存。"""

    _CACHE_TTL = 600.0  # 10 分钟正向缓存
    _MSG_UNAVAILABLE = "服务暂不可用，请稍后重试或检查密钥配置"
    _MSG_FORBIDDEN = "密钥格式错误、非本渠道密钥或密钥尚未激活，请稍后再试"
    _GLOBAL_CACHE: dict[str, float] = {}
    _GLOBAL_CACHE_LOCK = threading.Lock()

    def __init__(self, config_manager: ConfigManager, logger_instance=logger) -> None:
        self.config_manager = config_manager
        self.logger = logger_instance
        self._session = requests.Session()
        self._disabled_logged = False

    def _mask_key(self, api_key: str) -> str:
        if not api_key:
            return "<empty>"
        if len(api_key) <= 6:
            return f"{api_key[:3]}...{api_key[-1:]}"
        return f"{api_key[:4]}...{api_key[-2:]}"

    def _get_cached(self, api_key: str) -> bool:
        now = time.time()
        with self._GLOBAL_CACHE_LOCK:
            expires_at = self._GLOBAL_CACHE.get(api_key)
            if expires_at and expires_at > now:
                return True
            if expires_at:
                self._GLOBAL_CACHE.pop(api_key, None)
        return False

    def _store_cache(self, api_key: str) -> None:
        with self._GLOBAL_CACHE_LOCK:
            self._GLOBAL_CACHE[api_key] = time.time() + self._CACHE_TTL

    def _build_verify_flag(
        self,
        cfg: KVAuthConfig,
        verify_ssl: bool,
        disable_ssl_override: Optional[bool],
    ) -> bool:
        if disable_ssl_override is True:
            return False
        if disable_ssl_override is False:
            return True
        return verify_ssl and not cfg.disable_ssl_verify

    def _build_headers(self, cfg: KVAuthConfig) -> dict:
        headers = {"Accept": "application/json"}
        if cfg.token:
            headers["Authorization"] = f"Bearer {cfg.token}"
        return headers

    def _handle_fail_open(self, reason: str, masked_key: str, elapsed: float) -> KVAuthCheckResult:
        self.logger.warning(
            f"[KVAuth] 远端异常已按 fail-open 放行，key={masked_key}，原因={reason}，耗时 {elapsed:.2f}s"
        )
        return KVAuthCheckResult(
            allowed=True,
            from_cache=False,
            fail_open=True,
            user_message="",
        )

    def _handle_forbidden(
        self,
        masked_key: str,
        payload: Optional[dict],
        elapsed: float,
        extra: Optional[str] = None,
    ) -> KVAuthCheckResult:
        detail = extra or ""
        if payload:
            self.logger.warning(
                f"[KVAuth] 拒绝，key={masked_key}，payload={payload}，耗时 {elapsed:.2f}s"
            )
        else:
            if detail:
                self.logger.warning(f"[KVAuth] 拒绝，key={masked_key}，原因={detail}，耗时 {elapsed:.2f}s")
            else:
                self.logger.warning(f"[KVAuth] 拒绝，key={masked_key}，耗时 {elapsed:.2f}s")
        return KVAuthCheckResult(
            allowed=False,
            user_message=self._MSG_FORBIDDEN,
            error_detail="forbidden",
        )

    def _handle_unavailable(self, masked_key: str, detail: str, elapsed: float) -> KVAuthCheckResult:
        self.logger.error(
            f"[KVAuth] 校验失败，key={masked_key}，原因={detail}，耗时 {elapsed:.2f}s"
        )
        return KVAuthCheckResult(
            allowed=False,
            user_message=self._MSG_UNAVAILABLE,
            error_detail=detail,
        )

    def _parse_response(self, response: Response, masked_key: str, elapsed: float, cfg: KVAuthConfig, sanitized: str) -> KVAuthCheckResult:
        status = response.status_code
        if status >= 500:
            if cfg.fail_open_on_5xx:
                return self._handle_fail_open(f"HTTP {status}", masked_key, elapsed)
            return self._handle_unavailable(masked_key, f"HTTP {status}", elapsed)
        if status >= 400:
            body = ""
            try:
                body = (response.text or "").strip()
                # Obfuscated: apicha.yfeng.wang
                kv_host = base64.b64decode("YXBpY2hhLnlmZW5nLndhbmc=").decode()
                body = body.replace(kv_host, "<kv-host>")
            except Exception:
                body = ""
            detail = f"HTTP {status}"
            if body:
                detail = f"{detail} {body[:120]}"
            return self._handle_forbidden(masked_key, None, elapsed, extra=detail)

        try:
            payload = response.json()
        except Exception as exc:
            return self._handle_unavailable(masked_key, f"响应解析失败: {exc}", elapsed)

        ok = bool(payload.get("ok"))
        exists = bool(payload.get("exists"))
        if ok and exists:
            self._store_cache(sanitized)
            self.logger.success(
                f"[KVAuth] KV有效通过，key={masked_key}，耗时 {elapsed:.2f}s"
            )
            return KVAuthCheckResult(allowed=True)

        # ok=False 或 exists=False 视为拒绝
        return self._handle_forbidden(masked_key, payload, elapsed)

    def check(
        self,
        api_key: str,
        verify_ssl: bool = True,
        disable_ssl_override: Optional[bool] = None,
    ) -> KVAuthCheckResult:
        sanitized = self.config_manager.sanitize_api_key(api_key)
        if not sanitized:
            return KVAuthCheckResult(allowed=False, user_message="请填写有效的 API Key")

        cfg = self.config_manager.load_kv_auth_config()
        masked_key = self._mask_key(sanitized)

        if not cfg.enabled:
            if not self._disabled_logged:
                self.logger.warning("KV 预检已关闭（调试模式），请确认风险后再运行")
                self._disabled_logged = True
            return KVAuthCheckResult(allowed=True)

        if self._get_cached(sanitized):
            return KVAuthCheckResult(allowed=True, from_cache=True)

        headers = self._build_headers(cfg)
        verify_flag = self._build_verify_flag(cfg, verify_ssl, disable_ssl_override)
        start = time.time()
        try:
            response = self._session.get(
                cfg.endpoint,
                params={"key": sanitized},
                headers=headers,
                timeout=(cfg.timeout_seconds, cfg.timeout_seconds),
                verify=verify_flag,
            )
            elapsed = time.time() - start
            return self._parse_response(response, masked_key, elapsed, cfg, sanitized)
        except requests.Timeout:
            elapsed = time.time() - start
            if cfg.fail_open_on_5xx:
                return self._handle_fail_open("timeout", masked_key, elapsed)
            return self._handle_unavailable(masked_key, "timeout", elapsed)
        except requests.RequestException as exc:
            elapsed = time.time() - start
            # 脱敏处理：只保留错误类型，避免泄露 URL
            detail = f"连接异常 ({type(exc).__name__})"
            return self._handle_unavailable(masked_key, detail, elapsed)
