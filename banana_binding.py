"""
绑定上下文与本地注册表工具。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
import uuid
from typing import Any, Dict, Tuple

BINDING_TYPE = "BANANA_BINDING"
_DEFAULT_TTL = 900.0  # 秒
# 前置增强节点全局使用预算：每个 ComfyUI 进程生命周期内，
# 所有前置节点在一个时间窗口内总共只允许使用若干次。
_FRONT_BUDGET_MAX_USES = 3
_FRONT_BUDGET_WINDOW_SECONDS = 300.0  # 5 分钟


class BindingError(Exception):
    """绑定校验失败。"""


def _load_secret() -> bytes:
    env_secret = os.getenv("BANANA_BIND_SECRET")
    if env_secret and len(env_secret) >= 16:
        return env_secret.encode("utf-8", errors="ignore")
    # 内置 fallback，避免用户未配置时无法工作；不在日志暴露。
    return b"xb-binding-secret-keep-me-safe"


_SECRET = _load_secret()


def _normalize_binding(binding: Any) -> Dict[str, Any]:
    if isinstance(binding, dict):
        return binding
    raise BindingError("缺少有效的绑定上下文，请通过“心宝❤绑定生成/透传”节点接入。")


def _sign(session_id: str, issued_at: float, ttl: float) -> str:
    payload = f"{session_id}:{int(issued_at)}:{int(ttl)}"
    digest = hmac.new(_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest


def _extract_core(ctx: Dict[str, Any]) -> Tuple[str, float, float, str, str]:
    session_id = str(ctx.get("session_id") or "").strip()
    issued_at = float(ctx.get("issued_at") or 0.0)
    ttl = float(ctx.get("ttl") or 0.0)
    sig = str(ctx.get("sig") or "").strip()
    state = str(ctx.get("state") or "pending")
    if not session_id:
        raise BindingError("绑定上下文缺少 session_id，请重新生成绑定。")
    if ttl <= 0:
        raise BindingError("绑定上下文的有效期无效，请重新生成绑定。")
    if not sig:
        raise BindingError("绑定上下文缺少签名，请使用官方绑定生成节点。")
    return session_id, issued_at, ttl, sig, state


class BindingRegistry:
    """本地注册表，用于记录已激活的会话以及前置节点使用预算。"""

    def __init__(self) -> None:
        self._activated: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()
        # 全局前置增强使用预算：在 ComfyUI 进程生命周期内计数，
        # 并按时间窗口（例如 5 分钟内最多 3 次）进行限制。
        self._front_budget_used: int = 0
        self._front_budget_started_at: float = 0.0

    def _reset_front_budget_locked(self) -> None:
        """在持有锁的前提下重置前置预算。"""
        self._front_budget_used = 0
        self._front_budget_started_at = 0.0

    def mark_activated(self, session_id: str, issued_at: float, ttl: float, activated_at: float) -> None:
        with self._lock:
            self._activated[session_id] = {
                "issued_at": issued_at,
                "ttl": ttl,
                "activated_at": activated_at,
            }
            # 任意一个会话成功在核心节点激活后，重置前置节点的全局使用预算，
            # 方便已正常使用心宝❤Banana 的用户继续流畅使用前置增强。
            self._reset_front_budget_locked()

    def is_activated(self, session_id: str, now_ts: float) -> bool:
        with self._lock:
            info = self._activated.get(session_id)
            if not info:
                return False
            expired_at = info["issued_at"] + info["ttl"]
            if now_ts > expired_at:
                # 过期即移除
                self._activated.pop(session_id, None)
                return False
            return True

    def consume_front_budget(
        self,
        now_ts: float,
        max_uses: int,
        window_seconds: float,
    ) -> Tuple[bool, int]:
        """
        消耗一次前置增强使用配额。

        返回 (是否允许本次使用, 当前窗口内已使用次数)。
        """
        with self._lock:
            # 如果从未使用过，或已经超过窗口时间，则开启新窗口。
            if self._front_budget_started_at <= 0.0 or now_ts - self._front_budget_started_at > window_seconds:
                self._front_budget_started_at = now_ts
                self._front_budget_used = 0

            if self._front_budget_used >= max_uses:
                return False, self._front_budget_used

            self._front_budget_used += 1
            return True, self._front_budget_used


REGISTRY = BindingRegistry()


def create_binding_context(ttl_seconds: float = _DEFAULT_TTL) -> Dict[str, Any]:
    now_ts = time.time()
    session_id = uuid.uuid4().hex
    ttl = max(60.0, float(ttl_seconds))
    sig = _sign(session_id, now_ts, ttl)
    return {
        "session_id": session_id,
        "issued_at": now_ts,
        "ttl": ttl,
        "sig": sig,
        "state": "pending",
    }


def validate_pending(binding: Any, now_ts: float | None = None) -> Dict[str, Any]:
    ctx = _normalize_binding(binding)
    session_id, issued_at, ttl, sig, state = _extract_core(ctx)
    now_ts = now_ts or time.time()
    expired_at = issued_at + ttl
    if now_ts > expired_at:
        raise BindingError("绑定上下文已过期，请重新生成后再运行。")
    expected_sig = _sign(session_id, issued_at, ttl)
    if not hmac.compare_digest(sig, expected_sig):
        raise BindingError("绑定签名校验失败，请使用“心宝❤绑定生成”节点重新创建绑定。")
    if state not in ("pending", "activated"):
        raise BindingError("绑定状态异常，请重新生成绑定上下文。")
    return ctx


def activate_binding(binding: Any, now_ts: float | None = None) -> Dict[str, Any]:
    ctx = validate_pending(binding, now_ts)
    session_id, issued_at, ttl, _, _ = _extract_core(ctx)
    REGISTRY.mark_activated(session_id, issued_at, ttl, now_ts or time.time())
    ctx["state"] = "activated"
    return ctx


def enforce_front_budget(
    binding: Any,
    now_ts: float | None = None,
    max_uses: int = _FRONT_BUDGET_MAX_USES,
    window_seconds: float = _FRONT_BUDGET_WINDOW_SECONDS,
) -> Dict[str, Any]:
    """
    前置增强节点使用的统一入口：
    - 校验绑定上下文签名与有效期；
    - 叠加全局前置增强使用预算（时间窗口内最多若干次）。
    """
    ctx = validate_pending(binding, now_ts)
    # 这里不需要按 session 维度限制，而是全局限制：
    # 任何绑定上下文在前置节点上的使用，都会计入同一预算，
    # 避免通过大量新建绑定来规避限制。
    now_ts = now_ts or time.time()
    allowed, used = REGISTRY.consume_front_budget(now_ts, max_uses, window_seconds)
    if not allowed:
        raise BindingError(
            "当前前置增强节点在 5 分钟窗口内的全局使用次数已达上限，"
            "请先通过心宝❤Banana 节点成功生成图像以重置额度，或等待几分钟后再试。"
        )
    return ctx


def require_activated(binding: Any, now_ts: float | None = None) -> Dict[str, Any]:
    ctx = validate_pending(binding, now_ts)
    session_id, issued_at, ttl, _, state = _extract_core(ctx)
    now_ts = now_ts or time.time()
    # validate_pending 已经校验过期时间与签名，这里主要关注激活状态。
    # 优先使用本地注册表，其次兜底信任上下文本身的 state 标记，
    # 以避免少数情况下注册表丢失但上下文已标记为 activated 时出现“误报未激活”。
    if REGISTRY.is_activated(session_id, now_ts):
        ctx["state"] = "activated"
        return ctx
    if state == "activated":
        ctx["state"] = "activated"
        return ctx
    # 额外再做一次过期检查（防止 registry 中存在过期但 validate_pending 传入 now_ts 为 None 的调用方）
    if now_ts > issued_at + ttl:
        raise BindingError("绑定已过期，请重新生成绑定。")
    raise BindingError("绑定未激活，请确保绑定上下文已接入心宝❤Banana 节点后再使用后置节点。")


def build_missing_hint() -> str:
    return "缺少绑定上下文。请添加“心宝❤绑定生成”节点，并将其输出连接到当前节点与心宝❤Banana 节点。"
