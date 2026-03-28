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
    POINTS_DIVISOR = 5000.0

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
        base_url = (api_base_url or self.config_manager.get_default_api_base_url()).rstrip("/").lower()
        return f"{base_url}|{api_key}"

    def _store_snapshot(self, api_base_url: str, api_key: str, payload: Dict) -> None:
        cache_key = self._balance_cache_key(api_base_url, api_key)
        snapshot = {
            "payload": payload,
            "fetched_at": time.time(),
            "api_base_url": api_base_url,
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

    def _format_points(self, token_value: Optional[float], api_base_url: str = "") -> str:
        if token_value is None:
            return "-"
        try:
            divisor = self.POINTS_DIVISOR
            # [新增] 心宝测试渠道特殊换算： x0.6
            if self.config_manager.is_xinbao_test_base_url(api_base_url):
                divisor = self.POINTS_DIVISOR / 0.6

            points = abs(float(token_value)) / divisor
        except (TypeError, ValueError):
            return "-"
        # 积分展示不需要小数，直接去掉小数部分
        return f"{int(points):,}"

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

    def format_balance_summary(self, snapshot: Dict[str, Dict],
                               include_stale_hint: bool = False) -> str:
        data = snapshot.get("payload", {}).get("data", {})
        api_base_url = snapshot.get("api_base_url", "")

        available_points = self._format_points(data.get("total_available"), api_base_url)
        used_points = self._format_points(data.get("total_used"), api_base_url)
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
            f"剩余可用积分: {available_points}",
            f"已使用积分: {used_points}",
            f"到期: {expires}"
        ]
        if include_stale_hint and self._is_snapshot_stale(snapshot):
            age = self._snapshot_age(snapshot)
            if age is not None:
                summary_lines.append(
                    f"⚠️ 余额信息已 {int(age)}s 未刷新，点击节点按钮获取最新数据"
                )
        return "\n".join(summary_lines)

    def get_cached_balance_text(self, api_base_url: str, api_key: str, *args) -> Optional[str]:
        if args:
            self.logger.warning(f"get_cached_balance_text received extra arguments: {args}")
        sanitized = self.config_manager.sanitize_api_key(api_key)
        if not sanitized:
            return None
        snapshot = self._get_snapshot(api_base_url, sanitized)
        if not snapshot:
            return None
        try:
            return self.format_balance_summary(snapshot, include_stale_hint=True)
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

    def _get_prompt_server(self, prompt_server_provider):
        """统一获取 PromptServer，缺失时安排重试"""
        prompt_server = prompt_server_provider()
        if prompt_server is None:
            self._schedule_route_retry(prompt_server_provider)
            return None
        return prompt_server

    def _has_existing_route(self, prompt_server) -> bool:
        """检测重复路由以避免 aiohttp HEAD 冲突"""
        existing_routes = getattr(prompt_server, "routes", None)
        if not existing_routes:
            return False
        for route_def in list(existing_routes):
            if getattr(route_def, "path", None) == "/banana/token_usage" and str(getattr(route_def, "method", "")).upper() == "GET":
                self.logger.warning("检测到已有 /banana/token_usage GET 路由，跳过重复注册以避免 HEAD 冲突")
                self._route_registered = True
                return True
        return False

    def _parse_request_params(self, request) -> tuple:
        """解析查询参数，统一返回基础路由、API Key 与刷新标志"""
        route_choice = self.config_manager.normalize_xinbao_test_route_label(
            request.rel_url.query.get("route")
        )
        base_url = self.config_manager.get_effective_api_base_url(route_choice)
        refresh = self._parse_bool(request.rel_url.query.get("refresh"))
        api_key_from_request = (
            request.rel_url.query.get("banana_api_key")
            or request.rel_url.query.get("api_key")
            or ""
        ).strip()
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
        return base_url, api_key, refresh, bypass_from_query, disable_ssl_flag

    def _cached_response(self, base_url: str, api_key: Optional[str]):
        """缓存命中响应"""
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

        summary = self.format_balance_summary(snapshot, include_stale_hint=True)
        return web.json_response({
            "success": True,
            "data": snapshot.get("payload", {}).get("data"),
            "raw": snapshot.get("payload"),
            "summary": summary,
            "cached": True,
            "stale": self._is_snapshot_stale(snapshot)
        })

    async def _refresh_response(
        self,
        loop: asyncio.AbstractEventLoop,
        base_url: str,
        api_key: str,
        bypass_from_query: Optional[bool],
        disable_ssl_flag: Optional[bool],
    ):
        """刷新余额并返回响应，异常由调用方捕获"""
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
        summary = self.format_balance_summary(snapshot)
        return web.json_response({
            "success": True,
            "data": snapshot.get("payload", {}).get("data"),
            "raw": snapshot.get("payload"),
            "summary": summary,
            "cached": False,
            "stale": False
        })

    def _register_token_usage_route(self, prompt_server):
        """注册 /banana/token_usage 路由，分层处理查询与刷新"""
        @prompt_server.routes.get("/banana/token_usage")
        async def handle_token_usage(request):
            base_url, api_key, refresh, bypass_from_query, disable_ssl_flag = self._parse_request_params(request)
            loop = asyncio.get_running_loop()

            if not refresh:
                return self._cached_response(base_url, api_key)

            try:
                return await self._refresh_response(loop, base_url, api_key, bypass_from_query, disable_ssl_flag)
            except Exception as exc:
                self.logger.error(f"/banana/token_usage 刷新失败: {exc}")
                return web.json_response(
                    {"success": False, "message": str(exc)},
                    status=400
                )

    def ensure_route(self, prompt_server_provider):
        if self._route_registered:
            return

        prompt_server = self._get_prompt_server(prompt_server_provider)
        if prompt_server is None:
            return

        if self._has_existing_route(prompt_server):
            return

        self._register_token_usage_route(prompt_server)
        self._route_registered = True
