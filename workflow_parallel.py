from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

BANANA_V3_FIRST_POLL_DELAY = 10.0
BANANA_V3_SECOND_POLL_AT = 50.0
BANANA_V3_POLL_INTERVAL = 10.0


def compute_banana_v3_followup_delay(
    *,
    is_first_followup: bool,
    suggested_seconds: float = 0.0,
) -> float:
    """
    计算 BananaV3 后续轮询等待时间。

    基线节奏：
    - 首次轮询：第 10s（由调用方单独控制）
    - 第二次轮询：第 50s，即首轮询后默认再等 40s
    - 之后：每 10s 一次

    若服务端给出更大的建议间隔，则遵守更大的值。
    """
    baseline = (
        BANANA_V3_SECOND_POLL_AT - BANANA_V3_FIRST_POLL_DELAY
        if is_first_followup
        else BANANA_V3_POLL_INTERVAL
    )
    return max(float(baseline), float(suggested_seconds or 0.0))


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


# ---------------------------------------------------------------------------
# 跨节点异步轮询协调器
# ---------------------------------------------------------------------------


class _AsyncPollCoordinator:
    """
    跨节点异步轮询协调器。

    工作流并发模式下，多个 V3 节点的 task_id 注册到同一个协调器，
    由后台单线程统一 batch-get 轮询，结果通过 threading.Event 分发回各节点。

    生命周期：
    - 首个 V3 节点调用 register() 注册 task_id。
    - 任意节点调用 start_polling()（幂等），启动后台轮询线程。
    - 轮询线程持续运行直到所有 pending tasks 完成/失败/超时，或收到 stop 信号。
    - 各节点通过 evt.wait() 等待自己的结果，完成后调用 unregister() 清理。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Dict[str, Tuple[int, threading.Event]] = {}  # task_id -> (batch_idx, event)
        self._results: Dict[str, dict] = {}  # task_id -> succeeded item
        self._failures: Dict[str, str] = {}  # task_id -> failure reason
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started = False

        # 轮询参数（由 start_polling 设置）
        self._api_client: Any = None
        self._api_key: str = ""
        self._base_url: str = ""
        self._first_poll_delay: float = BANANA_V3_FIRST_POLL_DELAY
        self._poll_interval: float = BANANA_V3_POLL_INTERVAL
        self._total_timeout: float = 420.0
        self._poll_retry_max: int = 3
        self._interrupt_checker: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def register(self, task_id: str, batch_idx: int, callback_event: threading.Event) -> None:
        """注册一个 task_id 到协调器，附带 threading.Event 用于通知完成。"""
        with self._lock:
            self._pending[task_id] = (batch_idx, callback_event)

    def get_result(self, task_id: str) -> Optional[dict]:
        """获取已完成 task 的 batch-get item。线程安全。"""
        with self._lock:
            return self._results.get(task_id)

    def get_failure(self, task_id: str) -> Optional[str]:
        """获取失败原因。线程安全。"""
        with self._lock:
            return self._failures.get(task_id)

    def unregister(self, task_id: str) -> None:
        """节点取消或完成后清理 task_id。线程安全。"""
        with self._lock:
            self._pending.pop(task_id, None)
            # 保留 results/failures 让 get_result/get_failure 仍可查询

    def is_alive(self) -> bool:
        """轮询线程是否仍在运行。"""
        return self._poll_thread is not None and self._poll_thread.is_alive()

    def has_pending(self) -> bool:
        """是否还有 pending 的 task。"""
        with self._lock:
            return len(self._pending) > 0

    def start_polling(
        self,
        api_client: Any,
        api_key: str,
        base_url: str,
        first_poll_delay: float,
        poll_interval: float,
        total_timeout: float,
        poll_retry_max: int,
        interrupt_checker: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        启动后台轮询线程（幂等，只启动一次）。

        参数由第一个调用者设定，后续调用不会覆盖已有的轮询参数。
        """
        with self._lock:
            if self._started:
                # #3 修复：检测 API Key 不一致时发出警告
                if self._api_key and api_key and self._api_key != api_key:
                    try:
                        from logger import logger as _wlog
                        _wlog.warning(
                            "[协调轮询] 检测到不同 API Key 的节点共用同一个协调器，"
                            "轮询将使用首个节点的 Key。如需隔离，请关闭工作流并发。"
                        )
                    except Exception:
                        pass
                return
            self._started = True
            self._api_client = api_client
            self._api_key = api_key
            self._base_url = base_url
            self._first_poll_delay = first_poll_delay
            self._poll_interval = poll_interval
            self._total_timeout = total_timeout
            self._poll_retry_max = poll_retry_max
            self._interrupt_checker = interrupt_checker
            self._stop_event.clear()

        t = threading.Thread(target=self._poll_loop, daemon=True, name="AsyncPollCoordinator")
        with self._lock:
            self._poll_thread = t
        t.start()

    def stop(self) -> None:
        """请求停止轮询线程。"""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _interruptible_wait(self, seconds: float) -> None:
        """可中断的等待：以 0.25s 为单位，检查 stop_event 和 interrupt_checker。"""
        elapsed = 0.0
        while elapsed < seconds:
            if self._stop_event.is_set():
                return
            if self._interrupt_checker is not None:
                try:
                    self._interrupt_checker()
                except Exception:
                    self._stop_event.set()
                    return
            chunk = min(0.25, seconds - elapsed)
            time.sleep(chunk)
            elapsed += chunk

    def _do_batch_get_with_retry(self, task_ids: List[str]) -> Optional[dict]:
        """带退避重试的 batch-get 调用。"""
        for retry in range(self._poll_retry_max):
            try:
                result = self._api_client.batch_get_tasks(
                    self._api_key,
                    task_ids,
                    self._base_url,
                )
                return result
            except Exception as e:
                if retry < self._poll_retry_max - 1:
                    backoff = 2 ** (retry + 1)  # 2s, 4s, 8s
                    try:
                        from logger import logger
                        logger.warning(
                            f"[协调轮询] batch-get 失败，{backoff}s 后重试: {e}"
                        )
                    except Exception:
                        pass
                    self._interruptible_wait(backoff)
                else:
                    try:
                        from logger import logger
                        logger.warning(
                            f"[协调轮询] batch-get 连续 {self._poll_retry_max} 次失败，跳过本轮"
                        )
                    except Exception:
                        pass
        return None

    def _cleanup_pending(self, reason: str = "interrupted") -> None:
        """唤醒所有剩余 pending 等待者，标记为指定原因。"""
        with self._lock:
            for tid in list(self._pending.keys()):
                self._failures[tid] = reason
                _, evt = self._pending.pop(tid)
                evt.set()

    def _poll_loop(self) -> None:
        """后台轮询线程主循环。"""
        try:
            from logger import logger as _log
        except Exception:
            _log = None  # type: ignore[assignment]

        try:
            self._poll_loop_inner(_log)
        except Exception as exc:
            if _log:
                _log.warning(f"[协调轮询] 轮询线程异常退出: {exc}")
        finally:
            # #8 修复：任何退出路径都唤醒剩余等待者
            self._cleanup_pending("interrupted")
            # #9 修复：重置 _started 允许后续 start_polling 重启
            with self._lock:
                self._started = False

    def _poll_loop_inner(self, _log) -> None:
        """轮询主循环内部逻辑。"""
        # --- 首次轮询延迟 ---
        if _log:
            _log.info(f"[协调轮询] 等待 {self._first_poll_delay}s 后开始首次 batch-get")
        self._interruptible_wait(self._first_poll_delay)

        poll_start = time.time()
        poll_round = 0

        while not self._stop_event.is_set():
            # #9 修复：在 lock 内检查 pending 是否为空，防止与 register 竞态
            with self._lock:
                current_pending = list(self._pending.keys())
                if not current_pending:
                    if _log:
                        _log.info("[协调轮询] 所有任务已完成，轮询线程退出")
                    return

            if (time.time() - poll_start) > self._total_timeout:
                # 超时：标记所有 pending 为失败
                if _log:
                    _log.warning(
                        f"[协调轮询] 全局超时 ({self._total_timeout}s)，"
                        f"标记剩余 {len(current_pending)} 个任务为超时"
                    )
                with self._lock:
                    for tid in list(self._pending.keys()):
                        self._failures[tid] = "timeout"
                        _, evt = self._pending.pop(tid)
                        evt.set()
                break

            poll_round += 1
            elapsed_s = time.time() - poll_start
            if _log:
                _log.info(
                    f"[协调轮询] 第 {poll_round} 轮 | "
                    f"查询 {len(current_pending)} 个任务 | "
                    f"已等待 {elapsed_s:.0f}s/{self._total_timeout}s"
                )

            batch_response = self._do_batch_get_with_retry(current_pending)

            if batch_response:
                items = batch_response.get("items", [])
                next_poll_ms = batch_response.get("next_poll_after_ms", 0)
                round_succeeded = 0
                round_failed = 0
                with self._lock:
                    for item in items:
                        tid = item.get("id")
                        if tid not in self._pending:
                            continue
                        status = item.get("status", "")
                        if status == "succeeded":
                            self._results[tid] = item
                            _, evt = self._pending.pop(tid)
                            evt.set()
                            round_succeeded += 1
                        elif status in ("failed", "upstream_timeout", "not_found"):
                            # 保留服务端错误详情（如有），便于排查
                            error_detail = item.get("error") or item.get("message") or ""
                            if isinstance(error_detail, dict):
                                error_detail = error_detail.get("message") or str(error_detail)
                            fail_reason = f"{status}: {error_detail}" if error_detail else status
                            self._failures[tid] = fail_reason
                            if _log and error_detail:
                                _log.warning(f"[协调轮询] 任务 {tid} 失败: {fail_reason}")
                            _, evt = self._pending.pop(tid)
                            evt.set()
                            round_failed += 1
                        # running/queued/accepted -> 继续等待
                    remaining = len(self._pending)

                if _log:
                    parts = []
                    if round_succeeded:
                        parts.append(f"{round_succeeded} 个完成")
                    if round_failed:
                        parts.append(f"{round_failed} 个失败")
                    parts.append(f"{remaining} 个等待中")
                    _log.info(f"[协调轮询] 第 {poll_round} 轮结果: {', '.join(parts)}")

                # 检查是否全部完成
                if remaining == 0:
                    if _log:
                        _log.info(f"[协调轮询] 全部任务已完成（共 {poll_round} 轮，耗时 {elapsed_s:.0f}s）")
                    break

                next_poll = compute_banana_v3_followup_delay(
                    is_first_followup=(poll_round == 1),
                    suggested_seconds=next_poll_ms / 1000,
                )
            else:
                next_poll = compute_banana_v3_followup_delay(
                    is_first_followup=(poll_round == 1),
                )

            self._interruptible_wait(next_poll)


_ASYNC_POLL_COORDINATOR: Optional[_AsyncPollCoordinator] = None
_ASYNC_POLL_COORDINATOR_LOCK = threading.Lock()


def get_async_poll_coordinator() -> _AsyncPollCoordinator:
    """
    获取或创建进程级轮询协调器。

    重建条件：协调器曾被启动过（_started == True），但轮询线程已退出且无 pending 任务。
    首次调用或协调器尚未启动时，直接复用已有实例。
    """
    global _ASYNC_POLL_COORDINATOR
    with _ASYNC_POLL_COORDINATOR_LOCK:
        if _ASYNC_POLL_COORDINATOR is None:
            _ASYNC_POLL_COORDINATOR = _AsyncPollCoordinator()
        elif (
            not _ASYNC_POLL_COORDINATOR.is_alive()
            and not _ASYNC_POLL_COORDINATOR.has_pending()
            and _ASYNC_POLL_COORDINATOR._poll_thread is not None
        ):
            # 上一轮已结束（线程曾启动过但已退出），创建新实例
            _ASYNC_POLL_COORDINATOR = _AsyncPollCoordinator()
        return _ASYNC_POLL_COORDINATOR
