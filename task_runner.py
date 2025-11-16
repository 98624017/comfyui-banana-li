from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, List, Optional

import comfy.model_management


ResultType = dict
TaskType = tuple


class BatchGenerationRunner:
    """统一的批次任务调度器，负责线程池并发、进度条与日志回调。"""

    def __init__(
        self,
        logger,
        ensure_not_interrupted: Callable[[], None],
        progress_bar_factory: Callable[[int], object],
    ):
        self.logger = logger
        self.ensure_not_interrupted = ensure_not_interrupted
        self.progress_bar_factory = progress_bar_factory

    def run(
        self,
        tasks: List[TaskType],
        worker_fn: Callable[[TaskType], ResultType],
        batch_size: int,
        actual_workers: int,
        continue_on_error: bool,
        progress_callback: Callable[[ResultType, int, int, object], None],
    ) -> List[ResultType]:
        """通过线程池或串行方式执行任务，并在每个结果返回时调用 progress_callback。"""

        if batch_size <= 0:
            return []

        progress_bar = self.progress_bar_factory(batch_size)
        self.ensure_not_interrupted()

        if actual_workers > 1 and batch_size > 1:
            return self._run_parallel(
                tasks,
                worker_fn,
                batch_size,
                actual_workers,
                continue_on_error,
                progress_callback,
                progress_bar,
            )

        return self._run_sequential(
            tasks,
            worker_fn,
            batch_size,
            continue_on_error,
            progress_callback,
            progress_bar,
        )

    def _run_parallel(
        self,
        tasks: List[TaskType],
        worker_fn: Callable[[TaskType], ResultType],
        batch_size: int,
        actual_workers: int,
        continue_on_error: bool,
        progress_callback: Callable[[ResultType, int, int, object], None],
        progress_bar: object,
    ) -> List[ResultType]:
        results: List[ResultType] = []
        completed = 0
        executor = ThreadPoolExecutor(max_workers=actual_workers)
        should_stop = False

        try:
            future_to_task = {
                executor.submit(worker_fn, task): task
                for task in tasks
            }
            pending = set(future_to_task.keys())

            while pending:
                done, pending = wait(
                    pending,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED
                )

                if not done:
                    continue

                for future in done:
                    task = future_to_task.pop(future, None)
                    try:
                        self.ensure_not_interrupted()
                        result = future.result()
                    except comfy.model_management.InterruptProcessingException:
                        for future_ref in list(future_to_task.keys()):
                            future_ref.cancel()
                        raise
                    except Exception as exc:  # pragma: no cover - worker 应返回统一结构
                        self.logger.error(f"批次任务异常: {exc}")
                        result = {"success": False, "index": -1, "error": str(exc)}

                    results.append(result)
                    completed += 1
                    progress_callback(result, completed, batch_size, progress_bar)

                    if not continue_on_error and not result.get("success"):
                        should_stop = True
                        break

                if should_stop:
                    for future_ref in pending:
                        future_ref.cancel()
                    break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return results

    def _run_sequential(
        self,
        tasks: List[TaskType],
        worker_fn: Callable[[TaskType], ResultType],
        batch_size: int,
        continue_on_error: bool,
        progress_callback: Callable[[ResultType, int, int, object], None],
        progress_bar: object,
    ) -> List[ResultType]:
        results: List[ResultType] = []
        completed = 0

        for task in tasks:
            self.ensure_not_interrupted()
            result = worker_fn(task)
            results.append(result)
            completed += 1
            progress_callback(result, completed, batch_size, progress_bar)

            if not continue_on_error and not result.get("success"):
                break

        return results

