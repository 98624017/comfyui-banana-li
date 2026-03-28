from __future__ import annotations

import asyncio
import os
import threading
import time
import traceback
from typing import Dict, List, Optional, Tuple

import requests
import torch

import comfy.model_management
import comfy.utils

from image_uploader import ImageUploadError, ImageUploader
from logger import logger
from config_manager import ConfigManager
from banana_kv_auth import EdgeKVAuthClient
from task_runner import BatchGenerationRunner
from workflow_parallel import (
    get_non_parallel_workflow_lock,
    get_workflow_parallel_shared_lock,
    make_execution_blocker,
    stagger_parallel_workflow_start,
)

from ..core.constants import MAX_IMAGE_BYTES, SORA_TOTAL_TIMEOUT
from ..core.deps import _CONFIG_MANAGER, _KV_AUTH_CLIENT
from ..core.download import download_video
from ..core.doubao_video_utils import (
    SUPPORTED_RATIOS,
    SUPPORTED_RESOLUTIONS,
    aggregate_batch_results,
    build_doubao_prompt,
    build_input_reference,
    is_undefined_task_id,
)
from ..core.image_utils import _compress_image, _tensor_to_rgb_image
from ..core.interrupt import _ensure_not_interrupted
from ..core.masking import _mask_text
from ..core.routing_auth import check_kv_auth, resolve_api_key_and_base
from ..core.video_task_manager import VIDEO_TASK_MANAGER
from ..core.video_types import BananaVideo
from ..providers.doubao_videos_api import DoubaoVideoClient


_DOUBAO_ROUTE_LABEL = "心宝❤新渠道"
_DOUBAO_MODELS = ["doubao-seedance-1-5-pro-251215"]


def _prepare_image_bytes(image_tensor: torch.Tensor) -> bytes:
    _ensure_not_interrupted()
    image = _tensor_to_rgb_image(image_tensor)
    compressed = _compress_image(image, max_bytes=MAX_IMAGE_BYTES)
    if len(compressed) > MAX_IMAGE_BYTES:
        logger.warning("压缩后仍超过 5MB，已使用最小质量版本")
    return compressed


def _upload_image_tensor_to_public_url(session: requests.Session, image_tensor: torch.Tensor) -> str:
    _ensure_not_interrupted()
    image_bytes = _prepare_image_bytes(image_tensor)
    uploader = ImageUploader(session=session, logger=logger, interrupt_checker=_ensure_not_interrupted)
    try:
        return uploader.upload(image_bytes)
    except ImageUploadError as exc:
        raise RuntimeError(str(exc)) from exc


def _extract_task_id(payload: Dict[str, object]) -> str:
    task_id = str(payload.get("id") or payload.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("豆包视频任务创建失败：未返回任务 ID")
    return task_id


def _extract_video_url(payload: Dict[str, object]) -> str:
    content = payload.get("content")
    if isinstance(content, dict):
        url = content.get("video_url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    raise RuntimeError("豆包视频任务已完成但未返回 content.video_url")


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


def _safe_progress_bar_update(progress_bar: Optional[object], value: int, total: int) -> None:
    if progress_bar is None:
        return
    try:
        progress_bar.update_absolute(int(value), int(total))
    except Exception:
        return


class XinbaoDoubaoVideoGenerator:
    RETURN_TYPES = ("VIDEO", "STRING", "VIDEO")
    RETURN_NAMES = ("video", "text", "videos")
    OUTPUT_IS_LIST = (False, False, True)
    FUNCTION = "generate_async"
    OUTPUT_NODE = True
    CATEGORY = "❤️‍🔥心宝专用/视频"

    # [兼容性] 接受旧工作流中残留的旧渠道名称
    _LEGACY_ROUTE_NAMES = frozenset({
        "测试高速渠道(key不通用)",
        "心宝❤测试新渠道（Key不通用）",
        "心宝❤测试新渠道(Key不通用)",
        "心宝测试新渠道",
    })

    @classmethod
    def VALIDATE_INPUTS(cls, 线路=None, **kwargs):
        if 线路 is not None and 线路 in cls._LEGACY_ROUTE_NAMES:
            return True
        return True

    def __init__(
        self,
        config_manager: Optional[ConfigManager] = None,
        kv_auth_client: Optional[EdgeKVAuthClient] = None,
    ) -> None:
        self.config_manager = config_manager or _CONFIG_MANAGER
        self.kv_auth_client = kv_auth_client or _KV_AUTH_CLIENT

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "让这段文字生成一段视频",
                        "tooltip": "视频生成提示词，支持中文；高级参数由节点选项自动追加到末尾",
                    },
                ),
                "banana_api_key": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": "调用服务的 API Key；留空则读取 config.ini（该渠道 Key 与旧渠道不通用，仅为提示，不影响机制）",
                    },
                ),
                "model": (
                    _DOUBAO_MODELS,
                    {
                        "default": _DOUBAO_MODELS[0],
                        "tooltip": "当前仅支持该模型（后续可能扩展列表）",
                    },
                ),
                "seconds": (
                    "INT",
                    {
                        "default": 12,
                        "min": 4,
                        "max": 12,
                        "tooltip": "视频时长（秒）：当前模型 4–12s",
                    },
                ),
                "batch_size": (
                    "INT",
                    {
                        "default": 1,
                        "min": 1,
                        "max": 8,
                        "tooltip": "一次生成的视频数量，最大并发8，每个请求间隔2秒启动",
                    },
                ),
            },
            "optional": {
                "ratio": (
                    ["(不指定)"] + SUPPORTED_RATIOS,
                    {
                        "default": "(不指定)",
                        "tooltip": "通过在 prompt 末尾追加 -ratio=... 控制比例",
                    },
                ),
                "分辨率": (
                    ["(不指定)"] + SUPPORTED_RESOLUTIONS,
                    {
                        "default": "720p",
                        "tooltip": "通过在 prompt 末尾追加 -resolution=... 控制分辨率（当前模型 480p/720p）",
                    },
                ),
                "生成音频": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "通过在 prompt 末尾追加 -generate_audio=true/false 控制是否生成音频",
                    },
                ),
                "固定镜头": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "通过在 prompt 末尾追加 -camera_fixed=true/false 控制相机视角是否固定",
                    },
                ),
                "启用工作流并发": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "启用后，多个 BananaV2/视频节点可在同一工作流中并发发起 API 调用；默认关闭以保持更保守的资源占用。",
                    },
                ),
                "仅提交不等待": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "仅提交任务（写入 history.toml）并立即返回，不等待轮询与下载；进度请在右下角“🎬 心宝视频任务”查看",
                    },
                ),
                "first_image": (
                    "IMAGE",
                    {
                        "tooltip": "可选参考图（首帧）。支持 0/1/2 图输入；将上传到公网 URL 后提交（推荐，解析更快）",
                    },
                ),
                "last_image": (
                    "IMAGE",
                    {
                        "tooltip": "可选参考图（尾帧）。若与首帧同时提供，将构造 input_reference 为 JSON 数组字符串（内容为两个 URL）",
                    },
                ),
                "绕过代理": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "梯子不稳定时可开启，强制直连",
                    },
                ),
                "禁用SSL验证": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "关闭证书校验（仅在信任网络环境时使用）",
                    },
                ),
                "线路": (
                    [_DOUBAO_ROUTE_LABEL],
                    {
                        "default": _DOUBAO_ROUTE_LABEL,
                        "tooltip": "该节点仅提供测试新渠道（Key 不通用仅为提示）",
                    },
                ),
            },
        }

    def _create_session(self, bypass_proxy: bool, verify_ssl: bool) -> requests.Session:
        session = requests.Session()
        if bypass_proxy:
            session.trust_env = False
            session.proxies = {}
        session.verify = verify_ssl
        return session

    def _resolve_api_key_and_base(self, raw_input_key: str, route_choice: str) -> Tuple[str, str, str]:
        return resolve_api_key_and_base(self.config_manager, raw_input_key, route_choice)

    async def generate_async(self, 启用工作流并发: bool = False, **kwargs):
        """
        ComfyUI 并发 API 节点入口（协程），行为对齐「心宝❤视频生成」：

        - 默认关闭并发：跨节点互斥（exclusive），更保守串行启动。
        - 启用并发：共享锁 + 启动错峰（削峰），并发模式下软失败输出 ExecutionBlocker，避免中断其它分支。
        """
        enable_parallel = bool(启用工作流并发)
        payload = dict(kwargs)
        payload.pop("启用工作流并发", None)

        async def _run_sync():
            return await asyncio.to_thread(self.generate, **payload)

        if not enable_parallel:
            await asyncio.sleep(0)
            async with get_non_parallel_workflow_lock():
                return await _run_sync()

        try:
            logger.info("已启用工作流并发：豆包视频节点将并发发起 API 调用")
            stagger_seconds = 0.35
            try:
                stagger_seconds = float(os.environ.get("BANANA_PARALLEL_NODE_STAGGER_SECONDS", "0.35") or 0.35)
            except Exception:
                stagger_seconds = 0.35
            stagger_seconds = max(0.0, min(stagger_seconds, 5.0))
            async with get_workflow_parallel_shared_lock():
                await stagger_parallel_workflow_start(stagger_seconds)
                return await _run_sync()
        except comfy.model_management.InterruptProcessingException:
            raise
        except Exception as exc:
            logger.error("豆包视频节点并发模式执行异常（已降级为阻断输出）:\n" + _mask_text(traceback.format_exc()))
            brief = str(exc or "").strip()
            if len(brief) > 300:
                brief = brief[:300] + "…"
            msg = f"豆包视频节点并发模式执行失败：{brief or '未知错误'}"
            return (make_execution_blocker(None), msg, [])

    def _apply_stagger_delay(self, batch_index: int, stagger_delay: float) -> None:
        """按批次索引增加交错延迟，减少瞬时请求尖峰"""
        if stagger_delay <= 0:
            return
        delay = batch_index * stagger_delay
        if delay > 0:
            time.sleep(delay)

    def _create_task_id(self, client: DoubaoVideoClient, create_payload: Dict[str, object]) -> str:
        """创建豆包视频任务并返回 task_id（包含 undefined 任务 ID 的一次性重试兜底）。"""
        created = client.create(dict(create_payload))
        task_id = _extract_task_id(created)
        if is_undefined_task_id(task_id):
            logger.warning("检测到服务端返回异常任务 ID（undefined...），将自动重试一次创建请求")
            created = client.create(dict(create_payload))
            task_id = _extract_task_id(created)
            if is_undefined_task_id(task_id):
                raise RuntimeError("服务端连续返回异常任务 ID（undefined...），已终止本次请求，请稍后重试")
        return task_id

    def _generate_single_video(
        self,
        session: requests.Session,
        api_key: str,
        base_url: str,
        create_payload: Dict[str, object],
        *,
        progress_cb: Optional[callable] = None,
    ) -> BananaVideo:
        _ensure_not_interrupted()
        client = DoubaoVideoClient(session=session, api_key=api_key, base_url=base_url)

        def _emit_progress(pct: float) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(float(pct))
            except Exception:
                return

        logger.info("开始创建豆包视频任务...")
        task_id = self._create_task_id(client, create_payload)

        logger.info(f"豆包视频任务已创建：{task_id}")
        VIDEO_TASK_MANAGER.record_task_created(
            task_id=task_id,
            provider="doubao",
            model=str(create_payload.get("model") or "").strip(),
            prompt=str(create_payload.get("prompt") or "").strip(),
            route_choice=_DOUBAO_ROUTE_LABEL,
            base_url=str(base_url or "").strip(),
            api_key_source="node_input",
            verify_ssl=bool(getattr(session, "verify", True)),
            bypass_proxy=not bool(getattr(session, "trust_env", True)),
        )

        start_time = time.time()
        last_log_time = start_time
        last_progress: Optional[float] = None
        consecutive_errors = 0

        while True:
            _ensure_not_interrupted()
            elapsed = time.time() - start_time
            if elapsed > SORA_TOTAL_TIMEOUT:
                raise RuntimeError(f"豆包视频生成超时（>{int(SORA_TOTAL_TIMEOUT)}s）：{task_id}")

            time.sleep(15.0)
            try:
                status_payload = client.status(task_id)
                consecutive_errors = 0  # 成功调用，重置计数
            except Exception as exc:
                # 上游接口可能偶发 502/503/504 网关错误，或发生 ReadTimeout 等网络波动，视为临时波动自动重试
                msg = str(exc)
                is_gateway_error = "HTTP 502" in msg or "HTTP 504" in msg or "HTTP 503" in msg
                is_network_error = isinstance(exc, (requests.exceptions.RequestException, ConnectionError))

                if is_gateway_error or is_network_error:
                    consecutive_errors += 1
                    if consecutive_errors < 3:
                        logger.warning(f"检测到轮询异常（{msg}），正在重试 ({consecutive_errors}/3)...")
                        time.sleep(2.0)
                        continue
                    # 连续失败次数耗尽，抛出异常
                    raise RuntimeError(f"轮询连续失败 3 次，已终止：{msg}") from exc
                raise

            status = str(status_payload.get("status") or "").strip().lower()
            progress = status_payload.get("progress")
            progress_num: Optional[float] = None
            if isinstance(progress, (int, float)):
                progress_num = float(progress)
            elif isinstance(progress, str):
                try:
                    progress_num = float(progress.strip())
                except Exception:
                    progress_num = None

            if status in ("queued", "pending"):
                if time.time() - last_log_time >= 15.0:
                    logger.info("豆包视频任务排队中...")
                    last_log_time = time.time()
                _emit_progress(1)
                continue

            if status in ("processing", "in_progress", "running"):
                if progress_num is not None:
                    clamped = float(min(max(progress_num, 0.0), 100.0))
                    # 进度变化 OR 距离上次打印超过 20秒（心跳日志，避免用户误以为卡死）
                    should_log = (
                        last_progress is None
                        or clamped != last_progress
                        or (time.time() - last_log_time >= 20.0)
                    )
                    if should_log:
                        logger.info(f"豆包视频生成中：{clamped:.1f}%")
                        last_progress = clamped
                        last_log_time = time.time()
                    _emit_progress(float(min(max(clamped, 1.0), 98.0)))
                else:
                    if time.time() - last_log_time >= 20.0:
                        waited = int(time.time() - start_time)
                        logger.info(f"豆包视频生成中...（已等待 {waited}s）")
                        last_log_time = time.time()
                    _emit_progress(50)
                continue

            if status == "completed":
                _emit_progress(98)
                video_url = _extract_video_url(status_payload)
                # 即使本地下载失败，仍应将视频链接写入任务中心，便于用户在 UI 中找回
                VIDEO_TASK_MANAGER.record_task_success(task_id, video_url)
                logger.info("豆包视频生成完成，准备下载视频...")
                video_obj = download_video(
                    session,
                    video_url,
                    progress_cb=_emit_progress,
                    progress_min=98,
                    progress_max=100,
                    enable_parallel_range_download=True,
                )
                _emit_progress(100)
                return video_obj

            if status in ("failed", "error", "canceled", "cancelled"):
                reason = _extract_error_message(status_payload)
                if reason:
                    raise RuntimeError(f"豆包视频生成失败：{_mask_text(reason)}")
                raise RuntimeError("豆包视频生成失败：未知原因")

            if time.time() - last_log_time >= 20.0:
                logger.info(f"豆包视频状态：{status or 'unknown'}")
                last_log_time = time.time()

    def generate(
        self,
        prompt: str,
        banana_api_key: str,
        model: str,
        seconds: int,
        batch_size: int,
        仅提交不等待: bool = False,
        ratio: str = "(不指定)",
        分辨率: str = "720p",
        生成音频: bool = True,
        固定镜头: bool = False,
        first_image: Optional[torch.Tensor] = None,
        last_image: Optional[torch.Tensor] = None,
        绕过代理: bool = False,
        禁用SSL验证: bool = False,
        线路: str = _DOUBAO_ROUTE_LABEL,
    ) -> Tuple[BananaVideo, str, List[BananaVideo]]:
        _ensure_not_interrupted()

        # [兼容性处理] 旧渠道名称自动映射到新渠道（静默兼容旧工作流）
        _legacy_route_names = {
            "测试高速渠道(key不通用)",
            "心宝❤测试新渠道（Key不通用）",
            "心宝❤测试新渠道(Key不通用)",
            "心宝测试新渠道",
        }
        if 线路 in _legacy_route_names:
            线路 = _DOUBAO_ROUTE_LABEL

        if 线路 != _DOUBAO_ROUTE_LABEL:
            raise RuntimeError("该节点仅支持“心宝❤新渠道”线路")

        actual_batch_size = 1
        if batch_size is not None:
            try:
                actual_batch_size = int(batch_size)
            except Exception:
                actual_batch_size = 1
        if actual_batch_size < 1:
            actual_batch_size = 1
        if actual_batch_size > 8:
            raise ValueError("批量上限为 8，请调整后重试")

        api_key, base_url, key_source = self._resolve_api_key_and_base(banana_api_key, 线路)
        verify_ssl = not bool(禁用SSL验证)

        logger.info(f"使用密钥来源：{key_source}")
        kv_result = check_kv_auth(
            self.kv_auth_client,
            api_key,
            线路,
            verify_ssl=verify_ssl,
            disable_ssl_override=bool(禁用SSL验证),
        )
        if not kv_result.allowed:
            raise RuntimeError(kv_result.user_message or "密钥不可用或已失效，请检查后再试")
        VIDEO_TASK_MANAGER.set_key("doubao", api_key, route_choice=线路, source=key_source)

        session = self._create_session(bypass_proxy=bool(绕过代理), verify_ssl=verify_ssl)

        try:
            ratio_value = None if ratio == "(不指定)" else ratio
            resolution_value = None if 分辨率 == "(不指定)" else 分辨率

            # 实测：图生视频时 -ratio 可能被服务端忽略，实际比例以首帧图为准（仅文生视频受 -ratio 控制）
            if ratio_value and (first_image is not None or last_image is not None):
                logger.warning("提示：检测到参考图输入，实测图生视频时 -ratio 参数可能无效，实际比例以首帧图为准；仅文生视频时 -ratio 生效")

            final_prompt = build_doubao_prompt(
                prompt,
                ratio=ratio_value,
                resolution=resolution_value,
                generate_audio=bool(生成音频),
                camera_fixed=bool(固定镜头),
            )

            first_ref = _upload_image_tensor_to_public_url(session, first_image) if first_image is not None else None
            last_ref = _upload_image_tensor_to_public_url(session, last_image) if last_image is not None else None
            input_reference = build_input_reference(first_ref, last_ref)

            seconds_value = int(seconds)
            if seconds_value < 4 or seconds_value > 12:
                raise ValueError("seconds 仅支持 4–12s")

            create_payload: Dict[str, object] = {
                "model": str(model).strip(),
                "prompt": final_prompt,
                "seconds": str(seconds_value),
            }
            if input_reference is not None:
                create_payload["input_reference"] = input_reference

            if actual_batch_size == 1:
                if bool(仅提交不等待):
                    client = DoubaoVideoClient(session=session, api_key=api_key, base_url=base_url)
                    task_id = self._create_task_id(client, create_payload)
                    VIDEO_TASK_MANAGER.record_task_created(
                        task_id=task_id,
                        provider="doubao",
                        model=str(create_payload.get("model") or "").strip(),
                        prompt=str(create_payload.get("prompt") or "").strip(),
                        route_choice=_DOUBAO_ROUTE_LABEL,
                        base_url=str(base_url or "").strip(),
                        api_key_source=str(key_source or "").strip() or "node_input",
                        verify_ssl=bool(getattr(session, "verify", True)),
                        bypass_proxy=not bool(getattr(session, "trust_env", True)),
                    )
                    msg = f"✅ 任务已提交至视频任务中心（ID: {task_id}），请在右下角“🎬 心宝视频任务”查看进度"
                    logger.info(msg)
                    return make_execution_blocker(None), msg, []

                progress_bar = comfy.utils.ProgressBar(100)
                _safe_progress_bar_update(progress_bar, 0, 100)
                video_obj = self._generate_single_video(
                    session,
                    api_key,
                    base_url,
                    create_payload,
                    progress_cb=lambda pct: _safe_progress_bar_update(progress_bar, int(pct), 100),
                )
                _safe_progress_bar_update(progress_bar, 100, 100)
                return video_obj, "✅ 豆包视频生成成功", [video_obj]

            stagger_delay = 2.0  # 每个请求间隔 2 秒启动
            progress_bar = comfy.utils.ProgressBar(actual_batch_size * 100)
            progress_state = [0 for _ in range(actual_batch_size)]
            progress_lock = threading.Lock()

            def _sync_progress(batch_idx: int, pct: float) -> None:
                if batch_idx < 0 or batch_idx >= actual_batch_size:
                    return
                with progress_lock:
                    clamped = max(0, min(100, int(pct)))
                    progress_state[batch_idx] = clamped
                    _safe_progress_bar_update(progress_bar, sum(progress_state), actual_batch_size * 100)

            tasks = []
            for i in range(actual_batch_size):
                tasks.append((i, session, api_key, base_url, create_payload, stagger_delay))

            def _worker(args_list: tuple) -> Dict[str, object]:
                batch_index = int(args_list[0])
                task_session = args_list[1]
                task_api_key = str(args_list[2])
                task_base_url = str(args_list[3])
                task_payload = dict(args_list[4])
                task_stagger_delay = float(args_list[5])

                try:
                    _ensure_not_interrupted()
                    self._apply_stagger_delay(batch_index, task_stagger_delay)
                    video_obj = self._generate_single_video(
                        task_session,
                        task_api_key,
                        task_base_url,
                        task_payload,
                        progress_cb=lambda pct: _sync_progress(batch_index, pct),
                    )
                    return {"success": True, "index": batch_index, "video_obj": video_obj, "text": "豆包视频生成成功"}
                except comfy.model_management.InterruptProcessingException:
                    raise
                except Exception as exc:
                    return {"success": False, "index": batch_index, "error": str(exc)}

            actual_workers = min(actual_batch_size, 8)
            task_runner = BatchGenerationRunner(
                logger,
                _ensure_not_interrupted,
                lambda total: progress_bar,
            )

            def progress_callback(result: Dict, completed_count: int, total_count: int, _progress_bar: object):
                if result.get("success"):
                    logger.success(f"[{completed_count}/{total_count}] 批次 {int(result['index']) + 1} 完成")
                else:
                    batch_label = result.get("index", -1)
                    batch_text = "?" if batch_label is None or int(batch_label) < 0 else int(batch_label) + 1
                    logger.error(f"[{completed_count}/{total_count}] 批次 {batch_text} 失败")

                idx = result.get("index", -1)
                try:
                    idx_int = int(idx)
                except Exception:
                    idx_int = -1
                if 0 <= idx_int < actual_batch_size:
                    _sync_progress(idx_int, 100)

            results = task_runner.run(
                tasks,
                _worker,
                actual_batch_size,
                actual_workers,
                True,  # continue_on_error
                progress_callback,
            )

            videos, combined_text = aggregate_batch_results(results, actual_batch_size)
            return videos[0], combined_text, videos

        except comfy.model_management.InterruptProcessingException:
            raise
        except Exception as exc:
            raise RuntimeError(f"豆包视频生成异常：{exc}") from exc
        finally:
            session.close()
