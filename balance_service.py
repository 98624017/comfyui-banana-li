import asyncio
import os
import sys
import threading
import time
from functools import partial
from typing import Any, Dict, Optional

from aiohttp import web

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from logger import logger


class BalanceService:
    TOKENS_PER_RATE = 100000
    CURRENCY_PER_RATE = 0.20
    BASE_COST_PER_TOKEN = CURRENCY_PER_RATE / TOKENS_PER_RATE

    def __init__(self, api_client, config_manager, logger_instance=logger):
        self.logger = logger_instance
        self.api_client = api_client
        self.config_manager = config_manager
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.Lock()
        self._cache_ttl = 60.0
        self._route_registered = False
        self._route_timer: Optional[threading.Timer] = None

    def _balance_cache_key(self, api_base_url: str, api_key: str) -> str:
        base_url = (api_base_url or self.config_manager.get_effective_api_base_url()).rstrip("/").lower()
        return f"{base_url}|{api_key}"

    def _store_snapshot(self, api_base_url: str, api_key: str, payload: Dict) -> None:
        cache_key = self._balance_cache_key(api_base_url, api_key)
        snapshot = {
            "payload": payload,
            "fetched_at": time.time()
        }
        with self._cache_lock:
            self._cache[cache_key] = snapshot

    def _get_snapshot(self, api_base_url: str, api_key: str) -> Optional[Dict]:
        cache_key = self._balance_cache_key(api_base_url, api_key)
        with self._cache_lock:
            return self._cache.get(cache_key)

    @staticmethod
    def _snapshot_age(snapshot: Optional[Dict]) -> Optional[float]:
        if not snapshot:
            return None
        fetched_at = snapshot.get("fetched_at")
        if not fetched_at:
            return None
        return max(0.0, time.time() - fetched_at)

    def _is_snapshot_stale(self, snapshot: Optional[Dict]) -> bool:
        age = self._snapshot_age(snapshot)
        if age is None:
            return True
        return age > self._cache_ttl

    def refresh_snapshot(
        self,
        api_base_url: str,
        api_key: str,
        timeout: int = 15,
        bypass_proxy: Optional[bool] = None,
        verify_ssl: Optional[bool] = None,
    ) -> None:
        sanitized = self.config_manager.sanitize_api_key(api_key)
        if not sanitized:
            raise ValueError("未配置有效的 API Key")
        # 查询余额时的代理行为只由调用方显式控制，
        # 不再从 config.ini 中读取 bypass_proxy 配置，避免与节点 UI 状态不一致。
        bypass = bool(bypass_proxy) if bypass_proxy is not None else False
        verify = True if verify_ssl is None else bool(verify_ssl)
        payload = self.api_client.fetch_token_usage(
            api_base_url,
            sanitized,
            timeout=timeout,
            bypass_proxy=bypass,
            verify_ssl=verify,
        )
        self._store_snapshot(api_base_url, sanitized, payload)

    @classmethod
    def _format_number(cls, value: Optional[float]) -> str:
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
        yuan = tokens_value * cls.BASE_COST_PER_TOKEN / cost_factor
        return f"¥{yuan:.4f}"

    @staticmethod
    def _format_expiry(timestamp: Optional[int]) -> str:
        if not timestamp or timestamp <= 0:
            return "不过期"
        from datetime import datetime

        try:
            dt = datetime.fromtimestamp(timestamp)
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(timestamp)

    def format_balance_summary(self, snapshot: Dict[str, Dict], cost_factor: float = 1.0,
                               include_stale_hint: bool = False) -> str:
        cost_factor = self.config_manager.clamp_cost_factor(cost_factor)
        data = snapshot.get("payload", {}).get("data", {})
        available = self._format_number(data.get("total_available"))
        used = self._format_number(data.get("total_used"))
        available_cost = self._format_cost(data.get("total_available"), cost_factor)
        used_cost = self._format_cost(data.get("total_used"), cost_factor)
        expires = self._format_expiry(data.get("expires_at"))
        fetched_at = snapshot.get("fetched_at")
        if fetched_at:
            from datetime import datetime
            fetched_text = datetime.fromtimestamp(fetched_at).strftime("%H:%M")
        else:
            from datetime import datetime
            fetched_text = datetime.now().strftime("%H:%M")

        summary_lines = [
            f"🔑 查询时间 {fetched_text}",
            f"估算费用: 可用 {available_cost} / 已用 {used_cost} (仅参考)",
            f"到期: {expires}"
        ]
        if include_stale_hint and self._is_snapshot_stale(snapshot):
            age = self._snapshot_age(snapshot)
            if age is not None:
                summary_lines.append(
                    f"⚠️ 余额信息已 {int(age)}s 未刷新，点击节点按钮获取最新数据"
                )
        return "\n".join(summary_lines)

    def get_cached_balance_text(self, api_base_url: str, api_key: str, cost_factor: float = 1.0) -> Optional[str]:
        sanitized = self.config_manager.sanitize_api_key(api_key)
        if not sanitized:
            return None
        snapshot = self._get_snapshot(api_base_url, sanitized)
        if not snapshot:
            return None
        try:
            return self.format_balance_summary(snapshot, cost_factor, include_stale_hint=True)
        except Exception:
            return None

    def _parse_bool(self, value: Optional[str]) -> bool:
        if value is None:
            return False
        return value.lower() in {"1", "true", "yes", "on"}

    def _schedule_route_retry(self, provider):
        if self._route_timer is not None and self._route_timer.is_alive():
            return

        def _retry():
            self._route_timer = None
            self.ensure_route(provider)

        timer = threading.Timer(1.0, _retry)
        timer.daemon = True
        self._route_timer = timer
        timer.start()

    def ensure_route(self, prompt_server_provider):
        if self._route_registered:
            return
        prompt_server = prompt_server_provider()
        if prompt_server is None:
            self._schedule_route_retry(prompt_server_provider)
            return

        @prompt_server.routes.get("/banana/token_usage")
        async def handle_token_usage(request):
            base_url = self.config_manager.get_effective_api_base_url()
            refresh = self._parse_bool(request.rel_url.query.get("refresh"))
            api_key_from_request = (request.rel_url.query.get("api_key") or "").strip()
            bypass_query_value = request.rel_url.query.get("bypass_proxy")
            bypass_from_query = (
                self._parse_bool(bypass_query_value)
                if bypass_query_value is not None
                else None
            )
            disable_ssl_value = request.rel_url.query.get("disable_ssl_verify")
            disable_ssl_flag = (
                self._parse_bool(disable_ssl_value)
                if disable_ssl_value is not None
                else None
            )
            api_key = (
                self.config_manager.sanitize_api_key(api_key_from_request)
                or self.config_manager.sanitize_api_key(self.config_manager.load_api_key())
            )
            cost_factor = self.config_manager.load_cost_factor()
            loop = asyncio.get_running_loop()

            if not refresh:
                snapshot = None
                if api_key:
                    snapshot = self._get_snapshot(base_url, api_key)
                if snapshot is None:
                    return web.json_response({
                        "success": False,
                        "message": "暂无余额缓存，请点击“查询余额”按钮刷新",
                        "cached": False,
                        "stale": True
                    })

                summary = self.format_balance_summary(snapshot, cost_factor, include_stale_hint=True)
                return web.json_response({
                    "success": True,
                    "data": snapshot.get("payload", {}).get("data"),
                    "raw": snapshot.get("payload"),
                    "summary": summary,
                    "cost_factor": cost_factor,
                    "cached": True,
                    "stale": self._is_snapshot_stale(snapshot)
                })

            try:
                await loop.run_in_executor(
                    None,
                    partial(
                        self.refresh_snapshot,
                        base_url,
                        api_key,
                        bypass_proxy=bypass_from_query,
                        verify_ssl=(
                            None
                            if disable_ssl_flag is None
                            else (not disable_ssl_flag)
                        ),
                    )
                )
                snapshot = self._get_snapshot(base_url, api_key)
                if snapshot is None:
                    raise RuntimeError("余额缓存更新失败")
                summary = self.format_balance_summary(snapshot, cost_factor)
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

        self._route_registered = True
