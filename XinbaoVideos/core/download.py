"""
视频下载（从原 `Xinbao_Video_Generator.py` 迁移）。

要求：超时/分块/进度日志行为保持一致，避免重构引入体验差异。
"""

from __future__ import annotations

import math
import io
import re
import threading
import time
from typing import Dict, Optional

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from logger import logger

from .constants import (
    CONNECT_TIMEOUT,
    DOWNLOAD_CHUNK_SIZE,
    DOWNLOAD_LOG_INTERVAL,
    DOWNLOAD_TIMEOUT,
    DOWNLOAD_TOTAL_TIMEOUT,
)
from .interrupt import _ensure_not_interrupted
from .masking import _clean_url, _mask_text
from .video_types import BananaVideo


def download_video(
    session: requests.Session,
    url: str,
    progress_cb: Optional[callable] = None,
    progress_min: int = 98,
    progress_max: int = 100,
    headers: Optional[Dict[str, str]] = None,
    enable_parallel_range_download: bool = False,
    parallel_range_workers: int = 4,
    parallel_range_min_size_bytes: int = 8 * 1024 * 1024,
) -> BananaVideo:
    logger.info(f"开始下载视频：{_clean_url(url)}")

    def _emit_progress(downloaded: int, total: Optional[int]) -> None:
        if progress_cb is None:
            return
        try:
            if progress_max <= progress_min:
                progress_cb(float(progress_max))
                return
            if total is not None and total > 0:
                ratio = max(0.0, min(1.0, downloaded / total))
                progress_cb(float(progress_min + (progress_max - progress_min) * ratio))
                return
            # 未知大小：下载开始后推进到中间，避免 UI 长时间停留
            progress_cb(float(progress_min + (progress_max - progress_min) * 0.5))
        except Exception:
            pass

    def _parse_total_size_from_content_range(content_range: str) -> Optional[int]:
        # 形如: "bytes 0-0/14998196"
        value = (content_range or "").strip()
        if not value:
            return None
        match = re.search(r"/(\d+)\s*$", value)
        if not match:
            return None
        try:
            total = int(match.group(1))
            return total if total > 0 else None
        except Exception:
            return None

    def _probe_range_total_size(target_url: str) -> Optional[int]:
        if not enable_parallel_range_download:
            return None
        try:
            probe_headers = dict(headers or {})
            probe_headers["Range"] = "bytes=0-0"
            with session.get(
                target_url,
                headers=probe_headers,
                timeout=(CONNECT_TIMEOUT, DOWNLOAD_TIMEOUT),
                stream=True,
            ) as resp:
                if resp.status_code != 206:
                    return None
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if content_type and not (
                    content_type.startswith("video/") or content_type == "application/octet-stream"
                ):
                    return None
                total = _parse_total_size_from_content_range(resp.headers.get("Content-Range") or "")
                if total is None or total <= 0:
                    return None
                return total
        except Exception:
            return None

    def _clone_session(base_session: requests.Session) -> requests.Session:
        cloned = requests.Session()
        try:
            cloned.verify = getattr(base_session, "verify", True)
        except Exception:
            pass
        try:
            cloned.trust_env = getattr(base_session, "trust_env", True)
        except Exception:
            pass
        try:
            cloned.proxies = dict(getattr(base_session, "proxies", {}) or {})
        except Exception:
            pass
        return cloned

    def _fetch_content_parallel_ranges(target_url: str, total_bytes: int, workers: int) -> io.BytesIO:
        worker_count = int(workers or 0)
        if worker_count < 2:
            raise ValueError("workers 必须 >= 2")

        segment_size = int(math.ceil(total_bytes / worker_count))
        ranges = []
        for i in range(worker_count):
            start = i * segment_size
            if start >= total_bytes:
                break
            end = min(total_bytes - 1, (i + 1) * segment_size - 1)
            ranges.append((start, end))

        if len(ranges) < 2:
            raise ValueError("分块范围计算失败：range 数量不足")

        logger.info(
            "检测到源站支持 Range 分块下载，启用并行下载：{} 并发，大小 {:.1f}MB".format(
                len(ranges),
                total_bytes / (1024 * 1024),
            )
        )

        start_time = time.time()
        last_log_time = start_time
        last_log_bytes = 0
        downloaded_bytes = 0
        progress_lock = threading.Lock()
        stop_event = threading.Event()

        raw_buf = bytearray(total_bytes)
        view = memoryview(raw_buf)

        def _update_progress(delta: int) -> None:
            nonlocal downloaded_bytes, last_log_time, last_log_bytes
            if delta <= 0:
                return
            now = time.time()
            with progress_lock:
                downloaded_bytes += delta
                if now - last_log_time >= DOWNLOAD_LOG_INTERVAL:
                    interval_seconds = max(now - last_log_time, 1e-6)
                    interval_bytes = downloaded_bytes - last_log_bytes
                    speed_mib = (interval_bytes / interval_seconds) / (1024 * 1024)
                    pct = (downloaded_bytes / total_bytes) * 100.0
                    logger.info(
                        "分块下载中：{:.1f}% ({:.1f}MB/{:.1f}MB, {:.2f}MB/s)".format(
                            pct,
                            downloaded_bytes / (1024 * 1024),
                            total_bytes / (1024 * 1024),
                            speed_mib,
                        )
                    )
                    _emit_progress(downloaded_bytes, total_bytes)
                    last_log_time = now
                    last_log_bytes = downloaded_bytes

        def _download_range(start: int, end: int) -> None:
            if stop_event.is_set():
                return

            local_session = _clone_session(session)
            try:
                range_headers = dict(headers or {})
                range_headers["Range"] = f"bytes={start}-{end}"
                expected = end - start + 1
                written = 0

                with local_session.get(
                    target_url,
                    headers=range_headers,
                    timeout=(CONNECT_TIMEOUT, DOWNLOAD_TIMEOUT),
                    stream=True,
                ) as resp:
                    if resp.status_code != 206:
                        raise RuntimeError(f"分块下载失败：HTTP {resp.status_code}")

                    for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if stop_event.is_set():
                            return
                        _ensure_not_interrupted()
                        now = time.time()
                        if now - start_time > DOWNLOAD_TOTAL_TIMEOUT:
                            raise RuntimeError(f"视频下载整体超时（>{int(DOWNLOAD_TOTAL_TIMEOUT)}s），请稍后重试")
                        if not chunk:
                            continue
                        chunk_len = len(chunk)
                        view[start + written : start + written + chunk_len] = chunk
                        written += chunk_len
                        _update_progress(chunk_len)

                if written != expected:
                    raise RuntimeError(f"分块下载长度不匹配：期望 {expected} 实际 {written}")
            except Exception:
                stop_event.set()
                raise
            finally:
                try:
                    local_session.close()
                except Exception:
                    pass

        futures = []
        executor = ThreadPoolExecutor(max_workers=len(ranges))
        try:
            for start, end in ranges:
                futures.append(executor.submit(_download_range, int(start), int(end)))

            for future in as_completed(futures):
                future.result()
        except Exception:
            stop_event.set()
            for future in futures:
                future.cancel()
            raise
        finally:
            # 强制等待线程结束，避免 Windows ProactorEventLoop 在线程销毁前触发异常导致卡死
            executor.shutdown(wait=True)

        if downloaded_bytes <= 0:
            raise RuntimeError("视频下载失败：响应体为空")
        if downloaded_bytes != total_bytes:
            raise RuntimeError(f"分块下载不完整：期望 {total_bytes} 实际 {downloaded_bytes}")

        elapsed_seconds = max(time.time() - start_time, 1e-6)
        avg_speed_mib = (downloaded_bytes / elapsed_seconds) / (1024 * 1024)
        logger.info(
            "分块下载完成：{:.1f}MB/{:.1f}MB，用时 {:.1f}s，平均 {:.2f}MB/s".format(
                downloaded_bytes / (1024 * 1024),
                total_bytes / (1024 * 1024),
                elapsed_seconds,
                avg_speed_mib,
            )
        )

        _emit_progress(downloaded_bytes, total_bytes)
        if progress_cb is not None:
            try:
                progress_cb(float(progress_max))
            except Exception:
                pass

        content_buf = io.BytesIO(raw_buf)
        content_buf.seek(0)
        return content_buf

    def _fetch_content(target_url: str) -> io.BytesIO:
        start_time = time.time()
        last_log_time = start_time
        last_log_bytes = 0
        downloaded_bytes = 0

        with session.get(
            target_url,
            headers=headers,
            timeout=(CONNECT_TIMEOUT, DOWNLOAD_TIMEOUT),
            stream=True,
        ) as resp:
            headers_elapsed = max(time.time() - start_time, 0.0)
            if resp.status_code != 200:
                resp.raise_for_status()

            content_type = (resp.headers.get("Content-Type") or "").lower()
            if content_type and not (
                content_type.startswith("video/") or content_type == "application/octet-stream"
            ):
                preview = ""
                try:
                    for chunk in resp.iter_content(chunk_size=1024):
                        if chunk:
                            preview = chunk[:200].decode("utf-8", errors="replace")
                        break
                except Exception:
                    preview = ""
                raise RuntimeError(
                    f"视频下载失败：源站返回非视频内容（{content_type}）{_mask_text((preview or '').strip())}"
                )

            total_bytes: Optional[int] = None
            content_length = resp.headers.get("Content-Length") or resp.headers.get("content-length") or ""
            if content_length:
                try:
                    total_bytes = int(content_length)
                except Exception:
                    total_bytes = None

            if headers_elapsed >= 2.0:
                if total_bytes is not None and total_bytes > 0:
                    logger.info(
                        "下载响应已建立：HTTP {}, {:.1f}MB，耗时 {:.1f}s".format(
                            resp.status_code,
                            total_bytes / (1024 * 1024),
                            headers_elapsed,
                        )
                    )
                else:
                    logger.info(
                        "下载响应已建立：HTTP {}，大小未知，耗时 {:.1f}s".format(
                            resp.status_code,
                            headers_elapsed,
                        )
                    )

            content_buf = io.BytesIO()
            first_chunk_logged = False
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                _ensure_not_interrupted()
                now = time.time()
                if now - start_time > DOWNLOAD_TOTAL_TIMEOUT:
                    raise RuntimeError(f"视频下载整体超时（>{int(DOWNLOAD_TOTAL_TIMEOUT)}s），请稍后重试")

                if chunk:
                    if not first_chunk_logged:
                        first_chunk_logged = True
                        first_chunk_elapsed = max(now - start_time, 0.0)
                        logger.info(f"开始接收视频数据：首包耗时 {first_chunk_elapsed:.1f}s")
                    content_buf.write(chunk)
                    downloaded_bytes += len(chunk)

                if now - last_log_time >= DOWNLOAD_LOG_INTERVAL:
                    interval_seconds = max(now - last_log_time, 1e-6)
                    interval_bytes = downloaded_bytes - last_log_bytes
                    speed_mib = (interval_bytes / interval_seconds) / (1024 * 1024)

                    if total_bytes is not None and total_bytes > 0:
                        pct = (downloaded_bytes / total_bytes) * 100.0
                        logger.info(
                            "下载中：{:.1f}% ({:.1f}MB/{:.1f}MB, {:.2f}MB/s)".format(
                                pct,
                                downloaded_bytes / (1024 * 1024),
                                total_bytes / (1024 * 1024),
                                speed_mib,
                            )
                        )
                    else:
                        logger.info(
                            "下载中：{:.1f}MB ({:.2f}MB/s)".format(
                                downloaded_bytes / (1024 * 1024),
                                speed_mib,
                            )
                        )

                    _emit_progress(downloaded_bytes, total_bytes)
                    last_log_time = now
                    last_log_bytes = downloaded_bytes

            if downloaded_bytes <= 0:
                raise RuntimeError("视频下载失败：响应体为空")

            content_buf.seek(0)
            elapsed_seconds = max(time.time() - start_time, 1e-6)
            avg_speed_mib = (downloaded_bytes / elapsed_seconds) / (1024 * 1024)
            if total_bytes is not None and total_bytes > 0:
                logger.info(
                    "下载完成：{:.1f}MB/{:.1f}MB，用时 {:.1f}s，平均 {:.2f}MB/s".format(
                        downloaded_bytes / (1024 * 1024),
                        total_bytes / (1024 * 1024),
                        elapsed_seconds,
                        avg_speed_mib,
                    )
                )
            else:
                logger.info(
                    "下载完成：{:.1f}MB，用时 {:.1f}s，平均 {:.2f}MB/s".format(
                        downloaded_bytes / (1024 * 1024),
                        elapsed_seconds,
                        avg_speed_mib,
                    )
                )

            _emit_progress(downloaded_bytes, total_bytes)
            if progress_cb is not None:
                try:
                    progress_cb(float(progress_max))
                except Exception:
                    pass
            return content_buf

    trimmed = _clean_url(url)
    candidates = [url]
    if trimmed and trimmed != url:
        candidates.append(trimmed)

    last_exc: Optional[BaseException] = None
    for idx, attempt_url in enumerate(candidates):
        is_last = idx >= len(candidates) - 1
        try:
            total_bytes = _probe_range_total_size(attempt_url)
            if total_bytes is not None and total_bytes >= int(parallel_range_min_size_bytes or 0):
                try:
                    worker_cap = max(2, min(int(parallel_range_workers or 4), 8))
                    # 小文件避免过多分块：大致按 2MB 一个分块，至少 2 个，至多 worker_cap 个
                    recommended = max(2, int(total_bytes // (2 * 1024 * 1024)))
                    workers = max(2, min(recommended, worker_cap))
                    buf = _fetch_content_parallel_ranges(attempt_url, total_bytes, workers)
                    return BananaVideo(buf)
                except Exception as exc:
                    logger.warning(f"Range 分块下载失败，将回退普通下载：{type(exc).__name__} {_mask_text(str(exc))}")

            buf = _fetch_content(attempt_url)
            return BananaVideo(buf)
        except requests.exceptions.ConnectTimeout as exc:
            last_exc = exc
            raise RuntimeError("视频下载失败：连接超时，请检查网络或线路选择") from exc
        except requests.exceptions.ReadTimeout as exc:
            last_exc = exc
            raise RuntimeError("视频下载失败：读取超时，源站可能限速或链接暂不可用，请稍后重试") from exc
        except requests.exceptions.ChunkedEncodingError as exc:
            last_exc = exc
            raise RuntimeError("视频下载失败：传输中断，可能是网络波动导致，请重试") from exc
        except requests.RequestException as exc:
            last_exc = exc
            if not is_last:
                logger.warning(f"下载失败（{type(exc).__name__}），将尝试备用 URL...")
                continue
            raise RuntimeError(f"视频下载失败：连接异常（{type(exc).__name__}）") from exc
        except Exception as exc:
            last_exc = exc
            if not is_last:
                logger.warning(f"下载失败（{type(exc).__name__}），将尝试备用 URL...")
                continue
            raise

    if last_exc:
        raise RuntimeError(f"视频下载失败：{type(last_exc).__name__}") from last_exc
    raise RuntimeError("视频下载失败：未知错误")
