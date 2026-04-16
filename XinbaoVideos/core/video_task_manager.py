from __future__ import annotations

import glob
import json
import os
import random
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

from aiohttp import web
import requests

import folder_paths

from logger import logger

from ..core.download import download_video
from ..core.masking import _clean_url, _mask_text
from ..providers.doubao_videos_api import DoubaoVideoClient
from ..providers.sora_videos_api import SoraVideoClient
from ..providers.veo_videos_api import VeoVideoClient


try:
    from server import PromptServer
except ImportError:
    class _DummyPromptServer:
        instance = None
        routes = None

    PromptServer = _DummyPromptServer()

try:
    import tomllib
    _TOML_LIB_TYPE = "std"
except ImportError:
    try:
        import toml as tomllib
        _TOML_LIB_TYPE = "pkg"
    except ImportError:
        tomllib = None
        _TOML_LIB_TYPE = None


_PLUGIN_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_HISTORY_PATH = os.path.join(_PLUGIN_ROOT, "history.toml")
_HISTORY_LOCK_PATH = _HISTORY_PATH + ".lock"
_HISTORY_BAK_PATH = _HISTORY_PATH + ".bak"

_DEFAULT_MAX_TERMINAL_TASKS = 100
_DEFAULT_POLL_TICK_SECONDS = 2.0
_DEFAULT_POLL_INTERVAL_SECONDS = 15.0

_TERMINAL_STATUSES = {"success", "failed"}
_ACTIVE_STATUSES = {"pending", "processing"}
_VIDEO_EXTENSIONS: frozenset = frozenset({".mp4", ".webm", ".mov", ".avi"})


def _now_ts() -> int:
    return int(time.time())


def _safe_int(value: object, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _safe_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _serialize_local_file_inline(lf: Dict[str, Any]) -> Optional[str]:
    """将 local_file 字典序列化为 TOML inline table 字符串。

    仅在 filename 非空时返回有效字符串，否则返回 None。
    """
    if not isinstance(lf, dict):
        return None
    fn = _safe_str(lf.get("filename")).strip()
    if not fn:
        return None
    sf = _safe_str(lf.get("subfolder")).strip()
    ft = _safe_str(lf.get("type")).strip() or "output"
    return "{ filename = %s, subfolder = %s, type = %s }" % (
        json.dumps(fn),
        json.dumps(sf, ensure_ascii=False),
        json.dumps(ft),
    )


def _sanitize_filename_component(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return "unknown"
    out = []
    for ch in raw:
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    sanitized = "".join(out).strip("._")
    return sanitized or "unknown"


def _local_file_to_abs_path(lf: Dict[str, Any]) -> Optional[str]:
    """将 local_file 字典转为绝对磁盘路径（无安全校验，仅拼接）。

    找不到文件时返回 None。适用于内部可信数据（如后台下载写入的记录）。
    """
    fn = _safe_str((lf or {}).get("filename")).strip()
    if not fn:
        return None
    sf = _safe_str((lf or {}).get("subfolder")).strip()
    ft = _safe_str((lf or {}).get("type")).strip() or "output"
    base = {
        "input": folder_paths.get_input_directory(),
        "temp": folder_paths.get_temp_directory(),
    }.get(ft, folder_paths.get_output_directory())
    path = os.path.join(base, sf, fn) if sf else os.path.join(base, fn)
    return path if os.path.isfile(path) else None


def _resolve_local_file_path(local_file: Dict[str, Any]) -> str | None:
    filename = _safe_str((local_file or {}).get("filename")).strip()
    subfolder = _safe_str((local_file or {}).get("subfolder")).strip()
    file_type = _safe_str((local_file or {}).get("type")).strip() or "output"

    if not filename:
        return None
    if os.path.basename(filename) != filename:
        return None

    base_dir = folder_paths.get_directory_by_type(file_type)
    if not base_dir:
        return None

    norm_subfolder = os.path.normpath(subfolder).replace("\\", "/").strip("/")
    if not norm_subfolder or norm_subfolder.startswith("..") or ":" in norm_subfolder:
        return None

    candidate = os.path.abspath(os.path.join(base_dir, norm_subfolder, filename))
    try:
        base_abs = os.path.abspath(base_dir)
        if os.path.commonpath([base_abs, candidate]) != base_abs:
            return None
    except Exception:
        return None

    if not os.path.isfile(candidate):
        return None
    return candidate


def _open_in_windows_explorer_select(file_path: str) -> bool:
    if os.name != "nt":
        return False

    path = (file_path or "").strip()
    if not path or not os.path.isfile(path):
        return False

    # explorer.exe 参数解析对引号较敏感，按常见用法拼为单参数最稳妥：/select,"C:\path\file.mp4"
    try:
        arg = f'/select,"{os.path.normpath(path)}"'
        subprocess.Popen(["explorer.exe", arg], close_fds=True)
        return True
    except Exception as exc:
        logger.warning(f"打开资源管理器失败：{type(exc).__name__} {_mask_text(str(exc))}")
        return False


@contextmanager
def _acquire_history_process_lock(timeout_seconds: float = 10.0):
    """
    跨进程文件锁：使用独立的 .lock 文件。

    说明：
    - history.toml 本身使用"读-改-写 + 原子替换"，但仍需要跨进程互斥，避免并发写入导致文件损坏。
    - 锁范围仅覆盖文件读写，不应包住网络请求（避免长时间占锁）。
    """
    os.makedirs(os.path.dirname(_HISTORY_LOCK_PATH), exist_ok=True)
    handle = open(_HISTORY_LOCK_PATH, "a+b")
    try:
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
        except Exception:
            pass

        handle.seek(0)
        start = time.time()

        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() - start > timeout_seconds:
                        raise TimeoutError("获取 history.toml 跨进程锁超时")
                    time.sleep(0.05)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError):
                    if time.time() - start > timeout_seconds:
                        raise TimeoutError("获取 history.toml 跨进程锁超时")
                    time.sleep(0.05)

        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            handle.close()
        except Exception:
            pass


class VideoTaskHistoryStore:
    """
    history.toml 任务存储（轻量数据库）。

    设计目标：
    - KISS：单文件存储 + 原子写入 + 互斥锁，避免引入 DB 依赖。
    - 安全：Key 不落盘；仅存 task 元数据与结果 URL/本地文件引用。
    - 可恢复：保留 .bak，文件损坏时可回退。
    """

    SCHEMA_VERSION = 1

    def __init__(self, max_terminal_tasks: int = _DEFAULT_MAX_TERMINAL_TASKS) -> None:
        self._lock = threading.RLock()
        self._max_terminal_tasks = max(1, int(max_terminal_tasks or _DEFAULT_MAX_TERMINAL_TASKS))
        self._memory_cache: List[Dict[str, Any]] = []
        self._allow_overwrite_without_toml = not os.path.exists(_HISTORY_PATH)

        if not tomllib:
            logger.warning("视频任务中心：未检测到 tomllib/toml，history.toml 将退化为内存模式（重启会丢失）")

    def _normalize_loaded(self, loaded: object) -> Dict[str, Any]:
        if not isinstance(loaded, dict):
            return {"schema_version": self.SCHEMA_VERSION, "tasks": []}
        tasks = loaded.get("tasks", [])
        if not isinstance(tasks, list):
            tasks = []
        normalized_tasks: List[Dict[str, Any]] = []
        for item in tasks:
            if isinstance(item, dict):
                normalized_tasks.append(dict(item))
        schema = loaded.get("schema_version")
        schema_version = _safe_int(schema, self.SCHEMA_VERSION) or self.SCHEMA_VERSION
        return {"schema_version": schema_version, "tasks": normalized_tasks}

    def _load_from_disk_unlocked(self) -> Dict[str, Any]:
        if not tomllib:
            return {"schema_version": self.SCHEMA_VERSION, "tasks": list(self._memory_cache)}
        if not os.path.exists(_HISTORY_PATH):
            return {"schema_version": self.SCHEMA_VERSION, "tasks": []}

        try:
            mode = "rb" if _TOML_LIB_TYPE == "std" else "r"
            encoding = None if _TOML_LIB_TYPE == "std" else "utf-8"
            
            with open(_HISTORY_PATH, mode, encoding=encoding) as handle:
                loaded = tomllib.load(handle)
            return self._normalize_loaded(loaded)
        except Exception as exc:
            logger.warning(f"视频任务中心：读取 history.toml 失败，将尝试回退 .bak：{type(exc).__name__} {_mask_text(str(exc))}")
            if os.path.exists(_HISTORY_BAK_PATH):
                try:
                    mode = "rb" if _TOML_LIB_TYPE == "std" else "r"
                    encoding = None if _TOML_LIB_TYPE == "std" else "utf-8"
                    
                    with open(_HISTORY_BAK_PATH, mode, encoding=encoding) as handle:
                        loaded = tomllib.load(handle)
                    return self._normalize_loaded(loaded)
                except Exception as bak_exc:
                    logger.warning(
                        f"视频任务中心：读取 history.toml.bak 也失败，将使用空历史：{type(bak_exc).__name__} {_mask_text(str(bak_exc))}"
                    )
            return {"schema_version": self.SCHEMA_VERSION, "tasks": []}

    def _trim_tasks_unlocked(self, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        active: List[Dict[str, Any]] = []
        terminal: List[Dict[str, Any]] = []
        for task in tasks:
            status = _safe_str(task.get("status")).strip().lower()
            if status in _TERMINAL_STATUSES:
                terminal.append(task)
            else:
                active.append(task)

        terminal.sort(key=lambda t: _safe_int(t.get("created_at"), 0), reverse=True)
        terminal = terminal[: self._max_terminal_tasks]

        # 输出给 UI 以"新任务优先"的顺序：created_at 降序
        merged = active + terminal
        merged.sort(key=lambda t: _safe_int(t.get("created_at"), 0), reverse=True)
        return merged

    def _toml_dump_unlocked(self, data: Dict[str, Any]) -> str:
        tasks = data.get("tasks", [])
        if not isinstance(tasks, list):
            tasks = []
        lines: List[str] = []
        lines.append("schema_version = 1\n")
        for task in tasks:
            if not isinstance(task, dict):
                continue
            lines.append("\n[[tasks]]\n")
            # 必填字段
            task_id = _safe_str(task.get("id")).strip()
            if task_id:
                lines.append(f"id = {json.dumps(task_id)}\n")

            created_at = _safe_int(task.get("created_at"), 0)
            if created_at > 0:
                lines.append(f"created_at = {created_at}\n")

            provider = _safe_str(task.get("provider")).strip()
            if provider:
                lines.append(f"provider = {json.dumps(provider)}\n")

            model = _safe_str(task.get("model")).strip()
            if model:
                lines.append(f"model = {json.dumps(model)}\n")

            route_choice = _safe_str(task.get("route_choice")).strip()
            if route_choice:
                lines.append(f"route_choice = {json.dumps(route_choice, ensure_ascii=False)}\n")

            base_url = _safe_str(task.get("base_url")).strip()
            if base_url:
                lines.append(f"base_url = {json.dumps(base_url)}\n")

            verify_ssl = task.get("verify_ssl")
            if isinstance(verify_ssl, bool):
                lines.append(f"verify_ssl = {'true' if verify_ssl else 'false'}\n")

            bypass_proxy = task.get("bypass_proxy")
            if isinstance(bypass_proxy, bool):
                lines.append(f"bypass_proxy = {'true' if bypass_proxy else 'false'}\n")

            prompt = _safe_str(task.get("prompt")).strip()
            if prompt:
                lines.append(f"prompt = {json.dumps(prompt, ensure_ascii=False)}\n")

            status = _safe_str(task.get("status")).strip()
            if status:
                lines.append(f"status = {json.dumps(status)}\n")

            api_key_source = _safe_str(task.get("api_key_source")).strip()
            if api_key_source:
                lines.append(f"api_key_source = {json.dumps(api_key_source)}\n")

            video_url = _safe_str(task.get("video_url")).strip()
            if video_url:
                lines.append(f"video_url = {json.dumps(video_url)}\n")

            lf_inline = _serialize_local_file_inline(task.get("local_file") or {})
            if lf_inline:
                lines.append(f"local_file = {lf_inline}\n")

            local_files = task.get("local_files")
            if isinstance(local_files, list) and local_files:
                items = [s for lf in local_files if (s := _serialize_local_file_inline(lf))]
                if items:
                    lines.append("local_files = [%s]\n" % ", ".join(items))

            retry_count = _safe_int(task.get("retry_count"), 0)
            if retry_count > 0:
                lines.append(f"retry_count = {retry_count}\n")

            last_checked_at = _safe_int(task.get("last_checked_at"), 0)
            if last_checked_at > 0:
                lines.append(f"last_checked_at = {last_checked_at}\n")

            next_poll_at = _safe_int(task.get("next_poll_at"), 0)
            if next_poll_at > 0:
                lines.append(f"next_poll_at = {next_poll_at}\n")

            progress = task.get("progress")
            if isinstance(progress, (int, float)):
                try:
                    clamped = float(min(max(float(progress), 0.0), 100.0))
                    lines.append(f"progress = {clamped:.2f}\n")
                except Exception:
                    pass

            error = _safe_str(task.get("error")).strip()
            if error:
                lines.append(f"error = {json.dumps(error, ensure_ascii=False)}\n")

            # banana_v3 扩展字段
            image_urls = task.get("image_urls")
            if isinstance(image_urls, list) and image_urls:
                # TOML 数组序列化
                url_items = ", ".join(json.dumps(u) for u in image_urls if isinstance(u, str))
                lines.append(f"image_urls = [{url_items}]\n")

            image_count = task.get("image_count")
            if isinstance(image_count, int) and image_count > 0:
                lines.append(f"image_count = {image_count}\n")

            batch_size_val = task.get("batch_size")
            if isinstance(batch_size_val, int) and batch_size_val > 0:
                lines.append(f"batch_size = {batch_size_val}\n")

            aspect_ratio = _safe_str(task.get("aspect_ratio")).strip()
            if aspect_ratio:
                lines.append(f"aspect_ratio = {json.dumps(aspect_ratio)}\n")

            seed_val = task.get("seed")
            if isinstance(seed_val, int):
                lines.append(f"seed = {seed_val}\n")

        return "".join(lines)

    def _write_to_disk_unlocked(self, data: Dict[str, Any]) -> None:
        if not tomllib and os.path.exists(_HISTORY_PATH) and not self._allow_overwrite_without_toml:
            logger.warning("视频任务中心：TOML 解析器不可用，已跳过写入以避免覆盖现有 history.toml。")
            return

        os.makedirs(os.path.dirname(_HISTORY_PATH), exist_ok=True)

        try:
            if os.path.exists(_HISTORY_PATH):
                try:
                    shutil.copy2(_HISTORY_PATH, _HISTORY_BAK_PATH)
                except Exception:
                    pass
        except Exception:
            pass

        content = self._toml_dump_unlocked(data)
        tmp_path = f"{_HISTORY_PATH}.tmp.{os.getpid()}.{random.randint(1000, 9999)}"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, _HISTORY_PATH)

        # 同步一份到内存（在无解析器模式下用于读取）
        tasks = data.get("tasks", [])
        if isinstance(tasks, list):
            self._memory_cache = [dict(t) for t in tasks if isinstance(t, dict)]

    def _mutate(self, mutator) -> object:
        with self._lock:
            with _acquire_history_process_lock():
                data = self._load_from_disk_unlocked()
                result = mutator(data)
                tasks = data.get("tasks", [])
                if not isinstance(tasks, list):
                    tasks = []
                data["schema_version"] = self.SCHEMA_VERSION
                data["tasks"] = self._trim_tasks_unlocked([dict(t) for t in tasks if isinstance(t, dict)])
                self._write_to_disk_unlocked(data)
                return result

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            with _acquire_history_process_lock():
                data = self._load_from_disk_unlocked()
                tasks = data.get("tasks", [])
                if not isinstance(tasks, list):
                    return []
                snapshot = [dict(t) for t in tasks if isinstance(t, dict)]
                return self._trim_tasks_unlocked(snapshot)

    def upsert_task(self, task: Dict[str, Any]) -> None:
        task_id = _safe_str(task.get("id")).strip()
        if not task_id:
            return

        def _do(data: Dict[str, Any]) -> None:
            tasks = data.get("tasks", [])
            if not isinstance(tasks, list):
                tasks = []
            existing_idx = -1
            for idx, item in enumerate(tasks):
                if isinstance(item, dict) and _safe_str(item.get("id")).strip() == task_id:
                    existing_idx = idx
                    break

            if existing_idx >= 0:
                merged = dict(tasks[existing_idx])
                merged.update({k: v for k, v in task.items() if v is not None})
                tasks[existing_idx] = merged
            else:
                tasks.append(dict(task))
            data["tasks"] = tasks

        self._mutate(_do)

    def update_task(self, task_id: str, fields: Dict[str, Any]) -> None:
        if not task_id:
            return

        def _do(data: Dict[str, Any]) -> None:
            tasks = data.get("tasks", [])
            if not isinstance(tasks, list):
                return
            for idx, item in enumerate(tasks):
                if not isinstance(item, dict):
                    continue
                if _safe_str(item.get("id")).strip() != task_id:
                    continue
                merged = dict(item)
                for key, value in (fields or {}).items():
                    if value is None:
                        continue
                    merged[key] = value
                tasks[idx] = merged
                break
            data["tasks"] = tasks

        self._mutate(_do)

    def touch_poll_now(self, task_ids: List[str]) -> None:
        ids = [str(i).strip() for i in (task_ids or []) if str(i).strip()]
        if not ids:
            return
        now = _now_ts()

        def _do(data: Dict[str, Any]) -> None:
            tasks = data.get("tasks", [])
            if not isinstance(tasks, list):
                return
            for idx, item in enumerate(tasks):
                if not isinstance(item, dict):
                    continue
                tid = _safe_str(item.get("id")).strip()
                if tid not in ids:
                    continue
                merged = dict(item)
                merged["next_poll_at"] = now
                current_status = _safe_str(merged.get("status")).strip().lower()
                if current_status in {"failed", "waiting_key"}:
                    merged["status"] = "processing"
                    merged["error"] = ""
                    merged["retry_count"] = 0
                tasks[idx] = merged
            data["tasks"] = tasks

        self._mutate(_do)

    def delete_tasks(self, task_ids: list) -> int:
        ids_set = set(task_ids)
        deleted = 0

        def _do(data: Dict[str, Any]) -> None:
            nonlocal deleted
            tasks = data.get("tasks", [])
            if not isinstance(tasks, list):
                return
            original_len = len(tasks)
            tasks[:] = [t for t in tasks if not (isinstance(t, dict) and _safe_str(t.get("id")).strip() in ids_set)]
            deleted = original_len - len(tasks)
            data["tasks"] = tasks

        self._mutate(_do)
        return deleted


class VideoTaskKeyCache:
    """
    Key 仅内存缓存（不落盘）。

    说明：
    - 为保持 KISS 与安全边界，不做任何持久化。
    - 支持按 provider + route_choice 维度存储；route_choice 为空时视为 provider 全局默认。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: Dict[Tuple[str, str], str] = {}

    def set_key(self, provider: str, api_key: str, route_choice: str = "") -> None:
        p = (provider or "").strip().lower()
        r = (route_choice or "").strip()
        k = (api_key or "").strip()
        if not p or not k:
            return
        with self._lock:
            self._cache[(p, r)] = k

    def get_key(self, provider: str, route_choice: str = "") -> Optional[str]:
        p = (provider or "").strip().lower()
        r = (route_choice or "").strip()
        if not p:
            return None
        with self._lock:
            hit = self._cache.get((p, r))
            if hit:
                return hit
            # provider 级兜底（route_choice 为空）
            return self._cache.get((p, "")) or None


class VideoTaskSettings:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._auto_download = False
        self._download_subfolder = "video_tasks"

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "auto_download": bool(self._auto_download),
                "download_subfolder": str(self._download_subfolder),
            }

    def set_auto_download(self, enabled: bool) -> None:
        with self._lock:
            self._auto_download = bool(enabled)

    def is_auto_download_enabled(self) -> bool:
        with self._lock:
            return bool(self._auto_download)

    def download_subfolder(self) -> str:
        with self._lock:
            return str(self._download_subfolder)


def _try_extract_http_status_code(message: str) -> Optional[int]:
    raw = (message or "").strip()
    if not raw:
        return None
    import re

    match = re.search(r"HTTP\s+(\d{3})", raw)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _extract_sora_video_url(payload: Dict[str, object]) -> Optional[str]:
    candidate = payload.get("video_url")
    if isinstance(candidate, str) and candidate.strip():
        return _clean_url(candidate.strip())
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("video_url")
        if isinstance(nested, str) and nested.strip():
            return _clean_url(nested.strip())
    return None


def _extract_veo_video_url(payload: Dict[str, object]) -> Optional[str]:
    root_url = payload.get("video_url")
    if isinstance(root_url, str) and root_url.strip():
        return _clean_url(root_url.strip())
    content = payload.get("content")
    if isinstance(content, dict):
        url = content.get("video_url") or content.get("url")
        if isinstance(url, str) and url.strip():
            return _clean_url(url.strip())
    detail = payload.get("detail")
    if isinstance(detail, dict):
        url = detail.get("video_url")
        if isinstance(url, str) and url.strip():
            return _clean_url(url.strip())
    return None


def _extract_doubao_video_url(payload: Dict[str, object]) -> Optional[str]:
    content = payload.get("content")
    if isinstance(content, dict):
        url = content.get("video_url")
        if isinstance(url, str) and url.strip():
            return _clean_url(url.strip())
    return None


def _extract_comfy_api_result_url(payload: Dict[str, object]) -> Optional[str]:
    """从 AI 应用轮询响应中提取结果资源 URL（兼容多种返回结构）。"""

    def _first_url(value: object) -> Optional[str]:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith(("http://", "https://")):
                return _clean_url(text)
        return None

    def _from_results(items: object) -> Optional[str]:
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict):
                for key in ("video_url", "url", "image_url"):
                    hit = _first_url(item.get(key))
                    if hit:
                        return hit
            else:
                hit = _first_url(item)
                if hit:
                    return hit
        return None

    def _from_payload(obj: object) -> Optional[str]:
        if not isinstance(obj, dict):
            return None
        hit = _from_results(obj.get("results"))
        if hit:
            return hit

        output = obj.get("output")
        if isinstance(output, dict):
            hit = _from_results(output.get("images"))
            if hit:
                return hit

        for key in ("video_url", "url", "image_url"):
            hit = _first_url(obj.get(key))
            if hit:
                return hit
        return None

    hit = _from_payload(payload)
    if hit:
        return hit

    hit = _from_payload(payload.get("data"))
    if hit:
        return hit

    return None


def _extract_error_message(payload: Dict[str, object]) -> str:
    error_obj = payload.get("error")
    if isinstance(error_obj, dict):
        message = error_obj.get("message") or error_obj.get("detail") or error_obj.get("error")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return str(error_obj)
    if isinstance(error_obj, str) and error_obj.strip():
        return error_obj.strip()
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return ""


def _compute_backoff_seconds(retry_count: int, *, base: float = 2.0, cap: float = 60.0) -> float:
    """
    退避策略：指数退避 + jitter。

    说明：
    - 轮询属于幂等 GET；遇到 429/5xx/网络波动时应保守退避，避免放大故障。
    """
    n = max(0, int(retry_count))
    n = min(n, 8)
    delay = float(base) * (2**n)
    delay = min(delay, float(cap))
    jitter = random.random() * 1.2  # 0~1.2s
    return delay + jitter


class VideoTaskDaemon:
    def __init__(self, history: VideoTaskHistoryStore, keys: VideoTaskKeyCache, settings: VideoTaskSettings) -> None:
        self._history = history
        self._keys = keys
        self._settings = settings
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._session_cache: Dict[Tuple[bool, bool], requests.Session] = {}
        self._download_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="BananaDownload")

    def _resolve_task_network_options(self, task: Dict[str, Any]) -> Tuple[bool, bool]:
        """
        从任务记录中解析网络选项。

        说明：
        - verify_ssl: 是否启用 SSL 证书验证（默认 True）
        - bypass_proxy: 是否绕过代理（默认 False，等价于 session.trust_env=True）
        """
        verify_ssl_raw = task.get("verify_ssl")
        bypass_proxy_raw = task.get("bypass_proxy")

        verify_ssl = True
        if isinstance(verify_ssl_raw, bool):
            verify_ssl = bool(verify_ssl_raw)

        bypass_proxy = False
        if isinstance(bypass_proxy_raw, bool):
            bypass_proxy = bool(bypass_proxy_raw)

        return verify_ssl, bypass_proxy

    @staticmethod
    def _make_session(verify_ssl: bool, bypass_proxy: bool) -> requests.Session:
        """创建配置好的 requests.Session（不缓存）。"""
        session = requests.Session()
        if bypass_proxy:
            session.trust_env = False
            session.proxies = {}
        session.verify = verify_ssl
        return session

    def _get_session(self, verify_ssl: bool, bypass_proxy: bool) -> requests.Session:
        """根据网络选项复用/创建 session（仅限轮询主线程使用）。"""
        key = (bool(verify_ssl), bool(bypass_proxy))
        cached = self._session_cache.get(key)
        if cached is not None:
            return cached
        session = self._make_session(*key)
        self._session_cache[key] = session
        return session

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        thread = threading.Thread(target=self._run, name="BananaVideoTaskDaemon", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._download_executor.shutdown(wait=False, cancel_futures=True)

    # ── 后台下载（与轮询线程解耦） ──────────────────────────

    def _download_in_background(
        self,
        task_id: str,
        provider: str,
        urls: List[str],
        verify_ssl: bool,
        bypass_proxy: bool,
        is_batch: bool,
    ) -> None:
        """将下载任务提交到线程池，立即返回不阻塞轮询。"""
        try:
            self._download_executor.submit(
                self._bg_download_and_update,
                task_id, provider, urls, verify_ssl, bypass_proxy, is_batch,
            )
        except RuntimeError:
            pass  # executor 已 shutdown，跳过本次下载

    def _bg_download_and_update(
        self,
        task_id: str,
        provider: str,
        urls: List[str],
        verify_ssl: bool,
        bypass_proxy: bool,
        is_batch: bool,
    ) -> None:
        """后台工作线程：下载文件并补写 local_file/local_files。

        在 ThreadPoolExecutor 中运行。失败不影响已写入的 success 状态。
        """
        session = None
        try:
            # requests.Session 不是线程安全的，后台线程需创建独立实例
            session = self._make_session(verify_ssl, bypass_proxy)

            saved_files: List[Dict[str, str]] = []
            for idx, url in enumerate(urls):
                download_id = f"{task_id}_{idx}" if is_batch and len(urls) > 1 else task_id
                try:
                    local_file = self._try_auto_download(
                        download_id, provider, url, session=session,
                    )
                    if local_file:
                        saved_files.append(local_file)
                except Exception as exc:
                    logger.warning(
                        f"后台下载失败（{download_id}）："
                        f"{type(exc).__name__} {_mask_text(str(exc))}"
                    )

            if saved_files:
                fields: Dict[str, Any] = {"local_file": saved_files[0]}
                # 保留部分下载保护：仅全部成功时设置 local_files
                if is_batch and len(saved_files) == len(urls):
                    fields["local_files"] = saved_files
                self._history.update_task(task_id, fields)
                try:
                    from app.assets.scanner import seed_assets
                    seed_assets(("output",))
                except Exception:
                    logger.debug("资产中心扫描跳过（ComfyUI 版本不支持或扫描失败）")
        except Exception as exc:
            logger.warning(
                f"后台下载异常（{task_id}）："
                f"{type(exc).__name__} {_mask_text(str(exc))}"
            )
        finally:
            if session:
                try:
                    session.close()
                except Exception:
                    pass

    # ── 轮询主循环 ─────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                logger.warning(f"视频任务守护线程异常（已忽略）：{type(exc).__name__} {_mask_text(str(exc))}")
            self._stop_event.wait(float(_DEFAULT_POLL_TICK_SECONDS))

    def _tick(self) -> None:
        tasks = self._history.list_tasks()
        if not tasks:
            return

        now = _now_ts()
        banana_v3_due: List[Dict[str, Any]] = []
        other_due: List[Dict[str, Any]] = []

        for task in tasks:
            if not isinstance(task, dict):
                continue
            status = _safe_str(task.get("status")).strip().lower()
            if status not in _ACTIVE_STATUSES:
                continue
            next_poll_at = _safe_int(task.get("next_poll_at"), 0)
            if next_poll_at > now:
                continue

            provider = _safe_str(task.get("provider")).strip().lower()
            if provider == "banana_v3":
                banana_v3_due.append(task)
            else:
                other_due.append(task)

        # BananaV3: 批量轮询（一次 batch-get 查询所有到期任务）
        if banana_v3_due:
            try:
                self._poll_banana_v3_batch(banana_v3_due, now)
            except Exception as exc:
                logger.warning(
                    f"[banana_v3] 批量轮询异常，降级为逐个轮询: {type(exc).__name__} {_mask_text(str(exc))}"
                )
                for task in banana_v3_due:
                    try:
                        self._process_task(task, now)
                    except Exception as inner_exc:
                        tid = _safe_str(task.get("id")).strip()
                        logger.warning(
                            f"视频任务轮询异常（已忽略）：{tid or 'unknown'} {type(inner_exc).__name__} {_mask_text(str(inner_exc))}"
                        )

        # 其他 provider: 逐个轮询（不变）
        for task in other_due:
            try:
                self._process_task(task, now)
            except Exception as exc:
                task_id = _safe_str(task.get("id")).strip()
                logger.warning(
                    f"视频任务轮询异常（已忽略）：{task_id or 'unknown'} {type(exc).__name__} {_mask_text(str(exc))}"
                )

    def _process_task(self, task: Dict[str, Any], now: int) -> None:
        task_id = _safe_str(task.get("id")).strip()
        provider = _safe_str(task.get("provider")).strip().lower()
        route_choice = _safe_str(task.get("route_choice")).strip()
        base_url = _safe_str(task.get("base_url")).strip()

        if not task_id or not provider or not base_url:
            if task_id:
                self._history.update_task(task_id, {"status": "failed", "error": "任务记录缺少必要字段（provider/base_url）"})
            return

        api_key = self._keys.get_key(provider, route_choice)
        if not api_key:
            self._history.update_task(
                task_id,
                {
                    "status": "waiting_key",
                    "error": "等待用户在前端助手输入 Key",
                    "next_poll_at": 0,
                },
            )
            return

        # banana_v3 使用独立的轮询逻辑，不走通用 _query_status 路径
        if provider == "banana_v3":
            return self._poll_banana_v3_status(task, api_key, now)

        try:
            verify_ssl, bypass_proxy = self._resolve_task_network_options(task)
            session = self._get_session(verify_ssl=verify_ssl, bypass_proxy=bypass_proxy)
            payload, raw_status, progress_num = self._query_status(provider, task_id, api_key, base_url, session=session)
        except Exception as exc:
            self._handle_poll_exception(task, now, exc)
            return

        fields: Dict[str, Any] = {
            "last_checked_at": int(now),
            "retry_count": 0,
        }
        if progress_num is not None:
            fields["progress"] = float(progress_num)

        status = (raw_status or "").strip().lower()
        if status in ("queued", "pending"):
            fields["status"] = "pending"
            fields["error"] = ""
            fields["next_poll_at"] = now + int(_DEFAULT_POLL_INTERVAL_SECONDS + random.random() * 2.0)
            self._history.update_task(task_id, fields)
            return

        if status in ("processing", "in_progress", "running"):
            fields["status"] = "processing"
            fields["error"] = ""
            fields["next_poll_at"] = now + int(_DEFAULT_POLL_INTERVAL_SECONDS + random.random() * 2.0)
            self._history.update_task(task_id, fields)
            return

        if status == "completed":
            video_url = self._extract_video_url(provider, task_id, api_key, base_url, payload, session=session)
            if not video_url:
                completed_seen_at = _safe_int(task.get("completed_seen_at"), 0)
                if completed_seen_at <= 0:
                    completed_seen_at = now
                waited = now - completed_seen_at
                if waited <= 60:
                    fields["status"] = "processing"
                    fields["completed_seen_at"] = completed_seen_at
                    fields["error"] = "任务已完成但 video_url 尚未就绪，继续等待"
                    fields["next_poll_at"] = now + 3
                    self._history.update_task(task_id, fields)
                    return
                fields["status"] = "failed"
                fields["error"] = "任务已完成但 video_url 仍未就绪（等待超时）"
                fields["next_poll_at"] = 0
                self._history.update_task(task_id, fields)
                return

            fields["status"] = "success"
            fields["video_url"] = video_url
            # 上游有时不会返回 100%，但既然已完成（且链接可用），本地应统一显示为 100%。
            fields["progress"] = 100.0
            fields["error"] = ""
            fields["next_poll_at"] = 0

            # 立即将 success 状态落盘，用户无需等待下载完成
            self._history.update_task(task_id, fields)

            # 后台异步下载：图片/AI应用总是下载；视频受 auto_download 开关控制
            _ext = self._guess_ext_from_url(video_url)
            _is_video = _ext in _VIDEO_EXTENSIONS
            _should_download = (not _is_video) or self._settings.is_auto_download_enabled()
            if _should_download:
                verify_ssl, bypass_proxy = self._resolve_task_network_options(task)
                self._download_in_background(
                    task_id, provider, [video_url],
                    verify_ssl, bypass_proxy, is_batch=False,
                )
            return

        if status in ("failed", "error", "canceled", "cancelled"):
            reason = _extract_error_message(payload) if isinstance(payload, dict) else ""
            fields["status"] = "failed"
            fields["error"] = (reason or "任务失败（上游返回 failed）").strip()
            fields["next_poll_at"] = 0
            self._history.update_task(task_id, fields)
            return

        # 未知状态：保守处理为 processing + 定期轮询
        fields["status"] = "processing"
        fields["error"] = ""
        fields["next_poll_at"] = now + int(_DEFAULT_POLL_INTERVAL_SECONDS + random.random() * 2.0)
        self._history.update_task(task_id, fields)

    def _query_status(
        self,
        provider: str,
        task_id: str,
        api_key: str,
        base_url: str,
        *,
        session: requests.Session,
    ) -> Tuple[Dict[str, object], str, Optional[float]]:
        if provider == "sora":
            client = SoraVideoClient(session=session, api_key=api_key, base_url=base_url)
            payload = client.status(task_id)
        elif provider == "veo":
            client = VeoVideoClient(session=session, api_key=api_key, base_url=base_url)
            payload = client.status(task_id)
        elif provider == "doubao":
            client = DoubaoVideoClient(session=session, api_key=api_key, base_url=base_url)
            payload = client.status(task_id)
        elif provider == "comfy_api":
            # AI应用：同一 API 接口 GET /v1/videos/{task_id}，复用 SoraVideoClient
            client = SoraVideoClient(session=session, api_key=api_key, base_url=base_url)
            payload = client.status(task_id)
        else:
            raise RuntimeError(f"未知 provider：{provider}")

        raw_status = _safe_str(payload.get("status")).strip()
        progress_val = payload.get("progress")
        progress_num: Optional[float] = None
        if isinstance(progress_val, (int, float)):
            progress_num = float(progress_val)
        elif isinstance(progress_val, str):
            try:
                progress_num = float(progress_val.strip())
            except Exception:
                progress_num = None

        return payload, raw_status, progress_num

    def _extract_video_url(
        self,
        provider: str,
        task_id: str,
        api_key: str,
        base_url: str,
        status_payload: Dict[str, object],
        *,
        session: requests.Session,
    ) -> Optional[str]:
        if provider == "sora":
            url = _extract_sora_video_url(status_payload)
            if url:
                return url
            # 兜底：尝试 content 接口（可能返回 302 Location）
            try:
                client = SoraVideoClient(session=session, api_key=api_key, base_url=base_url)
                resp = client.content(task_id, variant="video")
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location") or resp.headers.get("location")
                    if location and isinstance(location, str) and location.strip():
                        return _clean_url(location.strip())
            except Exception:
                return None
            return None
        if provider == "veo":
            return _extract_veo_video_url(status_payload)
        if provider == "doubao":
            return _extract_doubao_video_url(status_payload)
        if provider == "comfy_api":
            return _extract_comfy_api_result_url(status_payload)
        return None

    def _handle_poll_exception(self, task: Dict[str, Any], now: int, exc: BaseException) -> None:
        task_id = _safe_str(task.get("id")).strip()
        provider = _safe_str(task.get("provider")).strip().lower()
        route_choice = _safe_str(task.get("route_choice")).strip()
        verify_ssl, bypass_proxy = self._resolve_task_network_options(task)

        msg = _safe_str(exc).strip()
        masked = _mask_text(msg)
        status_code = _try_extract_http_status_code(msg)
        if task_id:
            logger.warning(
                "视频任务轮询失败（将按退避重试）：{} provider={} route={} verify_ssl={} bypass_proxy={} {} {}".format(
                    task_id,
                    provider or "unknown",
                    route_choice or "default",
                    "on" if verify_ssl else "off",
                    "on" if bypass_proxy else "off",
                    type(exc).__name__,
                    masked,
                )
            )

        if status_code == 401 or "密钥不可用" in msg:
            self._history.update_task(
                task_id,
                {
                    "status": "waiting_key",
                    "error": masked or "密钥不可用或已失效，请重新输入 Key",
                    "next_poll_at": 0,
                    "retry_count": 0,
                },
            )
            return

        retryable_http = {429, 500, 502, 503, 504}
        is_retryable = status_code in retryable_http if status_code is not None else False
        if isinstance(exc, (requests.exceptions.RequestException, ConnectionError)):
            is_retryable = True

        if is_retryable:
            retry_count = _safe_int(task.get("retry_count"), 0) + 1
            delay = _compute_backoff_seconds(retry_count)
            self._history.update_task(
                task_id,
                {
                    "status": "processing",
                    "error": masked or "轮询失败（将自动重试）",
                    "retry_count": retry_count,
                    "next_poll_at": now + int(delay),
                    "last_checked_at": int(now),
                },
            )
            return

        # 兜底：仍视为临时异常，避免误判失败导致结果丢失
        retry_count = _safe_int(task.get("retry_count"), 0) + 1
        delay = _compute_backoff_seconds(retry_count, base=3.0, cap=90.0)
        self._history.update_task(
            task_id,
            {
                "status": "processing",
                "error": masked or f"轮询异常：{type(exc).__name__}",
                "retry_count": retry_count,
                "next_poll_at": now + int(delay),
                "last_checked_at": int(now),
            },
        )

    # ── BananaV3 batch polling ─────────────────────────────────

    def _handle_banana_v3_success(self, task_id: str, task: Dict[str, Any],
                                  item: dict, client, now: int) -> None:
        """BananaV3 任务成功：提取图片、自动下载、更新历史。"""
        content_items, _ = client.extract_content(item)
        image_urls = [u for u in content_items if isinstance(u, str) and u.startswith("http")]
        logger.info(f"[banana_v3] 任务完成 {task_id}: {len(image_urls)} 张图片")

        fields: Dict[str, Any] = {
            "status": "success",
            "progress": 100,
            "image_urls": image_urls,
            "image_count": len(image_urls),
            "last_checked_at": now,
        }

        # 立即将 success 状态落盘，用户无需等待下载完成
        self._history.update_task(task_id, fields)

        # 后台异步下载，完成后补写 local_file/local_files
        if image_urls:
            verify_ssl, bypass_proxy = self._resolve_task_network_options(task)
            self._download_in_background(
                task_id, "banana_v3", image_urls,
                verify_ssl, bypass_proxy, is_batch=True,
            )

    def _poll_banana_v3_batch(self, tasks: List[Dict[str, Any]], now: int) -> None:
        """将多个到期的 BananaV3 任务合并为一次 batch-get 调用。

        按 (api_key, base_url) 分组，每组执行一次 batch_get_tasks()，
        再将响应中的 items 分发到各任务。
        """
        from workflow_parallel import compute_banana_v3_followup_delay
        from api_client import GeminiApiClient
        from config_manager import ConfigManager

        config_mgr = ConfigManager()
        client = GeminiApiClient(config_mgr, logger)

        # ── Step 1: 按 (api_key, base_url) 分组 ──
        groups: Dict[tuple, List[Dict[str, Any]]] = {}
        skipped: List[Dict[str, Any]] = []

        for task in tasks:
            task_id = _safe_str(task.get("id")).strip()
            provider = _safe_str(task.get("provider")).strip().lower()
            route_choice = _safe_str(task.get("route_choice")).strip()
            base_url = _safe_str(task.get("base_url")).strip()

            if not task_id or not base_url:
                skipped.append(task)
                continue

            api_key = self._keys.get_key(provider, route_choice)
            if not api_key:
                self._history.update_task(task_id, {
                    "status": "waiting_key",
                    "error": "等待用户在前端助手输入 Key",
                    "next_poll_at": 0,
                })
                continue

            group_key = (api_key, base_url)
            groups.setdefault(group_key, []).append(task)

        # ── Step 2: 每组一次 batch-get ──
        for (api_key, base_url), group_tasks in groups.items():
            task_ids = [_safe_str(t.get("id")).strip() for t in group_tasks]
            task_map = {_safe_str(t.get("id")).strip(): t for t in group_tasks}

            # verify_ssl: 组内有任何一个为 False 则整组 False
            verify_ssl = all(t.get("verify_ssl", True) for t in group_tasks)

            logger.info(f"[banana_v3] 批量轮询 {len(task_ids)} 个任务")

            try:
                response = client.batch_get_tasks(
                    api_key, task_ids,
                    async_base_url=base_url,
                    timeout=(15, 30),
                    verify_ssl=verify_ssl,
                )
            except Exception as exc:
                # batch 失败：对组内每个任务执行退避重试
                logger.warning(
                    f"[banana_v3] batch-get 失败 ({len(task_ids)} 个任务): "
                    f"{type(exc).__name__} {_mask_text(str(exc))}"
                )
                for task in group_tasks:
                    tid = _safe_str(task.get("id")).strip()
                    retry = _safe_int(task.get("retry_count"), 0) + 1
                    backoff = min(60, 15 * (2 ** min(retry, 4)))
                    self._history.update_task(tid, {
                        "retry_count": retry,
                        "last_checked_at": now,
                        "next_poll_at": now + backoff,
                    })
                continue

            # ── Step 3: 分发结果到各任务 ──
            items = response.get("items", [])
            next_poll_ms = response.get("next_poll_after_ms", 0)
            item_map: Dict[str, dict] = {}
            for item in items:
                iid = item.get("id")
                if iid:
                    item_map[iid] = item

            for task in group_tasks:
                tid = _safe_str(task.get("id")).strip()
                is_first = _safe_int(task.get("last_checked_at"), 0) <= 0
                item = item_map.get(tid)

                if not item:
                    # 服务端未返回该任务（仍在排队或已过期）
                    next_delay = compute_banana_v3_followup_delay(
                        is_first_followup=is_first,
                        suggested_seconds=next_poll_ms / 1000,
                    )
                    self._history.update_task(tid, {
                        "last_checked_at": now,
                        "next_poll_at": now + int(next_delay),
                    })
                    continue

                status = item.get("status", "")

                if status == "succeeded":
                    self._handle_banana_v3_success(tid, task, item, client, now)
                elif status in ("failed", "upstream_timeout", "not_found"):
                    error_detail = item.get("error") or item.get("message") or ""
                    if isinstance(error_detail, dict):
                        error_detail = error_detail.get("message") or str(error_detail)
                    fail_msg = f"{status}: {error_detail}" if error_detail else status
                    logger.warning(f"[banana_v3] 任务失败 {tid}: {fail_msg}")
                    self._history.update_task(tid, {
                        "status": "failed",
                        "error": fail_msg,
                        "last_checked_at": now,
                    })
                else:
                    # running/queued/accepted → 继续等待
                    next_delay = compute_banana_v3_followup_delay(
                        is_first_followup=is_first,
                        suggested_seconds=next_poll_ms / 1000,
                    )
                    self._history.update_task(tid, {
                        "status": "processing",
                        "last_checked_at": now,
                        "next_poll_at": now + int(next_delay),
                        "retry_count": 0,
                    })

        # 跳过的任务（缺少 id 或 base_url）降级为逐个处理
        for task in skipped:
            try:
                self._process_task(task, now)
            except Exception:
                pass

    def _poll_banana_v3_status(self, task, api_key, now):
        """banana_v3 异步生图任务的单任务轮询（batch 失败时的降级路径）。"""
        task_id = task["id"]
        base_url = task.get("base_url", "https://async.xinbao-ai.com")
        from workflow_parallel import compute_banana_v3_followup_delay

        from api_client import GeminiApiClient
        from config_manager import ConfigManager
        config_mgr = ConfigManager()
        client = GeminiApiClient(config_mgr, logger)
        is_first_followup = _safe_int(task.get("last_checked_at"), 0) <= 0

        try:
            response = client.batch_get_tasks(
                api_key, [task_id], async_base_url=base_url,
                timeout=(15, 30), verify_ssl=task.get("verify_ssl", True),
            )
        except Exception as exc:
            logger.warning(f"[banana_v3] 轮询失败 {task_id}: {exc}")
            retry = task.get("retry_count", 0) + 1
            backoff = min(60, 15 * (2 ** min(retry, 4)))
            self._history.update_task(task_id, {
                "retry_count": retry,
                "last_checked_at": now,
                "next_poll_at": now + backoff,
            })
            return

        items = response.get("items", [])
        if not items:
            next_delay = compute_banana_v3_followup_delay(
                is_first_followup=is_first_followup,
                suggested_seconds=response.get("next_poll_after_ms", 0) / 1000,
            )
            self._history.update_task(task_id, {
                "last_checked_at": now,
                "next_poll_at": now + int(next_delay),
            })
            return

        item = items[0]
        status = item.get("status", "")

        if status == "succeeded":
            self._handle_banana_v3_success(task_id, task, item, client, now)
        elif status in ("failed", "upstream_timeout", "not_found"):
            error_detail = item.get("error") or item.get("message") or ""
            if isinstance(error_detail, dict):
                error_detail = error_detail.get("message") or str(error_detail)
            fail_msg = f"{status}: {error_detail}" if error_detail else status
            logger.warning(f"[banana_v3] 任务失败 {task_id}: {fail_msg}")
            self._history.update_task(task_id, {
                "status": "failed",
                "error": fail_msg,
                "last_checked_at": now,
            })
        else:
            next_delay = compute_banana_v3_followup_delay(
                is_first_followup=is_first_followup,
                suggested_seconds=response.get("next_poll_after_ms", 0) / 1000,
            )
            self._history.update_task(task_id, {
                "status": "processing",
                "last_checked_at": now,
                "next_poll_at": now + int(next_delay),
                "retry_count": 0,
            })

    def _try_auto_download(
        self, task_id: str, provider: str, video_url: str, *, session: requests.Session
    ) -> Optional[Dict[str, str]]:
        output_dir = folder_paths.get_output_directory()
        subfolder = self._settings.download_subfolder()
        safe_id = _sanitize_filename_component(task_id)
        safe_provider = _sanitize_filename_component(provider)

        ext = self._guess_ext_from_url(video_url)
        filename = f"{safe_provider}_{safe_id}{ext}"

        target_dir = os.path.join(output_dir, subfolder)
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, filename)

        # 幂等检查：匹配同 stem 的任何扩展名（Content-Type 可能修正过扩展名）
        stem = f"{safe_provider}_{safe_id}"
        existing = glob.glob(os.path.join(target_dir, f"{stem}.*"))
        if existing:
            found_name = os.path.basename(existing[0])
            return {"filename": found_name, "subfolder": subfolder, "type": "output"}

        is_video = ext in _VIDEO_EXTENSIONS
        label = "视频" if is_video else "AI应用"

        if is_video:
            logger.info(f"{label}任务自动下载：{task_id} -> {subfolder}/{filename}")
            try:
                video_obj = download_video(
                    session=session,
                    url=video_url,
                    enable_parallel_range_download=True,
                )
                video_obj.save_to(target_path)
                logger.success(f"{label}任务自动下载完成：{subfolder}/{filename}")
                return {"filename": filename, "subfolder": subfolder, "type": "output"}
            except Exception as exc:
                logger.warning(f"{label}任务自动下载失败（将仅保留链接）：{type(exc).__name__} {_mask_text(str(exc))}")
                return None
        else:
            # 图片等非视频资源：简单 HTTP GET 保存
            logger.info(f"{label}任务自动下载：{task_id} -> {subfolder}/{filename}")
            try:
                resp = session.get(video_url, timeout=(15, 90))
                resp.raise_for_status()
                # 若服务端返回了 Content-Type，用它修正扩展名
                actual_ext = self._ext_from_content_type(resp.headers.get("Content-Type", ""))
                if actual_ext and actual_ext != ext:
                    filename = f"{safe_provider}_{safe_id}{actual_ext}"
                    target_path = os.path.join(target_dir, filename)
                with open(target_path, "wb") as f:
                    f.write(resp.content)
                logger.success(f"{label}任务自动下载完成：{subfolder}/{filename}")
                return {"filename": filename, "subfolder": subfolder, "type": "output"}
            except Exception as exc:
                logger.warning(f"{label}任务自动下载失败（将仅保留链接）：{type(exc).__name__} {_mask_text(str(exc))}")
                return None

    @staticmethod
    def _guess_ext_from_url(url: str) -> str:
        """从 URL 路径推断文件扩展名，无法判断时默认 .png。"""
        try:
            path = urlparse(url).path.lower()
            for ext in (".mp4", ".webm", ".mov", ".avi", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
                if path.endswith(ext):
                    return ext
        except Exception:
            pass
        return ".png"

    @staticmethod
    def _ext_from_content_type(content_type: str) -> Optional[str]:
        """从 Content-Type 头推断扩展名。"""
        ct = (content_type or "").lower().split(";")[0].strip()
        mapping = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
            "video/quicktime": ".mov",
        }
        return mapping.get(ct)


class VideoTaskManager:
    def __init__(self) -> None:
        self.history = VideoTaskHistoryStore()
        self.keys = VideoTaskKeyCache()
        self.settings = VideoTaskSettings()
        self.daemon = VideoTaskDaemon(self.history, self.keys, self.settings)

    def start(self) -> None:
        self.daemon.start()

    def record_task_created(
        self,
        *,
        task_id: str,
        provider: str,
        model: str = "",
        prompt: str = "",
        route_choice: str = "",
        base_url: str = "",
        api_key_source: str = "node_input",
        verify_ssl: Optional[bool] = None,
        bypass_proxy: Optional[bool] = None,
        daemon_poll: bool = True,
    ) -> None:
        tid = (task_id or "").strip()
        if not tid:
            return
        now = _now_ts()
        task: Dict[str, Any] = {
            "id": tid,
            "created_at": now,
            "provider": (provider or "").strip().lower(),
            "model": (model or "").strip(),
            "prompt": (prompt or "").strip()[:2000],
            "route_choice": (route_choice or "").strip(),
            "base_url": (base_url or "").strip(),
            "status": "pending",
            "api_key_source": (api_key_source or "").strip(),
            "retry_count": 0,
            "last_checked_at": 0,
            "next_poll_at": now,
            "error": "",
        }
        if isinstance(verify_ssl, bool) and not verify_ssl:
            task["verify_ssl"] = False
        if isinstance(bypass_proxy, bool) and bypass_proxy:
            task["bypass_proxy"] = True
        if not daemon_poll:
            # 协调器模式：延迟 daemon 轮询到协调器超时之后
            # 正常情况下协调器会在 ~420s 内回写结果（next_poll_at=0），daemon 永远不碰
            # 崩溃情况下 daemon 会在 ~8 分钟后恢复这些任务，不会永久卡住
            task["next_poll_at"] = now + 480
        elif provider == "banana_v3":
            # banana_v3 首次轮询延迟 10s，尽快发现快速失败
            task["next_poll_at"] = now + 10
        self.history.upsert_task(task)

    def record_task_success(self, task_id: str, video_url: str) -> None:
        tid = (task_id or "").strip()
        if not tid:
            return
        url = (video_url or "").strip()
        if not url:
            return
        self.history.update_task(
            tid,
            {
                "status": "success",
                "video_url": _clean_url(url),
                "progress": 100.0,
                "next_poll_at": 0,
                "retry_count": 0,
                "error": "",
                "last_checked_at": _now_ts(),
            },
        )

    def touch_poll_now(self, task_ids: List[str]) -> None:
        self.history.touch_poll_now(task_ids)

    def set_key(self, provider: str, api_key: str, route_choice: str = "", source: str = "ui") -> None:
        p = (provider or "").strip().lower()
        r = (route_choice or "").strip()
        k = (api_key or "").strip()
        if not p or not k:
            return
        self.keys.set_key(p, k, r)
        # 尝试唤醒 waiting_key 任务
        tasks = self.history.list_tasks()
        ids = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            if _safe_str(t.get("provider")).strip().lower() != p:
                continue
            if r and _safe_str(t.get("route_choice")).strip() != r:
                continue
            if _safe_str(t.get("status")).strip().lower() != "waiting_key":
                continue
            ids.append(_safe_str(t.get("id")).strip())
        for tid in ids:
            self.history.update_task(tid, {"status": "processing", "next_poll_at": _now_ts(), "api_key_source": source, "error": ""})

    def set_auto_download(self, enabled: bool) -> Dict[str, Any]:
        self.settings.set_auto_download(bool(enabled))
        return self.settings.snapshot()


VIDEO_TASK_MANAGER = VideoTaskManager()


# --- API Routes ---
_ROUTE_REGISTERED = False
_ROUTE_TIMER: threading.Timer | None = None


def _ensure_video_task_routes(prompt_server_provider) -> None:
    global _ROUTE_REGISTERED, _ROUTE_TIMER
    if _ROUTE_REGISTERED:
        return

    prompt_server = prompt_server_provider()
    if prompt_server is None:
        if _ROUTE_TIMER is None or not _ROUTE_TIMER.is_alive() or threading.current_thread() is _ROUTE_TIMER:
            timer = threading.Timer(1.0, lambda: _ensure_video_task_routes(prompt_server_provider))
            timer.daemon = True
            _ROUTE_TIMER = timer
            timer.start()
        return

    @prompt_server.routes.get("/banana/video_tasks")
    async def list_video_tasks_handler(request):
        tasks = VIDEO_TASK_MANAGER.history.list_tasks()
        # 兼容历史数据：部分上游在 completed 时不会上报 100%，导致 UI 长期停留在 89% 等数值。
        for t in tasks:
            if isinstance(t, dict) and _safe_str(t.get("status")).strip().lower() == "success":
                t["progress"] = 100.0
        return web.json_response({"success": True, "data": {"tasks": tasks, "settings": VIDEO_TASK_MANAGER.settings.snapshot()}})

    @prompt_server.routes.post("/banana/video_tasks/refresh")
    async def refresh_video_tasks_handler(request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        ids = payload.get("ids")
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list):
            single = payload.get("id")
            ids = [single] if isinstance(single, str) else []
        VIDEO_TASK_MANAGER.touch_poll_now([str(i) for i in ids if isinstance(i, str)])
        return web.json_response({"success": True})

    @prompt_server.routes.post("/banana/video_tasks/key")
    async def set_video_task_key_handler(request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        provider = str(payload.get("provider") or "").strip().lower()
        api_key = str(payload.get("api_key") or "").strip()
        route_choice = str(payload.get("route_choice") or "").strip()
        if not provider or not api_key:
            return web.json_response({"success": False, "message": "缺少 provider/api_key"}, status=400)
        VIDEO_TASK_MANAGER.set_key(provider, api_key, route_choice=route_choice, source="ui")
        return web.json_response({"success": True})

    @prompt_server.routes.post("/banana/video_tasks/settings")
    async def set_video_task_settings_handler(request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        auto_download = payload.get("auto_download")
        if not isinstance(auto_download, bool):
            auto_download = bool(int(auto_download)) if str(auto_download).isdigit() else False
        settings = VIDEO_TASK_MANAGER.set_auto_download(bool(auto_download))
        return web.json_response({"success": True, "data": settings})

    @prompt_server.routes.post("/banana/video_tasks/open_local")
    async def open_video_task_local_handler(request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        opened = False
        reason = ""

        if os.name != "nt":
            reason = "not_windows"
            return web.json_response({"success": True, "data": {"opened": opened, "reason": reason}})

        task_id = str(payload.get("id") or "").strip()
        local_file: Dict[str, Any] | None = None

        if task_id:
            try:
                for t in VIDEO_TASK_MANAGER.history.list_tasks():
                    if isinstance(t, dict) and _safe_str(t.get("id")).strip() == task_id:
                        lf = t.get("local_file")
                        if isinstance(lf, dict):
                            local_file = dict(lf)
                        break
            except Exception:
                local_file = None

        if local_file is None:
            lf = payload.get("local_file")
            if isinstance(lf, dict):
                local_file = dict(lf)

        if not isinstance(local_file, dict):
            reason = "missing_local_file"
            return web.json_response({"success": True, "data": {"opened": opened, "reason": reason}})

        file_path = _resolve_local_file_path(local_file)
        if not file_path:
            reason = "not_found"
            return web.json_response({"success": True, "data": {"opened": opened, "reason": reason}})

        opened = _open_in_windows_explorer_select(file_path)
        if not opened:
            reason = "explorer_failed"

        return web.json_response({"success": True, "data": {"opened": opened, "reason": reason}})

    @prompt_server.routes.post("/banana/video_tasks/delete")
    async def handle_delete(request):
        try:
            body = await request.json()
            task_ids = body.get("task_ids", [])
            if not task_ids:
                return web.json_response({"success": False, "message": "task_ids 不能为空"}, status=400)
            deleted = VIDEO_TASK_MANAGER.history.delete_tasks(task_ids)
            return web.json_response({"success": True, "data": {"deleted": deleted}})
        except Exception as exc:
            return web.json_response({"success": False, "message": str(exc)}, status=500)

    @prompt_server.routes.post("/banana/video_tasks/push_to_canvas")
    async def handle_push_to_canvas(request):
        try:
            body = await request.json()
            image_url = body.get("image_url", "").strip()
            if not image_url:
                return web.json_response({"success": False, "message": "image_url 不能为空"}, status=400)

            input_dir = folder_paths.get_input_directory()

            # 本地 /view URL：直接从输出目录复制到输入目录，跳过网络请求
            if image_url.startswith("/view"):
                params = parse_qs(urlparse(image_url).query)
                lf_dict = {
                    "filename": (params.get("filename") or [None])[0],
                    "subfolder": (params.get("subfolder") or [""])[0],
                    "type": (params.get("type") or ["output"])[0],
                }
                src_path = _local_file_to_abs_path(lf_dict)
                if src_path:
                    dest = os.path.join(input_dir, os.path.basename(src_path))
                    if not os.path.exists(dest):
                        shutil.copy2(src_path, dest)
                    return web.json_response({"success": True, "data": {"filename": os.path.basename(src_path)}})
                return web.json_response({"success": False, "message": "本地文件不存在"}, status=404)

            # 远程 URL：下载后保存到输入目录
            import requests as req_lib
            resp = req_lib.get(image_url, timeout=(15, 60), verify=False)
            resp.raise_for_status()
            image_bytes = resp.content

            import hashlib
            ct = resp.headers.get("content-type", "")
            ext = "jpg" if "jpeg" in ct else ("webp" if "webp" in ct else "png")
            md5_short = hashlib.md5(image_bytes).hexdigest()[:8]
            filename = f"banana_{md5_short}.{ext}"
            filepath = os.path.join(input_dir, filename)
            with open(filepath, "wb") as f:
                f.write(image_bytes)

            return web.json_response({"success": True, "data": {"filename": filename}})
        except Exception as exc:
            return web.json_response({"success": False, "message": str(exc)}, status=500)

    @prompt_server.routes.get("/banana/video_tasks/browse_dir")
    async def handle_browse_dir(request):
        try:
            path = request.query.get("path", "").strip()
            if not path:
                path = os.path.expanduser("~")
            if not os.path.isdir(path):
                return web.json_response({"success": False, "message": f"目录不存在: {path}"}, status=400)
            dirs = sorted([d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d)) and not d.startswith(".")])
            return web.json_response({"success": True, "data": {"current": path, "dirs": dirs, "parent": os.path.dirname(path)}})
        except PermissionError:
            return web.json_response({"success": False, "message": "权限不足"}, status=403)
        except Exception as exc:
            return web.json_response({"success": False, "message": str(exc)}, status=500)

    @prompt_server.routes.post("/banana/video_tasks/download")
    async def handle_batch_download(request):
        try:
            body = await request.json()
            task_ids = body.get("task_ids", [])
            save_dir = body.get("save_dir", "").strip()
            if not task_ids or not save_dir:
                return web.json_response({"success": False, "message": "task_ids 和 save_dir 不能为空"}, status=400)

            os.makedirs(save_dir, exist_ok=True)
            tasks = VIDEO_TASK_MANAGER.history.list_tasks()
            id_to_task = {t["id"]: t for t in tasks}

            import requests as req_lib
            downloaded = 0
            failed = 0

            for tid in task_ids:
                task = id_to_task.get(tid)
                if not task:
                    continue
                image_urls = task.get("image_urls", [])
                local_files = task.get("local_files", [])
                if not isinstance(local_files, list):
                    local_files = []
                for idx, url in enumerate(image_urls):
                    dest_path = os.path.join(save_dir, f"{tid}_{idx+1}.png")
                    try:
                        # 优先从已下载的本地文件复制（避免远程链接过期）
                        src = None
                        if idx < len(local_files) and isinstance(local_files[idx], dict):
                            src = _local_file_to_abs_path(local_files[idx])
                        if src:
                            shutil.copy2(src, dest_path)
                            downloaded += 1
                        else:
                            resp = req_lib.get(url, timeout=(15, 60), verify=False)
                            resp.raise_for_status()
                            with open(dest_path, "wb") as f:
                                f.write(resp.content)
                            downloaded += 1
                    except Exception:
                        failed += 1
                video_url = task.get("video_url", "")
                if video_url and not image_urls:
                    try:
                        resp = req_lib.get(video_url, timeout=(15, 120), verify=False)
                        resp.raise_for_status()
                        with open(os.path.join(save_dir, f"{tid}.mp4"), "wb") as f:
                            f.write(resp.content)
                        downloaded += 1
                    except Exception:
                        failed += 1

            return web.json_response({"success": True, "data": {"downloaded": downloaded, "failed": failed}})
        except Exception as exc:
            return web.json_response({"success": False, "message": str(exc)}, status=500)

    _ROUTE_REGISTERED = True


# Initialize daemon + routes
VIDEO_TASK_MANAGER.start()
_ensure_video_task_routes(lambda: getattr(PromptServer, "instance", None))
