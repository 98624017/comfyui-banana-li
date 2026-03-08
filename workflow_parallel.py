from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional


class _ParallelUrlCollector:
    """
    线程安全的 URL 收集器，用于工作流并发模式下多个调用共享。

    每个并发调用通过 add() 注册自己生成的 URL，最后由 drain() 一次性取出并清空。
    使用 threading.Lock 而非 asyncio.Lock，因为 generate_images() 运行在
    asyncio.to_thread 的工作线程中。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._urls: list[str] = []
        self._failed_urls: list[str] = []

    def add(self, urls: list[str]) -> None:
        """追加一批成功生成的 URL（线程安全）。"""
        with self._lock:
            self._urls.extend(urls)

    def add_failed(self, urls: list[str]) -> None:
        """追加一批下载失败的 URL（线程安全）。"""
        with self._lock:
            self._failed_urls.extend(urls)

    def drain(self) -> tuple[list[str], list[str]]:
        """取出全部 URL 并清空收集器（线程安全）。返回 (generated_urls, failed_urls)。"""
        with self._lock:
            urls = list(self._urls)
            failed = list(self._failed_urls)
            self._urls.clear()
            self._failed_urls.clear()
            return urls, failed


class _WorkflowConcurrencyGate:
    """
    为“启用/未启用工作流并发”的节点提供跨节点（跨文件）的并发闸门（读写锁语义）。

    目标（KISS）：
    - 允许“启用并发”的节点之间并发执行（shared/read）。
    - “未启用并发”的节点必须与任何节点互斥执行（exclusive/write），避免误触发并发洪峰。
    - 不依赖 prompt_id（节点侧不可稳定获取），因此该闸门是进程级全局。

    说明：
    - 读写锁采用 turnstile + room_empty 的经典实现，提供写优先：当写者到来后，阻止新的读者插队，避免写者饥饿。
    """

    def __init__(self) -> None:
        self._turnstile = asyncio.Lock()
        self._room_empty = asyncio.Lock()
        self._readers_lock = asyncio.Lock()
        self._readers = 0
        self._collect_readers = 0
        self._url_collector: _ParallelUrlCollector | None = None

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        # 读者通过 turnstile，确保写者到来后不会持续被新读者饿死（写优先）。
        async with self._turnstile:
            pass

        async with self._readers_lock:
            self._readers += 1
            if self._readers == 1:
                await self._room_empty.acquire()

        try:
            yield
        finally:
            async with self._readers_lock:
                self._readers -= 1
                if self._readers == 0:
                    # 防御性清理：即使通过 shared() 退出，也清理残留的收集器
                    self._url_collector = None
                    self._room_empty.release()

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        await self._turnstile.acquire()
        await self._room_empty.acquire()
        try:
            yield
        finally:
            self._room_empty.release()
            self._turnstile.release()

    @asynccontextmanager
    async def shared_with_url_collect(self) -> AsyncIterator[dict[str, Any]]:
        """
        shared lock + URL 收集器，用于工作流并发模式的链接汇总合并。

        语义：
        - 第一个进入 shared_with_url_collect 的调用（_collect_readers 0→1）创建新的收集器。
        - 每个调用通过 ctx["collector"].add(urls) 注册自己的 URL。
        - 最后一个退出 shared_with_url_collect 的调用（_collect_readers 1→0）drain 收集器，
          将结果写入 ctx["consolidated_urls"]，供调用方打印合并汇总。
        """
        # --- 进入 shared lock（与 shared() 相同逻辑） ---
        async with self._turnstile:
            pass

        async with self._readers_lock:
            self._readers += 1
            if self._readers == 1:
                await self._room_empty.acquire()
            self._collect_readers += 1
            if self._collect_readers == 1 or self._url_collector is None:
                # 首个收集读者：创建新收集器。
                # 关键修复：即使已有普通 shared() 读者，也必须确保收集器存在。
                self._url_collector = _ParallelUrlCollector()
            collector = self._url_collector
        ctx: dict[str, Any] = {
            "collector": collector,
            "consolidated_urls": None,
            "consolidated_failed_urls": None,
        }

        try:
            yield ctx
        finally:
            async with self._readers_lock:
                self._readers -= 1
                if self._collect_readers > 0:
                    self._collect_readers -= 1

                # 最后一个收集读者退出时，立即合并 URL，
                # 避免被仍在运行的普通 shared() 节点吞掉收集结果。
                if self._collect_readers == 0:
                    active_collector = self._url_collector
                    if active_collector is not None:
                        urls, failed = active_collector.drain()
                        ctx["consolidated_urls"] = urls
                        ctx["consolidated_failed_urls"] = failed
                    self._url_collector = None

                if self._readers == 0:
                    self._room_empty.release()


_WORKFLOW_CONCURRENCY_GATE: _WorkflowConcurrencyGate | None = None
_WORKFLOW_CONCURRENCY_GATE_LOOP: Optional[asyncio.AbstractEventLoop] = None
_PARALLEL_WORKFLOW_STAGGER_LOCK: asyncio.Lock | None = None
_PARALLEL_WORKFLOW_STAGGER_LOCK_LOOP: Optional[asyncio.AbstractEventLoop] = None
_LAST_PARALLEL_WORKFLOW_START: float | None = None


def _get_current_event_loop() -> Optional[asyncio.AbstractEventLoop]:
    """
    获取当前调用上下文可用的 event loop。

    说明：
    - 在 ComfyUI 的 async 节点执行上下文中应当总能拿到 running loop。
    - 某些环境下（或同步导入阶段）可能没有 running loop，此时返回 None。
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def get_workflow_concurrency_gate() -> _WorkflowConcurrencyGate:
    """
    获取“工作流并发闸门”（进程级）。
    """
    global _WORKFLOW_CONCURRENCY_GATE
    global _WORKFLOW_CONCURRENCY_GATE_LOOP

    current_loop = _get_current_event_loop()
    # 关键修复：ComfyUI 在部分执行路径下可能会为每次运行创建不同的 event loop，
    # 若复用旧 loop 绑定的 asyncio.Lock，会触发：
    # RuntimeError: <asyncio.locks.Lock ...> is bound to a different event loop
    # 因此当检测到 loop 变化时，重建闸门与内部锁，避免跨 loop 复用。
    #
    # 修复 2025-01-07：批量执行场景下 event loop 可能在执行间隙被替换，
    # 例如 ComfyUI 对批次的调度可能使用不同的 asyncio event loop。
    # 新增条件：若之前记录的 loop 不为 None 但当前拿不到 running loop，说明上下文已切换，
    # 此时也应主动重建以避免下一次真正获取到新 loop 时发生冲突。
    need_rebuild = (
        _WORKFLOW_CONCURRENCY_GATE is None
        or (_WORKFLOW_CONCURRENCY_GATE_LOOP is None and current_loop is not None)
        or (_WORKFLOW_CONCURRENCY_GATE_LOOP is not None and current_loop is None)
        or (
            _WORKFLOW_CONCURRENCY_GATE_LOOP is not None
            and current_loop is not None
            and current_loop is not _WORKFLOW_CONCURRENCY_GATE_LOOP
        )
    )
    if need_rebuild:
        _WORKFLOW_CONCURRENCY_GATE = _WorkflowConcurrencyGate()
        _WORKFLOW_CONCURRENCY_GATE_LOOP = current_loop
    return _WORKFLOW_CONCURRENCY_GATE


def get_workflow_parallel_shared_lock() -> Any:
    """
    "启用工作流并发"的节点使用：共享锁。

    - 多个启用并发的节点可以同时进入。
    - 若存在未启用并发的节点在执行（exclusive），共享锁会等待。
    """
    return get_workflow_concurrency_gate().shared()


def get_workflow_parallel_shared_lock_with_url_collect() -> Any:
    """
    "启用工作流并发"的节点使用：共享锁 + URL 收集器。

    - 共享锁语义与 get_workflow_parallel_shared_lock() 相同。
    - 额外提供 URL 收集器，用于并发调用间的链接汇总合并。
    - 返回异步上下文管理器，yield dict 包含 collector 和 consolidated_urls。
    """
    return get_workflow_concurrency_gate().shared_with_url_collect()


def get_workflow_parallel_exclusive_lock() -> Any:
    """
    “未启用工作流并发”的节点使用：独占锁。

    - 保证该节点与任何启用并发/未启用并发节点都不会同时执行。
    """
    return get_workflow_concurrency_gate().exclusive()


def get_non_parallel_workflow_lock() -> Any:
    """
    兼容旧接口：为“未开启工作流并发模式”的节点提供跨节点（跨文件）的独占锁。
    """
    return get_workflow_parallel_exclusive_lock()


def get_parallel_workflow_stagger_lock() -> asyncio.Lock:
    """
    为“已开启工作流并发模式”的节点提供启动错峰锁。

    设计目标（KISS）：
    - 不引入复杂的全局队列/限流器
    - 仅用于让并发节点的启动时间稍微错开，降低同一瞬间请求洪峰触发网关 5xx/限流的概率
    """
    global _PARALLEL_WORKFLOW_STAGGER_LOCK
    global _PARALLEL_WORKFLOW_STAGGER_LOCK_LOOP
    global _LAST_PARALLEL_WORKFLOW_START

    current_loop = _get_current_event_loop()
    # 修复 2025-01-07：与 get_workflow_concurrency_gate 保持一致的 loop 检测逻辑
    need_rebuild = (
        _PARALLEL_WORKFLOW_STAGGER_LOCK is None
        or (_PARALLEL_WORKFLOW_STAGGER_LOCK_LOOP is None and current_loop is not None)
        or (_PARALLEL_WORKFLOW_STAGGER_LOCK_LOOP is not None and current_loop is None)
        or (
            _PARALLEL_WORKFLOW_STAGGER_LOCK_LOOP is not None
            and current_loop is not None
            and current_loop is not _PARALLEL_WORKFLOW_STAGGER_LOCK_LOOP
        )
    )
    if need_rebuild:
        _PARALLEL_WORKFLOW_STAGGER_LOCK = asyncio.Lock()
        _PARALLEL_WORKFLOW_STAGGER_LOCK_LOOP = current_loop
        _LAST_PARALLEL_WORKFLOW_START = None
    return _PARALLEL_WORKFLOW_STAGGER_LOCK


async def stagger_parallel_workflow_start(min_interval_seconds: float = 0.35) -> None:
    """
    并发模式节点启动错峰。

    - 仅影响“开始发起请求”的时间点，不改变节点内部的轮询/下载逻辑。
    - 默认间隔较小（~350ms），主要用于削峰而非限速。
    """
    if min_interval_seconds <= 0:
        return

    lock = get_parallel_workflow_stagger_lock()
    async with lock:
        global _LAST_PARALLEL_WORKFLOW_START
        loop = asyncio.get_running_loop()
        now = loop.time()
        if _LAST_PARALLEL_WORKFLOW_START is None:
            _LAST_PARALLEL_WORKFLOW_START = now
            return

        target = _LAST_PARALLEL_WORKFLOW_START + float(min_interval_seconds)
        delay = target - now
        if delay > 0:
            await asyncio.sleep(delay)

        _LAST_PARALLEL_WORKFLOW_START = loop.time()


def make_execution_blocker(message: str | None) -> Any:
    """
    构造 ComfyUI 的 ExecutionBlocker。

    说明：
    - 在 ComfyUI 环境中返回 `comfy_execution.graph_utils.ExecutionBlocker`。
    - 在非 ComfyUI 环境中提供轻量 fallback，便于离线导入/静态检查（不会用于实际执行）。
    """
    try:
        from comfy_execution.graph_utils import ExecutionBlocker  # type: ignore

        return ExecutionBlocker(message)
    except Exception:
        class _FallbackExecutionBlocker:
            def __init__(self, message: str | None) -> None:
                self.message = message

        return _FallbackExecutionBlocker(message)
