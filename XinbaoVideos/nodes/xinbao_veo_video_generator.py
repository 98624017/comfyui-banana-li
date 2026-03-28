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
from ..core.doubao_video_utils import aggregate_batch_results, is_undefined_task_id
from ..core.image_utils import _compress_image, _tensor_to_rgb_image
from ..core.interrupt import _ensure_not_interrupted
from ..core.masking import _mask_text
from ..core.routing_auth import check_kv_auth, resolve_api_key_and_base
from ..core.video_task_manager import VIDEO_TASK_MANAGER
from ..core.video_types import BananaVideo
from ..providers.veo_videos_api import VeoVideoClient


_VEO_ROUTE_LABEL = "心宝❤新渠道"
_VEO_MODELS = ["veo_3_1", "veo_3_1-fast"]
_VEO_ASPECT_RATIOS = ["9:16", "16:9"]


def _extract_task_id(payload: Dict[str, object]) -> str:
    task_id = str(payload.get("id") or payload.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("Veo 视频任务创建失败：未返回任务 ID")
    return task_id





def _extract_video_url_from_root(payload: Dict[str, object]) -> Optional[str]:
    url = payload.get("video_url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def _extract_video_url_from_content(payload: Dict[str, object]) -> Optional[str]:
    content = payload.get("content")
    if isinstance(content, dict):
        url = content.get("video_url") or content.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _extract_video_url_from_detail(payload: Dict[str, object], key: str) -> Optional[str]:
    detail = payload.get("detail")
    if isinstance(detail, dict):
        url = detail.get(key)
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _extract_main_video_url(payload: Dict[str, object], *, prefer_root: bool) -> str:
    """
    提取“主视频”下载链接。

    - prefer_root=True：优先 payload.video_url，其次 content.video_url；
    - prefer_root=False：优先 content.video_url，其次 payload.video_url；

    说明：不同上游可能把最终视频链接放在不同字段，这里做兼容兜底，
    避免出现 “status=completed 但 video_url 为空” 的假卡死/误判。
    """
    root_url = _extract_video_url_from_root(payload)
    content_url = _extract_video_url_from_content(payload)

    if prefer_root:
        if root_url:
            return root_url
        if content_url:
            return content_url
    else:
        if content_url:
            return content_url
        if root_url:
            return root_url

        # 非超分场景：允许兜底到 detail.video_url（部分上游在 detail 中返回主视频链接）
        detail_url = _extract_video_url_from_detail(payload, "video_url")
        if detail_url:
            return detail_url

    raise RuntimeError("Veo 视频任务已完成但未返回 video_url")


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


def _sleep_with_interrupt(seconds: float, *, check_interval_seconds: float = 0.5) -> None:
    if seconds <= 0:
        return
    deadline = time.monotonic() + float(seconds)
    while True:
        _ensure_not_interrupted()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(float(check_interval_seconds), remaining))


class XinbaoVeoVideoGenerator:
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
                        "default": "",
                        "tooltip": "视频生成提示词（必填）。",
                    },
                ),
                "banana_api_key": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": "调用服务的 API Key；留空则读取 config.ini/全局密钥（机制与其它心宝节点一致）。",
                    },
                ),
                "model": (
                    _VEO_MODELS,
                    {
                        "default": _VEO_MODELS[0],
                        "tooltip": "Veo 模型选择。",
                    },
                ),
                "aspect_ratio": (
                    _VEO_ASPECT_RATIOS,
                    {
                        "default": _VEO_ASPECT_RATIOS[0],
                        "tooltip": "画面比例（默认 9:16）。",
                    },
                ),
                "batch_size": (
                    "INT",
                    {
                        "default": 1,
                        "min": 1,
                        "max": 8,
                        "tooltip": "一次生成的视频数量，最大并发8，每个请求间隔2秒启动。",
                    },
                ),
            },
            "optional": {
                "image_1": (
                    "IMAGE",
                    {
                        "tooltip": "可选参考图 1（支持 0~3 图；将按输入端口顺序上传到公网 URL 并提交）。\n提示：传入2张图为首尾帧图生视频模式，传入3张图片为元素参考生成视频模式",
                    },
                ),
                "image_2": (
                    "IMAGE",
                    {
                        "tooltip": "可选参考图 2（顺序与端口一致）。\n提示：传入2张图为首尾帧图生视频模式，传入3张图片为元素参考生成视频模式",
                    },
                ),
                "image_3": (
                    "IMAGE",
                    {
                        "tooltip": "可选参考图 3（顺序与端口一致）。\n提示：传入2张图为首尾帧图生视频模式，传入3张图片为元素参考生成视频模式",
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
                "绕过代理": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "梯子不稳定时可开启，强制直连。",
                    },
                ),
                "禁用SSL验证": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "关闭证书校验（仅在信任网络环境时使用）。",
                    },
                ),
                "线路": (
                    [_VEO_ROUTE_LABEL],
                    {
                        "default": _VEO_ROUTE_LABEL,
                        "tooltip": "该节点仅提供测试新渠道（Key 不通用仅为提示）。",
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
        ComfyUI 并发 API 节点入口（协程），行为对齐「心宝视频生成-即梦豆包系」：

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
            logger.info("已启用工作流并发：Veo 视频节点将并发发起 API 调用")
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
            logger.error("Veo 视频节点并发模式执行异常（已降级为阻断输出）:\n" + _mask_text(traceback.format_exc()))
            brief = str(exc or "").strip()
            if len(brief) > 300:
                brief = brief[:300] + "…"
            msg = f"Veo 视频节点并发模式执行失败：{brief or '未知错误'}"
            return (make_execution_blocker(None), msg, [])

    def _apply_stagger_delay(self, batch_index: int, stagger_delay: float) -> None:
        """按批次索引增加交错延迟，减少瞬时请求尖峰"""
        if stagger_delay <= 0:
            return
        delay = batch_index * stagger_delay
        if delay > 0:
            time.sleep(delay)

    def _prepare_image_bytes(self, image: torch.Tensor) -> bytes:
        _ensure_not_interrupted()
        rgb = _tensor_to_rgb_image(image)
        compressed = _compress_image(rgb, MAX_IMAGE_BYTES)
        if len(compressed) > MAX_IMAGE_BYTES:
            logger.warning("压缩后仍超过 5MB，已使用最小质量版本")
        return compressed

    def _upload_image_to_public_url(self, session: requests.Session, image: torch.Tensor) -> str:
        _ensure_not_interrupted()
        image_bytes = self._prepare_image_bytes(image)
        uploader = ImageUploader(session=session, logger=logger, interrupt_checker=_ensure_not_interrupted)
        try:
            return uploader.upload(image_bytes)
        except ImageUploadError as exc:
            raise RuntimeError(str(exc)) from exc

    def _create_task_id(self, client: VeoVideoClient, create_payload: Dict[str, object]) -> str:
        """创建 Veo 视频任务并返回 task_id（包含 undefined 任务 ID 的一次性重试兜底）。"""
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
        prefer_root_video_url: bool,
        progress_cb: Optional[callable] = None,
    ) -> BananaVideo:
        _ensure_not_interrupted()
        client = VeoVideoClient(session=session, api_key=api_key, base_url=base_url)

        def _emit_progress(pct: float) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(float(pct))
            except Exception:
                return

        logger.info("开始创建 Veo 视频任务...")
        task_id = self._create_task_id(client, create_payload)

        logger.info(f"Veo 视频任务已创建：{task_id}")
        VIDEO_TASK_MANAGER.record_task_created(
            task_id=task_id,
            provider="veo",
            model=str(create_payload.get("model") or "").strip(),
            prompt=str(create_payload.get("prompt") or "").strip(),
            route_choice=_VEO_ROUTE_LABEL,
            base_url=str(base_url or "").strip(),
            api_key_source="node_input",
            verify_ssl=bool(getattr(session, "verify", True)),
            bypass_proxy=not bool(getattr(session, "trust_env", True)),
        )

        start_time = time.time()
        last_log_time = start_time
        last_progress: Optional[float] = None
        consecutive_errors = 0
        poll_interval_seconds = 15.0
        retry_sleep_seconds = 2.0
        completed_url_grace_seconds = 60.0
        sleep_seconds = 0.0
        completed_url_wait_start: Optional[float] = None

        while True:
            _ensure_not_interrupted()
            elapsed = time.time() - start_time
            if elapsed > SORA_TOTAL_TIMEOUT:
                raise RuntimeError(f"Veo 视频生成超时（>{int(SORA_TOTAL_TIMEOUT)}s）：{task_id}")

            _sleep_with_interrupt(sleep_seconds)
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
                        sleep_seconds = retry_sleep_seconds
                        continue
                    # 连续失败次数耗尽，抛出异常
                    raise RuntimeError(f"轮询连续失败 3 次，已终止：{msg}") from exc
                raise

            sleep_seconds = poll_interval_seconds

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

            if status != "completed":
                completed_url_wait_start = None

            if status in ("queued", "pending"):
                if time.time() - last_log_time >= poll_interval_seconds:
                    logger.info("Veo 视频任务排队中...")
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
                        logger.info(f"Veo 视频生成中：{clamped:.1f}%")
                        last_progress = clamped
                        last_log_time = time.time()
                    _emit_progress(float(min(max(clamped, 1.0), 95.0)))
                else:
                    if time.time() - last_log_time >= 20.0:
                        waited = int(time.time() - start_time)
                        logger.info(f"Veo 视频生成中...（已等待 {waited}s）")
                        last_log_time = time.time()
                    _emit_progress(50)
                continue

            if status == "completed":
                _emit_progress(95)

                try:
                    main_url = _extract_main_video_url(status_payload, prefer_root=bool(prefer_root_video_url))
                except RuntimeError as exc:
                    now = time.time()
                    if completed_url_wait_start is None:
                        completed_url_wait_start = now
                    waited = now - completed_url_wait_start
                    if waited <= completed_url_grace_seconds:
                        if now - last_log_time >= 5.0:
                            logger.info(
                                f"Veo 已返回 completed 但 video_url 尚未就绪（已等待 {int(waited)}s），继续轮询：{task_id}"
                            )
                            last_log_time = now
                        sleep_seconds = retry_sleep_seconds
                        continue
                    raise RuntimeError(
                        f"Veo 视频任务已完成但 video_url 仍未就绪（等待 {int(waited)}s）：{task_id}"
                    ) from exc

                logger.info("Veo 视频生成完成，准备下载主视频...")
                # 即使本地下载失败，仍应将视频链接写入任务中心，便于用户在 UI 中找回
                VIDEO_TASK_MANAGER.record_task_success(task_id, main_url)
                video_obj = download_video(
                    session,
                    main_url,
                    progress_cb=_emit_progress,
                    progress_min=95,
                    progress_max=100,
                    enable_parallel_range_download=True,
                    parallel_range_min_size_bytes=1024 * 1024,
                    parallel_range_workers=8,
                )
                _emit_progress(100)
                return video_obj

            if status in ("failed", "error", "canceled", "cancelled"):
                reason = _extract_error_message(status_payload)
                if reason:
                    raise RuntimeError(f"Veo 视频生成失败：{_mask_text(reason)}")
                raise RuntimeError("Veo 视频生成失败：未知原因")

            if time.time() - last_log_time >= 20.0:
                logger.info(f"Veo 视频状态：{status or 'unknown'}")
                last_log_time = time.time()

    def generate(
        self,
        prompt: str,
        banana_api_key: str,
        model: str,
        aspect_ratio: str,
        batch_size: int,
        仅提交不等待: bool = False,
        image_1: Optional[torch.Tensor] = None,
        image_2: Optional[torch.Tensor] = None,
        image_3: Optional[torch.Tensor] = None,
        绕过代理: bool = False,
        禁用SSL验证: bool = False,
        线路: str = _VEO_ROUTE_LABEL,
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
            线路 = _VEO_ROUTE_LABEL

        if 线路 != _VEO_ROUTE_LABEL:
            raise RuntimeError("该节点仅支持“心宝❤新渠道”线路")

        cleaned_prompt = (prompt or "").strip()
        if not cleaned_prompt:
            raise RuntimeError("prompt 为必填参数，请填写提示词后重试")

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

        model_value = str(model or "").strip()
        if not model_value:
            raise RuntimeError("model 不能为空")

        aspect_value = str(aspect_ratio or "").strip()
        if aspect_value not in _VEO_ASPECT_RATIOS:
            raise ValueError(f"不支持的 aspect_ratio：{aspect_value}")

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
        VIDEO_TASK_MANAGER.set_key("veo", api_key, route_choice=线路, source=key_source)

        session = self._create_session(bypass_proxy=bool(绕过代理), verify_ssl=verify_ssl)

        try:
            image_urls: List[str] = []
            for image in (image_1, image_2, image_3):
                if image is None:
                    continue
                image_urls.append(self._upload_image_to_public_url(session, image))

            create_payload: Dict[str, object] = {
                "prompt": cleaned_prompt,
                "model": model_value,
                "aspect_ratio": aspect_value,
            }
            if image_urls:
                create_payload["images"] = image_urls

            if actual_batch_size == 1:
                if bool(仅提交不等待):
                    client = VeoVideoClient(session=session, api_key=api_key, base_url=base_url)
                    task_id = self._create_task_id(client, create_payload)
                    VIDEO_TASK_MANAGER.record_task_created(
                        task_id=task_id,
                        provider="veo",
                        model=str(create_payload.get("model") or "").strip(),
                        prompt=str(create_payload.get("prompt") or "").strip(),
                        route_choice=_VEO_ROUTE_LABEL,
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
                    prefer_root_video_url=False,
                    progress_cb=lambda pct: _safe_progress_bar_update(progress_bar, int(pct), 100),
                )
                _safe_progress_bar_update(progress_bar, 100, 100)

                text = "✅ Veo 视频生成成功"
                return video_obj, text, [video_obj]

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
                        prefer_root_video_url=False,
                        progress_cb=lambda pct: _sync_progress(batch_index, pct),
                    )
                    return {
                        "success": True,
                        "index": batch_index,
                        "video_obj": video_obj,
                        "text": "Veo 视频生成成功",
                    }
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
            raise RuntimeError(f"Veo 视频生成异常：{exc}") from exc
        finally:
            session.close()
