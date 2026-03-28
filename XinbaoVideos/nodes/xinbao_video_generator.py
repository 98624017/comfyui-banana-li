from __future__ import annotations

import asyncio
import io
import json
import os
import random
import re
import threading
import time
import traceback
import uuid
from typing import Dict, List, Optional, Tuple

import requests
import torch
from PIL import Image

import comfy.model_management
import comfy.utils

import folder_paths
from logger import logger
from config_manager import ConfigManager
from banana_kv_auth import EdgeKVAuthClient
from image_uploader import ImageUploader, ImageUploadError
from task_runner import BatchGenerationRunner
from workflow_parallel import (
    get_non_parallel_workflow_lock,
    get_workflow_parallel_shared_lock,
    make_execution_blocker,
    stagger_parallel_workflow_start,
)

from ..core.constants import (
    CHARACTER_CREATE_PROMPT,
    CONNECT_TIMEOUT,
    HANDSHAKE_RETRIES,
    MAX_IMAGE_BYTES,
    READ_TIMEOUT,
    READ_TIMEOUT_PRO,
    SORA_PRO_TOTAL_TIMEOUT,
    SORA_TOTAL_TIMEOUT,
    UPLOAD_RETRIES,
    UPLOAD_TIMEOUT,
)
from ..core.deps import _CONFIG_MANAGER, _KV_AUTH_CLIENT
from ..core.download import download_video
from ..core.image_utils import _compress_image, _tensor_to_rgb_image
from ..core.interrupt import _ensure_not_interrupted
from ..core.masking import _clean_url, _extract_error_from_html, _mask_key, _mask_text, _mask_url
from ..core.model_mapping import _build_model_id, _build_sora_pro_create_payload
from ..core.routing_auth import check_kv_auth, display_route_label, resolve_api_key_and_base
from ..core.video_task_manager import VIDEO_TASK_MANAGER
from ..core.video_types import BananaVideo
from ..providers.sora_videos_api import SoraVideoClient


def _safe_progress_bar_update(progress_bar: Optional[object], value: int, total: int) -> None:
    """
    安全更新 ComfyUI 进度条。

    说明：进度条属于 UI 辅助信息，开启工作流并发（后台线程执行）后，部分环境里
    进度条实现可能不具备线程安全保证。这里将异常吞掉，避免因 UI 更新失败中断轮询。
    """
    if progress_bar is None:
        return
    try:
        progress_bar.update_absolute(int(value), int(total))
    except Exception:
        return


def _try_extract_http_status_code(message: str) -> Optional[int]:
    if not message:
        return None
    match = re.search(r"HTTP\s+(\d{3})", message)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _append_unique_suffix(prompt: str) -> str:
    if not prompt:
        return prompt
    unique_id = f"{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    return f"{prompt} --unique={unique_id}"


def _extract_url(text: str) -> Optional[str]:
    if not text:
        return None
    match = re.search(r"https?://[^\s)>\]]+", text)
    if not match:
        return None
    return _clean_url(match.group(0))


def _log_progress_from_text(text: str, progress_bar: Optional[object] = None) -> None:
    if not text:
        return
    masked_text = _mask_text(text)
    if "队列" in text:
        logger.info(masked_text.strip())
    percentages = re.findall(r"(\d+(?:\.\d+)?)%", text)
    logged_raw = False
    for pct in percentages:
        try:
            value = float(pct)
            if not logged_raw and ("进度" in text or "🏃" in text):
                logger.info(masked_text.strip())
                logged_raw = True
            logger.info(f"当前进度：{value:.1f}%")
            if progress_bar is not None:
                _safe_progress_bar_update(progress_bar, min(int(value), 100), 100)
        except Exception:
            continue


class XinbaoVideoGenerator:
    RETURN_TYPES = ("VIDEO", "STRING", "VIDEO")
    RETURN_NAMES = ("video", "text", "videos")
    OUTPUT_IS_LIST = (False, False, True)
    FUNCTION = "generate_async"
    OUTPUT_NODE = True
    CATEGORY = "❤️‍🔥心宝专用/视频"
    _ERR_AUTH = "密钥不可用或已失效，请检查后再试"

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
                        "tooltip": "视频生成的提示词，支持中文",
                    },
                ),
                "banana_api_key": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": "调用服务的 API Key；留空则读取 config.ini",
                    },
                ),
                "model_type": (
                    ["sora-2-10s", "sora-2-15s", "sora-2-pro-10s", "sora-2-pro-15s", "sora-2-pro-25s"],
                    {
                        "default": "sora-2-10s",
                        "tooltip": "选择视频生成模型（sora-2-10s 为旧值兼容；sora-2-pro-* 通过 seconds/size 控制）",
                    },
                ),
                "aspect_ratio": (
                    ["横版", "竖版"],
                    {
                        "default": "横版",
                        "tooltip": "横板=landscape (16:9)，竖版=portrait (9:16)",
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
                "启用工作流并发": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "启用后，多个 BananaV2/视频节点可在同一工作流中并发发起 API 调用；默认关闭以保持更保守的资源占用。开启并发时单节点失败不会终止整图。",
                    },
                ),
                "仅提交不等待": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "仅提交任务（写入 history.toml）并立即返回，不等待轮询与下载；进度请在右下角“🎬 心宝视频任务”查看（仅支持 Sora，Veo 请使用 Veo 专用节点）",
                    },
                ),
                "seed": (
                    "INT",
                    {
                        "default": -1,
                        "min": -1,
                        "max": 2_147_483_647,
                        "tooltip": "随机种子，-1 为自动随机；接口当前可能忽略该字段",
                    },
                ),
                "image": (
                    "IMAGE",
                    {
                        "tooltip": "可选的参考图；>5MB 自动压缩；Sora 会上传图床并以 JSON 的 image=url 方式提交；Veo 会上传图床",
                    },
                ),
                "流式模式": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "仅 veo3.1 生效：开启 SSE 流式返回（带进度，默认）；Sora 分支固定轮询 status/progress",
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
                    ConfigManager.VALID_ROUTE_LABELS + ["心宝❤新渠道"],
                    {
                        "default": "心宝❤新渠道",
                        "tooltip": "线路选择，fixsk- 密钥会自动使用隐藏线路",
                    },
                ),
                "启用角色": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "仅 Sora 生效：单批次优先按文档使用 video+prompt 单次请求创建角色并生成；批量模式为避免重复创建，将先创建角色拿到 @username 再复用生成",
                    },
                ),
                "仅创建角色": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "仅 Sora 生效：只上传角色参考视频创建角色并返回 @username（不生成新视频）；视频输出将原样透传角色参考视频",
                    },
                ),
                "保存角色到提示词助手": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "仅 Sora 生效：当角色创建成功返回 @username 时，将其作为提示词片段写入 snippets.toml（category=Sora，color=#FF9800）；默认关闭",
                    },
                ),
                "角色参考视频": (
                    "VIDEO",
                    {
                        "tooltip": "仅 Sora 生效：角色参考视频（建议 3-10 秒，清晰正面更佳）",
                    },
                ),
            },
        }

    async def generate_async(self, 启用工作流并发: bool = False, **kwargs):
        """
        ComfyUI 并发 API 节点入口（协程）。

        - 通过 `await asyncio.to_thread(...)` 让出事件循环，触发 ComfyUI pending_async_nodes 并发调度。
        - 默认关闭并发时，使用跨节点互斥锁使同类节点更保守（串行启动）。
        - 开启并发时，捕获异常并输出空结果（video=None / videos=[]），避免单节点失败阻断其它分支。
        """
        enable_parallel = bool(启用工作流并发)
        payload = dict(kwargs)
        payload.pop("启用工作流并发", None)

        async def _run_sync():
            return await asyncio.to_thread(self.generate, **payload)

        if not enable_parallel:
            # 让出一次事件循环 tick，尽量让开启并发的节点先启动（语义 A：先开始）。
            await asyncio.sleep(0)
            async with get_non_parallel_workflow_lock():
                return await _run_sync()

        try:
            logger.info("已启用工作流并发：视频节点将并发发起 API 调用")
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
            logger.error("视频节点并发模式执行异常（已降级为阻断输出）:\n" + _mask_text(traceback.format_exc()))
            brief = str(exc or "").strip()
            if len(brief) > 300:
                brief = brief[:300] + "…"
            # 并发模式下“软失败”：
            # - 不抛异常：避免一个节点失败中断整个工作流（否则可能导致其它分支已发起付费调用但本地中途终止轮询/保存）
            # - 返回“静默 ExecutionBlocker”：阻断需要 VIDEO 输入的下游节点（如 SaveVideo），同时避免触发 ExecutionBlocked 错误事件
            #   这样其它分支的 SaveVideo 仍能正常完成并在 UI 中展示输出。
            msg = f"视频节点并发模式执行失败：{brief or '未知错误'}"
            return (make_execution_blocker(None), msg, [])

    def _prepare_image_bytes(self, image: torch.Tensor) -> Tuple[bytes, Image.Image]:
        _ensure_not_interrupted()
        rgb = _tensor_to_rgb_image(image)
        compressed = _compress_image(rgb, MAX_IMAGE_BYTES)
        if len(compressed) > MAX_IMAGE_BYTES:
            logger.warning("压缩后仍超过 5MB，已使用最小质量版本")
        logger.info(f"参考图大小：{len(compressed) / 1024:.1f} KB")
        return compressed, rgb

    def _upload_image_to_public_url(
        self,
        session: requests.Session,
        image_bytes: bytes,
    ) -> str:
        """使用 ImageUploader 上传图片到公网图床，返回 URL（带缓存）"""
        _ensure_not_interrupted()
        uploader = ImageUploader(session=session, logger=logger, interrupt_checker=_ensure_not_interrupted)
        try:
            url = uploader.upload(image_bytes)
            return url
        except ImageUploadError as exc:
            raise RuntimeError(str(exc)) from exc

    def _prepare_video_bytes(self, video_obj: object) -> bytes:
        """
        将 ComfyUI VIDEO 输入转换为 bytes。

        兼容常见形态：BananaVideo、带 data(BytesIO/bytes) 属性的对象、dict("data")、或本地文件路径。
        """
        _ensure_not_interrupted()
        if video_obj is None:
            raise RuntimeError("未提供角色参考视频（VIDEO）")

        # 支持 ComfyUI 原生 VideoFromFile 对象（comfy_api.latest._input_impl.video_types.VideoFromFile）
        # 该对象将文件路径存储在私有属性 _VideoFromFile__file 中
        video_file_path = getattr(video_obj, '_VideoFromFile__file', None)
        if video_file_path and isinstance(video_file_path, str) and os.path.isfile(video_file_path):
            logger.info(f"检测到原生 VideoFromFile，读取文件: {os.path.basename(video_file_path)}")
            with open(video_file_path, "rb") as handle:
                return handle.read()

        if isinstance(video_obj, (bytes, bytearray)):
            return bytes(video_obj)

        if isinstance(video_obj, dict) and "data" in video_obj:
            candidate = video_obj.get("data")
            if isinstance(candidate, io.BytesIO):
                original_pos = 0
                try:
                    original_pos = candidate.tell()
                except Exception:
                    original_pos = 0
                candidate.seek(0)
                data = candidate.read()
                try:
                    candidate.seek(original_pos)
                except Exception:
                    pass
                return data
            if isinstance(candidate, (bytes, bytearray)):
                return bytes(candidate)

        candidate_data = getattr(video_obj, "data", None)
        if isinstance(candidate_data, io.BytesIO):
            original_pos = 0
            try:
                original_pos = candidate_data.tell()
            except Exception:
                original_pos = 0
            candidate_data.seek(0)
            data = candidate_data.read()
            try:
                candidate_data.seek(original_pos)
            except Exception:
                pass
            return data
        if isinstance(candidate_data, (bytes, bytearray)):
            return bytes(candidate_data)

        if isinstance(video_obj, torch.Tensor):
            raise RuntimeError("检测到图像（IMAGE/Tensor）输入：请连接 VIDEO 端口或使用视频加载节点。")

        def _resolve_local_path(p: str) -> Optional[str]:
            if not p or not isinstance(p, str):
                return None
            if os.path.isfile(p):
                return p
            try:
                # 尝试通过 ComfyUI 路径管理器解析（处理相对路径/输入目录文件）
                res = folder_paths.get_annotated_filepath(p)
                if res and isinstance(res, (tuple, list)) and res[0] and os.path.isfile(str(res[0])):
                    return str(res[0])
            except Exception:
                pass
            return None

        if isinstance(video_obj, (tuple, list)):
            for item in video_obj:
                try:
                    return self._prepare_video_bytes(item)
                except Exception:
                    continue

        if isinstance(video_obj, dict):
            for key in ("path", "file", "filepath", "filename", "source", "video"):
                candidate_path = _resolve_local_path(video_obj.get(key))
                if candidate_path:
                    with open(candidate_path, "rb") as handle:
                        return handle.read()

        for attr in ("path", "file", "filepath", "filename", "source", "video"):
            candidate_path = _resolve_local_path(getattr(video_obj, attr, None))
            if candidate_path:
                with open(candidate_path, "rb") as handle:
                    return handle.read()

        if isinstance(video_obj, str):
            candidate_path = _resolve_local_path(video_obj)
            if candidate_path:
                with open(candidate_path, "rb") as handle:
                    return handle.read()

        raise RuntimeError("角色参考视频格式不支持：请连接 VIDEO 输出或提供有效的视频对象")

    def _infer_video_upload_info(
        self,
        video_obj: object,
        default_filename: str = "character.mp4",
        default_content_type: str = "video/mp4",
    ) -> Tuple[str, str]:
        def _resolve_local_path(p: str) -> Optional[str]:
            if not p or not isinstance(p, str):
                return None
            if os.path.isfile(p):
                return p
            try:
                res = folder_paths.get_annotated_filepath(p)
                if res and isinstance(res, (tuple, list)) and res[0] and os.path.isfile(str(res[0])):
                    return str(res[0])
            except Exception:
                pass
            return None

        if isinstance(video_obj, (tuple, list)):
            for item in video_obj:
                try:
                    return self._infer_video_upload_info(item, default_filename, default_content_type)
                except Exception:
                    continue

        candidate_path: Optional[str] = None
        if isinstance(video_obj, str):
            candidate_path = _resolve_local_path(video_obj.strip())
        elif isinstance(video_obj, dict):
            for key in ("path", "file", "filepath", "filename", "source", "video"):
                value = video_obj.get(key)
                if isinstance(value, str):
                    path = _resolve_local_path(value.strip())
                    if path:
                        candidate_path = path
                        break
        else:
            for attr in ("path", "file", "filepath", "filename", "source", "video"):
                value = getattr(video_obj, attr, None)
                if isinstance(value, str):
                    path = _resolve_local_path(value.strip())
                    if path:
                        candidate_path = path
                        break

        filename = (default_filename or "character.mp4").strip() or "character.mp4"
        if candidate_path:
            basename = os.path.basename(candidate_path)
            if basename:
                filename = basename

        ext = os.path.splitext(filename)[1].lower()
        content_type_by_ext = {
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".avi": "video/x-msvideo",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
        }
        content_type = content_type_by_ext.get(ext) or (default_content_type or "application/octet-stream")
        return filename, content_type

    def _extract_sora_character_fields(self, payload: Dict[str, object]) -> Tuple[Optional[str], Optional[str]]:
        username: Optional[str] = None
        display_name: Optional[str] = None

        if not isinstance(payload, dict):
            return None, None

        character_obj = payload.get("character")
        if isinstance(character_obj, dict):
            nested_username = character_obj.get("username")
            if isinstance(nested_username, str) and nested_username.strip():
                username = nested_username.strip()
            nested_display = character_obj.get("display_name")
            if isinstance(nested_display, str) and nested_display.strip():
                display_name = nested_display.strip()

        if not username:
            top_username = payload.get("username")
            if isinstance(top_username, str) and top_username.strip():
                username = top_username.strip()

        if not display_name:
            top_display = payload.get("display_name")
            if isinstance(top_display, str) and top_display.strip():
                display_name = top_display.strip()

        if not username:
            message = payload.get("message")
            if isinstance(message, str) and message:
                match = re.search(r"@([\w-]+)", message)
                if match:
                    username = match.group(1).strip()

        # 某些实现会将角色标识直接注入到 prompt 字段中，而不单独返回 character.username
        if not username:
            prompt_value = payload.get("prompt")
            if isinstance(prompt_value, str) and prompt_value:
                match = re.search(r"@([\w-]+)", prompt_value)
                if match:
                    username = match.group(1).strip()

        return username, display_name

    def _normalize_character_username(self, username: str) -> str:
        cleaned = (username or "").strip()
        if not cleaned:
            raise RuntimeError("角色创建完成但未返回有效 username")
        return cleaned if cleaned.startswith("@") else f"@{cleaned}"

    def _inject_character_into_prompt(self, prompt: str, username_tag: str) -> str:
        normalized_prompt = (prompt or "").strip()
        tag = (username_tag or "").strip()
        if not tag:
            return normalized_prompt
        if tag in normalized_prompt:
            return normalized_prompt
        if not normalized_prompt:
            return tag
        return f"{tag} {normalized_prompt}"

    def _try_sync_character_snippet(self, username_tag: str) -> bool:
        """
        角色创建成功后，将 @username 同步为提示词片段（尽力而为，不影响主流程）。

        设计说明：提示词片段的持久化属于“提示词助手”职责，这里仅做触发与降级处理。
        """
        tag = (username_tag or "").strip()
        if not tag or not tag.startswith("@"):
            return False

        try:
            from snippet_manager import SNIPPET_MANAGER

            SNIPPET_MANAGER.ensure_snippet(
                content=tag,
                category="Sora",
                color="#FF9800",
                update_existing=True,
            )
            return True
        except Exception as exc:
            logger.warning(f"提示词片段同步失败（{tag}）：{exc}")
            return False

    def _create_sora_character(
        self,
        session: requests.Session,
        api_key: str,
        base_url: str,
        character_video: object,
        sync_to_snippets: bool = False,
    ) -> Tuple[str, Optional[str], Dict[str, object]]:
        _ensure_not_interrupted()
        client = SoraVideoClient(session=session, api_key=api_key, base_url=base_url)

        character_video_bytes = self._prepare_video_bytes(character_video)
        filename, content_type = self._infer_video_upload_info(character_video, default_filename="character.mp4")
        logger.info(f"角色参考视频大小：{len(character_video_bytes) / 1024 / 1024:.2f} MB")

        raw_capture: Dict[str, object] = {"create": None, "final_status": None}
        create_file_payload = ("video", filename, character_video_bytes, content_type)

        try:
            create_payload = client.create(
                model="sora-2-characters",
                prompt=CHARACTER_CREATE_PROMPT,
                file_payload=create_file_payload,
            )
        except requests.exceptions.ConnectTimeout as exc:
            raise RuntimeError("Sora 角色接口连接超时，请稍后重试") from exc
        except requests.exceptions.ReadTimeout as exc:
            raise RuntimeError("Sora 角色接口等待超时，服务端可能仍在处理，请勿重复提交") from exc
        except requests.ConnectionError as exc:
            raise RuntimeError("Sora 角色接口连接失败，请检查网络或线路选择") from exc

        raw_capture["create"] = create_payload

        task_id = str(create_payload.get("id") or create_payload.get("video_id") or "").strip()
        if not task_id:
            raise RuntimeError("Sora 角色任务创建失败：未返回任务 ID")

        logger.info(f"Sora 角色任务已创建：{task_id}")

        start_time = time.time()
        poll_delays = [10.0, 25.0, 25.0]
        poll_index = 0
        last_state_log_time = start_time
        last_progress_value: Optional[float] = None
        last_status: Optional[str] = None

        final_payload: Optional[Dict[str, object]] = None

        while True:
            _ensure_not_interrupted()
            if time.time() - start_time > SORA_TOTAL_TIMEOUT:
                raise RuntimeError(f"Sora 角色创建超时（>{int(SORA_TOTAL_TIMEOUT)}s）：{task_id}")

            sleep_seconds = poll_delays[poll_index] if poll_index < len(poll_delays) else 10.0
            poll_index += 1
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

            _ensure_not_interrupted()
            try:
                status_payload = client.status(task_id)
            except requests.exceptions.ConnectTimeout as exc:
                raise RuntimeError("Sora 角色接口连接超时，请稍后重试") from exc
            except requests.exceptions.ReadTimeout as exc:
                raise RuntimeError("Sora 角色接口等待超时，服务端可能仍在处理，请勿重复提交") from exc
            except requests.ConnectionError as exc:
                raise RuntimeError("Sora 角色接口连接失败，请检查网络或线路选择") from exc

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
                now = time.time()
                if status != last_status or now - last_state_log_time >= 30.0:
                    logger.info("Sora 角色任务排队中...")
                    last_state_log_time = now
                    last_status = status
                continue

            if status in ("in_progress", "processing", "running"):
                now = time.time()
                if progress_num is not None:
                    if last_progress_value is None or progress_num != last_progress_value:
                        logger.info(f"Sora 角色创建中：{progress_num:.1f}%")
                        last_state_log_time = now
                        last_progress_value = progress_num
                        last_status = status
                    elif now - last_state_log_time >= 45.0:
                        elapsed = int(now - start_time)
                        logger.info(f"Sora 角色创建中：{progress_num:.1f}%（进度暂未更新，已等待 {elapsed}s）")
                        last_state_log_time = now
                        last_status = status
                else:
                    if status != last_status or now - last_state_log_time >= 45.0:
                        elapsed = int(now - start_time)
                        logger.info(f"Sora 角色创建中...（已等待 {elapsed}s）")
                        last_state_log_time = now
                        last_status = status
                continue

            if status == "completed":
                final_payload = status_payload
                break

            if status in ("failed", "error", "canceled", "cancelled"):
                reason = self._extract_sora_error_message(status_payload)
                if reason:
                    raise RuntimeError(f"Sora 角色创建失败：{_mask_text(reason)}")
                raise RuntimeError("Sora 角色创建失败：未知原因")

            logger.info(f"Sora 角色状态：{status or 'unknown'}")

        if final_payload is None:
            raise RuntimeError("Sora 角色创建失败：未获得完成状态")

        raw_capture["final_status"] = final_payload

        username, display_name = self._extract_sora_character_fields(final_payload)
        if not username:
            raise RuntimeError("Sora 角色创建完成但未返回 username，请稍后重试或更换参考视频")

        username_tag = self._normalize_character_username(username)
        if sync_to_snippets:
            self._try_sync_character_snippet(username_tag)
        return username_tag, display_name, raw_capture

    def _resolve_api_key_and_base(self, raw_input_key: str, route_choice: str) -> Tuple[str, str, str]:
        return resolve_api_key_and_base(self.config_manager, raw_input_key, route_choice)

    def _create_session(self, bypass_proxy: bool, verify_ssl: bool) -> requests.Session:
        session = requests.Session()
        if bypass_proxy:
            session.trust_env = False
            session.proxies = {}
        session.verify = verify_ssl
        return session

    def _raise_for_http_error(self, response: requests.Response) -> None:
        """统一处理 chat/completions 的 HTTP 层错误，尽量透传服务端 message。"""
        if response.status_code < 400:
            return

        detail = ""
        try:
            data = response.json()
            error_obj = data.get("error")
            if isinstance(error_obj, dict):
                detail = error_obj.get("message") or str(error_obj)
            elif isinstance(error_obj, str):
                detail = error_obj
        except Exception:
            # HTML 错误页面（如 Cloudflare 524）：提取关键信息而非截取原始 HTML
            raw_text = response.text or ""
            if "<html" in raw_text.lower() or "<!doctype" in raw_text.lower():
                detail = _extract_error_from_html(raw_text, response.status_code)
            else:
                detail = raw_text[:200]

        masked_detail = _mask_text(detail or "")

        if response.status_code == 401:
            if masked_detail:
                raise RuntimeError(f"{self._ERR_AUTH}：{masked_detail}")
            raise RuntimeError(self._ERR_AUTH)

        raise RuntimeError(
            f"视频接口异常：HTTP {response.status_code} {masked_detail}"
        )

    def _parse_stream(
        self,
        response: requests.Response,
        progress_bar: Optional[object],
        read_timeout: float = READ_TIMEOUT,
        progress_cb: Optional[callable] = None,
    ) -> Tuple[str, Optional[str]]:
        text_fragments: List[str] = []
        video_url: Optional[str] = None
        server_error: Optional[str] = None  # 记录服务器返回的错误信息
        start_time = time.time()

        buffer = ""
        for raw_line in response.iter_lines(decode_unicode=True):
            _ensure_not_interrupted()
            if time.time() - start_time > read_timeout:
                raise RuntimeError(f"视频接口整体超时（>{int(read_timeout)}s），请稍后重试")

            if not raw_line:
                continue

            line = raw_line.strip()
            if line.startswith("data:"):
                buffer = line[5:].strip()
            elif buffer:
                # Continuation of previous line (server sent newline inside JSON)
                buffer += "\n" + raw_line
            else:
                continue

            if buffer == "[DONE]":
                break

            try:
                payload = json.loads(buffer, strict=False)
                # Successfully parsed JSON, clear buffer
                buffer = ""
            except Exception:
                # Incomplete JSON, wait for more lines
                continue

            # 检查服务器返回的错误信息（如内容审核拒绝等）
            error_obj = payload.get("error")
            if error_obj:
                if isinstance(error_obj, dict):
                    server_error = error_obj.get("message") or str(error_obj)
                elif isinstance(error_obj, str):
                    server_error = error_obj
                logger.warning(f"服务器返回错误: {_mask_text(server_error or '')}")
                # 不要立即 break，继续读取可能还有其他信息

            choices = payload.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")

            # 检查 finish_reason 是否指示错误
            finish_reason = choices[0].get("finish_reason")
            if finish_reason and finish_reason not in ("stop", None):
                # 可能是 content_filter、length 等异常终止原因
                if not server_error:
                    server_error = f"生成被终止: {finish_reason}"
                logger.warning(f"finish_reason 异常: {finish_reason}")

            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text_parts = [item.get("text", "") for item in content if isinstance(item, dict)]
                text = "".join(text_parts)

            reasoning = delta.get("reasoning_content")
            if reasoning and isinstance(reasoning, str):
                text = (text or "") + reasoning

            if text:
                text_fragments.append(text)
                _log_progress_from_text(text, progress_bar)
                percentages = re.findall(r"(\d+(?:\.\d+)?)%", text)
                if percentages and progress_cb is not None:
                    try:
                        progress_cb(float(percentages[-1]))
                    except Exception:
                        pass
                maybe_url = _extract_url(text)
                if maybe_url:
                    video_url = maybe_url

        # 如果没有 video_url 但有服务器错误，将错误放入 text 中
        full_text = "".join(text_fragments)
        if not video_url and server_error:
            if full_text:
                full_text = f"{full_text}\n\n[服务器错误] {server_error}"
            else:
                full_text = f"[服务器错误] {server_error}"

        return full_text, video_url

    def _parse_non_stream(
        self,
        response: requests.Response,
    ) -> Tuple[str, Optional[str]]:
        """解析非流式返回的 chat/completions 响应。"""
        try:
            payload = response.json()
        except Exception:
            raw_text = response.text or ""
            return raw_text, _extract_url(raw_text)

        server_error: Optional[str] = None
        error_obj = payload.get("error")
        if error_obj:
            if isinstance(error_obj, dict):
                server_error = error_obj.get("message") or str(error_obj)
            elif isinstance(error_obj, str):
                server_error = error_obj
            logger.warning(f"服务器返回错误: {_mask_text(server_error or '')}")

        text = ""
        choices = payload.get("choices") or []
        if isinstance(choices, list) and choices:
            choice0 = choices[0] if isinstance(choices[0], dict) else {}
            message = choice0.get("message") or {}
            content = None
            if isinstance(message, dict):
                content = message.get("content")
            if content is None:
                content = choice0.get("text")

            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        part_text = item.get("text")
                        if part_text is not None:
                            parts.append(str(part_text))
                text = "".join(parts)

            finish_reason = choice0.get("finish_reason")
            if finish_reason and finish_reason not in ("stop", None):
                if not server_error:
                    server_error = f"生成被终止: {finish_reason}"
                logger.warning(f"finish_reason 异常: {finish_reason}")

        video_url = _extract_url(text)
        if not video_url and server_error:
            if text:
                text = f"{text}\n\n[服务器错误] {server_error}"
            else:
                text = f"[服务器错误] {server_error}"

        return (text or "").strip(), video_url

    def _download_video(
        self,
        session: requests.Session,
        url: str,
        progress_cb: Optional[callable] = None,
        progress_min: int = 98,
        progress_max: int = 100,
        headers: Optional[Dict[str, str]] = None,
    ) -> BananaVideo:
        return download_video(
            session,
            url,
            progress_cb=progress_cb,
            progress_min=progress_min,
            progress_max=progress_max,
            headers=headers,
        )

    def _apply_stagger_delay(self, batch_index: int, stagger_delay: float) -> None:
        """按批次索引增加交错延迟，减少瞬时请求尖峰"""
        if stagger_delay <= 0:
            return
        delay = batch_index * stagger_delay
        if delay > 0:
            import time as time_module
            time_module.sleep(delay)

    def _prepare_content_blocks(self, prompt: str, image_payload: Optional[Dict]) -> List[Dict[str, object]]:
        content_blocks: List[Dict[str, object]] = []
        if image_payload:
            content_blocks.append(image_payload)
        if prompt:
            content_blocks.append({"type": "text", "text": _append_unique_suffix(prompt)})
        return content_blocks

    def _is_sora_model(self, model_id: str) -> bool:
        mid = (model_id or "").strip().lower()
        return mid.startswith("sora-") or mid.startswith("sora_")

    def _extract_sora_video_url(self, payload: Dict[str, object]) -> Optional[str]:
        candidate = payload.get("video_url")
        if isinstance(candidate, str) and candidate.strip():
            return _clean_url(candidate)
        data = payload.get("data")
        if isinstance(data, dict):
            nested = data.get("video_url")
            if isinstance(nested, str) and nested.strip():
                return _clean_url(nested)
        return None

    def _extract_sora_error_message(self, payload: Dict[str, object]) -> str:
        error_obj = payload.get("error")
        if isinstance(error_obj, dict):
            message = str(error_obj.get("message") or error_obj.get("detail") or str(error_obj))
            if "photorealistic people" in message.lower():
                return "服务端拒绝：当前不支持上传包含写实人物的参考媒体（photorealistic people）。请换用二次元/CG/非写实角色参考视频，或不传参考视频。"
            return message
        if isinstance(error_obj, str):
            message = error_obj
            if "photorealistic people" in message.lower():
                return "服务端拒绝：当前不支持上传包含写实人物的参考媒体（photorealistic people）。请换用二次元/CG/非写实角色参考视频，或不传参考视频。"
            return message
        for key in ("message", "detail", "reason"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                message = value
                if "photorealistic people" in message.lower():
                    return "服务端拒绝：当前不支持上传包含写实人物的参考媒体（photorealistic people）。请换用二次元/CG/非写实角色参考视频，或不传参考视频。"
                return message
        return ""

    def _generate_sora_video(
        self,
        session: requests.Session,
        api_key: str,
        base_url: str,
        model_id: str,
        prompt: str,
        create_file_payload: Optional[Tuple[str, str, bytes, str]],
        create_url_payload: Optional[Tuple[str, str]],
        create_extra_payload: Optional[Dict[str, object]] = None,
        progress_bar: Optional[object] = None,
        progress_cb: Optional[callable] = None,
        final_status_cb: Optional[callable] = None,
        route_choice: str = "",
        api_key_source: str = "node_input",
    ) -> Tuple[str, BananaVideo]:
        _ensure_not_interrupted()
        client = SoraVideoClient(session=session, api_key=api_key, base_url=base_url)

        progress_value = 0

        def _update_progress(pct: float) -> None:
            nonlocal progress_value
            try:
                pct_int = int(float(pct))
            except Exception:
                return
            pct_int = max(0, min(100, pct_int))
            if pct_int < progress_value:
                pct_int = progress_value
            progress_value = pct_int
            if progress_bar is not None:
                _safe_progress_bar_update(progress_bar, progress_value, 100)
            if progress_cb is not None:
                try:
                    progress_cb(float(progress_value))
                except Exception:
                    pass

        retryable_http_status = {
            408,  # Request Timeout
            425,  # Too Early（部分网关会返回）
            429,  # Too Many Requests
            500,
            502,
            503,
            504,
            520,
            521,
            522,
            523,
            524,
        }

        # Sora 已为异步任务接口：无需再通过向提示词注入随机后缀来“打散”请求。
        # 保持用户输入的 prompt 原样提交，避免额外 token 干扰生成效果与可复现性。
        final_prompt = prompt or ""

        # 创建阶段为非幂等操作：仅在明确的临时性 HTTP 错误时做有限重试，并附带 Idempotency-Key
        #（若上游支持幂等，则可避免并发下的重复任务）。
        create_idempotency_key = uuid.uuid4().hex

        # ⚠️ 重要：创建接口通常可能触发计费。默认不自动重试以避免潜在的重复计费风险。
        # 如确需重试，可通过环境变量开启：BANANA_SORA_CREATE_RETRIES=1（最多建议 1 次）。
        create_retries = 0
        try:
            create_retries = int(os.environ.get("BANANA_SORA_CREATE_RETRIES", "0") or 0)
        except Exception:
            create_retries = 0
        create_retries = max(0, min(create_retries, 3))
        max_create_attempts = 1 + create_retries

        for attempt in range(max_create_attempts):
            _ensure_not_interrupted()
            try:
                create_payload = client.create(
                    model=model_id,
                    prompt=final_prompt,
                    file_payload=create_file_payload,
                    url_payload=create_url_payload,
                    extra_payload=create_extra_payload,
                    idempotency_key=create_idempotency_key,
                )
                break
            except RuntimeError as exc:
                status_code = _try_extract_http_status_code(str(exc))
                if status_code in retryable_http_status and attempt < max_create_attempts - 1:
                    backoff_seconds = 1.5 + (random.random() * 1.5)
                    logger.warning(
                        f"Sora 创建任务遇到可重试错误（HTTP {status_code}），{backoff_seconds:.1f}s 后重试..."
                    )
                    time.sleep(backoff_seconds)
                    continue
                raise
            except requests.exceptions.ConnectTimeout as exc:
                raise RuntimeError("Sora 视频接口连接超时，请稍后重试") from exc
            except requests.exceptions.ReadTimeout as exc:
                raise RuntimeError("Sora 视频接口等待超时，服务端可能仍在处理，请勿重复提交") from exc
            except requests.ConnectionError as exc:
                raise RuntimeError("Sora 视频接口连接失败，请检查网络或线路选择") from exc
        else:
            raise RuntimeError("Sora 视频任务创建失败：未知原因")

        video_id = str(create_payload.get("id") or create_payload.get("video_id") or "").strip()
        if not video_id:
            raise RuntimeError("Sora 视频任务创建失败：未返回 video_id")

        logger.info(f"Sora 任务已创建：{video_id}")
        VIDEO_TASK_MANAGER.record_task_created(
            task_id=video_id,
            provider="sora",
            model=str(model_id or "").strip(),
            prompt=str(final_prompt or "").strip(),
            route_choice=str(route_choice or "").strip(),
            base_url=str(base_url or "").strip(),
            api_key_source=str(api_key_source or "").strip() or "node_input",
            verify_ssl=bool(getattr(session, "verify", True)),
            bypass_proxy=not bool(getattr(session, "trust_env", True)),
        )
        _update_progress(1)

        start_time = time.time()
        poll_delays = [10.0, 25.0, 25.0]
        poll_index = 0
        last_state_log_time = start_time
        last_progress_value: Optional[float] = None
        last_status: Optional[str] = None

        # 轮询阶段属于幂等 GET：在并发场景下更容易遇到网络抖动/网关限流/偶发 5xx，
        # 不应因单次轮询失败而直接中止任务（否则服务端任务仍在跑，但本地不再继续跟进）。
        consecutive_poll_errors = 0
        max_consecutive_poll_errors = 3

        timeout_limit = SORA_PRO_TOTAL_TIMEOUT if create_extra_payload else SORA_TOTAL_TIMEOUT

        while True:
            _ensure_not_interrupted()
            if time.time() - start_time > timeout_limit:
                raise RuntimeError(f"Sora 视频生成超时（>{int(timeout_limit)}s）：{video_id}")

            sleep_seconds = poll_delays[poll_index] if poll_index < len(poll_delays) else 10.0
            poll_index += 1
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

            _ensure_not_interrupted()
            try:
                status_payload = client.status(video_id)
            except requests.exceptions.ConnectTimeout as exc:
                consecutive_poll_errors += 1
                if consecutive_poll_errors < max_consecutive_poll_errors:
                    logger.warning(
                        f"Sora 状态轮询连接超时，将继续等待重试（{consecutive_poll_errors}/{max_consecutive_poll_errors - 1}）"
                    )
                    continue
                raise RuntimeError(f"Sora 状态轮询连续失败，已中止：{video_id}") from exc
            except requests.exceptions.ReadTimeout as exc:
                consecutive_poll_errors += 1
                if consecutive_poll_errors < max_consecutive_poll_errors:
                    logger.warning(
                        f"Sora 状态轮询读取超时，将继续等待重试（{consecutive_poll_errors}/{max_consecutive_poll_errors - 1}）"
                    )
                    continue
                raise RuntimeError(f"Sora 状态轮询连续失败，已中止：{video_id}") from exc
            except requests.ConnectionError as exc:
                consecutive_poll_errors += 1
                if consecutive_poll_errors < max_consecutive_poll_errors:
                    logger.warning(
                        f"Sora 状态轮询连接失败，将继续等待重试（{consecutive_poll_errors}/{max_consecutive_poll_errors - 1}）"
                    )
                    continue
                raise RuntimeError(f"Sora 状态轮询连续失败，已中止：{video_id}") from exc
            except RuntimeError as exc:
                status_code = _try_extract_http_status_code(str(exc))
                if status_code in retryable_http_status:
                    consecutive_poll_errors += 1
                    if consecutive_poll_errors < max_consecutive_poll_errors:
                        logger.warning(
                            f"Sora 状态轮询遇到可重试错误（HTTP {status_code}），将继续等待重试（{consecutive_poll_errors}/{max_consecutive_poll_errors - 1}）"
                        )
                        continue
                    raise RuntimeError(f"Sora 状态轮询连续失败，已中止：{video_id}") from exc
                raise

            consecutive_poll_errors = 0

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
                now = time.time()
                if status != last_status or now - last_state_log_time >= 30.0:
                    logger.info("Sora 任务排队中...")
                    last_state_log_time = now
                    last_status = status
                _update_progress(progress_num if progress_num is not None else 5)
                continue

            if status in ("in_progress", "processing", "running"):
                now = time.time()
                if progress_num is not None:
                    if last_progress_value is None or progress_num != last_progress_value:
                        logger.info(f"Sora 生成中：{progress_num:.1f}%")
                        last_state_log_time = now
                        last_progress_value = progress_num
                        last_status = status
                    elif now - last_state_log_time >= 45.0:
                        elapsed = int(now - start_time)
                        logger.info(f"Sora 生成中：{progress_num:.1f}%（进度暂未更新，已等待 {elapsed}s）")
                        last_state_log_time = now
                        last_status = status
                    _update_progress(progress_num)
                else:
                    if status != last_status or now - last_state_log_time >= 45.0:
                        elapsed = int(now - start_time)
                        logger.info(f"Sora 生成中...（已等待 {elapsed}s）")
                        last_state_log_time = now
                        last_status = status
                    _update_progress(30)
                continue

            if status == "completed":
                completed_progress = progress_num if progress_num is not None else 95
                _update_progress(min(completed_progress, 95))
                if final_status_cb is not None:
                    try:
                        final_status_cb(status_payload)
                    except Exception:
                        pass
                video_url_from_status = self._extract_sora_video_url(status_payload)
                video_url: Optional[str] = None

                # 优先策略：如果 status 中直接返回了 OSS 链接，优先尝试直接下载，绕过可能不稳定的 content 接口
                if video_url_from_status and video_url_from_status.startswith("http"):
                    # 即使本地下载失败，仍应将视频链接写入任务中心，便于用户在 UI 中找回
                    VIDEO_TASK_MANAGER.record_task_success(video_id, video_url_from_status)
                    logger.info(f"从任务状态中获取到视频链接，尝试直接下载：{_clean_url(video_url_from_status)}")
                    try:
                        # 尝试使用带鉴权头下载（防止是内部签名链接）
                        # 如果是 OSS 公网链接，带头通常会被忽略或报错；为求稳妥，若失败可考虑 fallback
                        video_obj = self._download_video(
                            session,
                            video_url_from_status,
                            progress_cb=_update_progress,
                            headers={"Authorization": f"Bearer {api_key}"},
                        )
                        _update_progress(100)
                        return (f"视频生成成功：{_clean_url(video_url_from_status)}", video_obj)
                    except Exception as dl_exc:
                        logger.warning(f"直接下载 status 链接失败（{dl_exc}），将尝试 content 接口...")

                # 即便 status 返回了 video_url，也优先通过 content 接口确认“内容已就绪”，避免提前下载导致长时间卡住。
                try:
                    content_resp = client.content(video_id, variant="video")
                except requests.exceptions.ConnectTimeout as exc:
                    consecutive_poll_errors += 1
                    if consecutive_poll_errors < max_consecutive_poll_errors:
                        logger.warning(
                            f"Sora 内容接口连接超时，将继续等待重试（{consecutive_poll_errors}/{max_consecutive_poll_errors - 1}）"
                        )
                        continue
                    raise RuntimeError(f"Sora 内容接口连续失败，已中止：{video_id}") from exc
                except requests.exceptions.ReadTimeout as exc:
                    consecutive_poll_errors += 1
                    if consecutive_poll_errors < max_consecutive_poll_errors:
                        logger.warning(
                            f"Sora 内容接口读取超时，将继续等待重试（{consecutive_poll_errors}/{max_consecutive_poll_errors - 1}）"
                        )
                        continue
                    raise RuntimeError(f"Sora 内容接口连续失败，已中止：{video_id}") from exc
                except requests.ConnectionError as exc:
                    consecutive_poll_errors += 1
                    if consecutive_poll_errors < max_consecutive_poll_errors:
                        logger.warning(
                            f"Sora 内容接口连接失败，将继续等待重试（{consecutive_poll_errors}/{max_consecutive_poll_errors - 1}）"
                        )
                        continue
                    raise RuntimeError(f"Sora 内容接口连续失败，已中止：{video_id}") from exc

                try:
                    status_code = int(getattr(content_resp, "status_code", 0) or 0)
                    if status_code in (301, 302, 303, 307, 308):
                        location = content_resp.headers.get("Location") or content_resp.headers.get("location") or ""
                        if not location:
                            raise RuntimeError("Sora 内容接口返回重定向但缺少 Location")
                        video_url = _clean_url(location)
                    elif status_code == 202:
                        logger.info("Sora 视频内容尚未就绪，继续等待...")
                        continue
                    elif status_code == 410:
                        raise RuntimeError("Sora 视频生成失败：任务已失效或已被清理")
                    elif status_code == 401:
                        raise RuntimeError(self._ERR_AUTH)
                    elif status_code >= 500:
                        # 上游源站偶发 5xx（尤其 502/503）时，通常是内容尚未完全就绪或转存链路抖动。
                        # 这里优先尝试使用 status 返回的 video_url 下载；若失败则继续轮询等待。
                        detail = _mask_text((content_resp.text or "")[:200])
                        if video_url_from_status:
                            logger.warning(
                                f"Sora 内容接口异常（HTTP {status_code}），将尝试使用状态返回的 video_url 下载..."
                            )
                            try:
                                # 即使本地下载失败，仍应将视频链接写入任务中心，便于用户在 UI 中找回
                                VIDEO_TASK_MANAGER.record_task_success(video_id, video_url_from_status)
                                video_obj = self._download_video(
                                    session,
                                    video_url_from_status,
                                    progress_cb=_update_progress,
                                    headers={"Authorization": f"Bearer {api_key}"},
                                )
                                _update_progress(100)
                                return (f"视频生成成功：{_clean_url(video_url_from_status)}", video_obj)
                            except Exception as download_exc:
                                logger.warning(f"备用 video_url 下载失败，将继续等待：{download_exc}")
                        logger.info("Sora 视频内容尚未就绪或源站抖动，继续等待...")
                        continue
                    elif status_code >= 400:
                        detail = _mask_text((content_resp.text or "")[:200])
                        raise RuntimeError(f"Sora 视频内容获取失败：HTTP {status_code} {detail}".strip())
                    else:
                        content_type = (content_resp.headers.get("Content-Type") or "").lower()
                        if content_resp.content and (
                            content_type.startswith("video/") or content_type == "application/octet-stream"
                        ):
                            logger.info("Sora 内容接口直接返回视频数据")
                            _update_progress(100)
                            return (f"视频生成成功：{video_id}", BananaVideo(io.BytesIO(content_resp.content)))
                finally:
                    try:
                        content_resp.close()
                    except Exception:
                        pass

                if not video_url:
                    video_url = video_url_from_status

                _update_progress(98)
                if not video_url:
                    raise RuntimeError("Sora 内容接口未返回可用的视频链接")
                # 即使本地下载失败，仍应将视频链接写入任务中心，便于用户在 UI 中找回
                VIDEO_TASK_MANAGER.record_task_success(video_id, video_url)
                video_obj = self._download_video(
                    session,
                    video_url,
                    progress_cb=_update_progress,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                _update_progress(100)
                return (f"视频生成成功：{video_url}", video_obj)

            if status in ("failed", "error", "canceled", "cancelled"):
                reason = self._extract_sora_error_message(status_payload)
                if reason:
                    raise RuntimeError(f"Sora 视频生成失败：{_mask_text(reason)}")
                raise RuntimeError("Sora 视频生成失败：未知原因")

            logger.info(f"Sora 状态：{status or 'unknown'}")

    def _generate_single_video(self, args) -> Dict:
        """
        生成单个视频的核心逻辑，供 BatchGenerationRunner 调用。
        返回统一结构: {index, success, video_obj?, text, seed, error?}
        """
        args_list = list(args) if isinstance(args, (list, tuple)) else [args]
        if len(args_list) < 14:
            raise RuntimeError("任务参数不足，无法生成视频")

        batch_index = args_list[0]
        current_seed = args_list[1]
        session = args_list[2]
        api_key = args_list[3]
        base_url = args_list[4]
        model_id = args_list[5]
        model_type = args_list[6]
        prompt = args_list[7]
        image_payload = args_list[8]
        sora_image_url = args_list[9]
        current_read_timeout = args_list[10]
        stagger_delay = args_list[11]
        enable_streaming = args_list[12]
        progress_sync = args_list[13]
        character_create_file_payload = args_list[14] if len(args_list) >= 15 else None
        sora_create_extra_payload = args_list[15] if len(args_list) >= 16 else None
        route_choice = args_list[16] if len(args_list) >= 17 else ""
        api_key_source = args_list[17] if len(args_list) >= 18 else "node_input"

        _ensure_not_interrupted()
        self._apply_stagger_delay(batch_index, stagger_delay)

        logger.info(f"批次 {batch_index + 1} 开始请求...")

        try:
            if self._is_sora_model(model_id):
                if character_create_file_payload is not None:
                    create_file_payload = character_create_file_payload
                    create_url_payload = None
                else:
                    create_file_payload = None
                    create_url_payload = ("image", sora_image_url) if sora_image_url else None
                text_content, video_obj = self._generate_sora_video(
                    session=session,
                    api_key=api_key,
                    base_url=base_url,
                    model_id=model_id,
                    prompt=prompt,
                    create_file_payload=create_file_payload,
                    create_url_payload=create_url_payload,
                    create_extra_payload=sora_create_extra_payload,
                    progress_bar=None,
                    progress_cb=(lambda pct: progress_sync(batch_index, pct)) if progress_sync else None,
                    route_choice=str(route_choice or "").strip(),
                    api_key_source=str(api_key_source or "").strip() or "node_input",
                )
                final_text = (text_content or "").strip()
                logger.success(f"批次 {batch_index + 1} 完成")
                return {
                    "index": batch_index,
                    "success": True,
                    "video_obj": video_obj,
                    "text": final_text,
                    "seed": current_seed,
                }

            content_blocks = self._prepare_content_blocks(prompt, image_payload)
            if not content_blocks:
                return {
                    "index": batch_index,
                    "success": False,
                    "error": "请求体构造失败",
                    "seed": current_seed,
                }

            payload = {
                "model": model_id,
                "messages": [{"role": "user", "content": content_blocks}],
                "stream": bool(enable_streaming),
            }

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            url = f"{base_url.rstrip('/')}/v1/chat/completions"

            last_exc: Optional[BaseException] = None
            for attempt in range(HANDSHAKE_RETRIES + 1):
                _ensure_not_interrupted()
                try:
                    response = session.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=(CONNECT_TIMEOUT, current_read_timeout),
                        stream=bool(enable_streaming),
                    )
                    response.encoding = "utf-8"
                    self._raise_for_http_error(response)

                    # 单批次无进度条，传 None
                    if enable_streaming:
                        text_content, video_url = self._parse_stream(
                            response,
                            None,
                            read_timeout=current_read_timeout,
                            progress_cb=(lambda pct: progress_sync(batch_index, pct)) if progress_sync else None,
                        )
                    else:
                        text_content, video_url = self._parse_non_stream(response)
                    if not video_url:
                        # 检查是否有服务器错误信息（脱敏处理源站信息）
                        if text_content and "[服务器错误]" in text_content:
                            err_msg = _mask_text(text_content.split("[服务器错误]")[-1].strip())
                            raise RuntimeError(err_msg)
                        elif text_content and len(text_content.strip()) > 0:
                            raise RuntimeError(f"视频生成失败: {_mask_text(text_content.strip()[:200])}")
                        else:
                            raise RuntimeError("接口未返回视频链接，请稍后重试")

                    video_obj = self._download_video(
                        session,
                        video_url,
                        progress_cb=(lambda pct: progress_sync(batch_index, pct)) if progress_sync else None,
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                    final_text = text_content or f"视频生成成功：{video_url}"
                    logger.success(f"批次 {batch_index + 1} 完成")
                    return {
                        "index": batch_index,
                        "success": True,
                        "video_obj": video_obj,
                        "text": final_text.strip(),
                        "seed": current_seed,
                    }
                except (
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.StreamConsumedError,
                ) as e:
                    raise RuntimeError(
                        "视频流传输中断 (ChunkedEncodingError)，可能是网络波动导致，请重试"
                    ) from e
                except requests.exceptions.ConnectTimeout as exc:
                    last_exc = exc
                    if attempt < HANDSHAKE_RETRIES:
                        logger.warning(f"批次 {batch_index + 1} 连接超时，正在重试...")
                        continue
                    raise RuntimeError("视频接口连接超时，请稍后重试") from exc
                except requests.exceptions.ReadTimeout as exc:
                    raise RuntimeError(
                        "视频接口等待超时，服务端可能仍在处理，请勿重复提交"
                    ) from exc
                except requests.ConnectionError as exc:
                    last_exc = exc
                    if attempt < HANDSHAKE_RETRIES:
                        logger.warning(f"批次 {batch_index + 1} 连接异常，正在重试...")
                        continue
                    raise RuntimeError("视频接口连接失败，请检查网络或线路选择") from exc
                except BaseException as exc:
                    last_exc = exc
                    raise

            if last_exc:
                raise RuntimeError(f"视频生成失败：{last_exc}") from last_exc
            raise RuntimeError("视频生成失败：未知错误")

        except comfy.model_management.InterruptProcessingException:
            logger.warning(f"批次 {batch_index + 1} 已取消")
            raise
        except Exception as exc:
            error_msg = str(exc)[:200]
            logger.error(f"批次 {batch_index + 1} 失败: {error_msg}")
            return {
                "index": batch_index,
                "success": False,
                "error": error_msg,
                "seed": current_seed,
            }

    def generate(
        self,
        prompt: str,
        banana_api_key: str = "",
        model_type: str = "sora-2-10s",
        aspect_ratio: str = "横版",
        batch_size: int = 1,
        仅提交不等待: bool = False,
        seed: int = -1,
        image: Optional[torch.Tensor] = None,
        流式模式: bool = True,
        绕过代理: bool = False,
        禁用SSL验证: bool = False,
        线路: str = "心宝❤新渠道",
        启用角色: bool = False,
        仅创建角色: bool = False,
        保存角色到提示词助手: bool = False,
        角色参考视频: Optional[object] = None,
    ):
        _ensure_not_interrupted()

        # [兼容性处理] 旧渠道名称自动映射到新渠道（静默兼容旧工作流）
        _legacy_route_names = {
            "测试高速渠道(key不通用)",
            "心宝❤测试新渠道（Key不通用）",
            "心宝❤测试新渠道(Key不通用)",
            "心宝测试新渠道",
        }
        if 线路 in _legacy_route_names:
            logger.warning(f"检测到旧渠道名称 '{线路}'，已自动重定向至 '心宝❤新渠道'")
            线路 = "心宝❤新渠道"

        enable_character_requested = bool(启用角色)
        create_character_only_requested = bool(仅创建角色)

        if create_character_only_requested:
            if 角色参考视频 is None:
                raise RuntimeError("已启用仅创建角色，但未提供角色参考视频（VIDEO）")
        else:
            if not prompt and image is None:
                raise RuntimeError("请提供提示词或参考图像")

        # 参数校验
        batch_size = max(1, min(batch_size, 8))

        try:
            api_key, base_url, key_source = self._resolve_api_key_and_base(banana_api_key, 线路)
        except RuntimeError as exc:
            raise RuntimeError(str(exc)) from exc
        masked_key = _mask_key(api_key)
        logger.info(f"API Key 已解析（来源: {key_source}）")
        verify_ssl = not bool(禁用SSL验证)
        if 禁用SSL验证:
            logger.warning("已禁用 SSL 验证，请确保当前网络可信以免泄露密钥")

        enable_streaming = True if 流式模式 is None else bool(流式模式)
        session = self._create_session(bool(绕过代理), verify_ssl)
        try:
            kv_result = check_kv_auth(
                self.kv_auth_client,
                api_key,
                线路,
                verify_ssl=verify_ssl,
                disable_ssl_override=禁用SSL验证,
            )
            if 线路 == "心宝❤新渠道":
                logger.info("测试渠道，KV 验证通过")
            if not kv_result.allowed:
                raise RuntimeError(kv_result.user_message or self._ERR_AUTH)
            if kv_result.fail_open:
                logger.warning("KV 鉴权异常已按 fail-open 放行，请确认服务状态")

            model_id = _build_model_id(model_type, aspect_ratio)
            if 线路 == "心宝❤新渠道":
                test_model_map = {
                    "sora-2-portrait-15s": "sora_video2-portrait-15s",
                    "sora-2-landscape-15s": "sora_video2-landscape-15s",
                    "sora-2-portrait": "sora_video2-portrait",
                    "sora-2-landscape": "sora_video2-landscape",
                }
                model_id = test_model_map.get(model_id, model_id)
            is_sora_model = self._is_sora_model(model_id)
            sora_create_extra_payload = _build_sora_pro_create_payload(model_type, aspect_ratio)
            if 仅提交不等待:
                if not is_sora_model:
                    raise RuntimeError("“仅提交不等待”仅支持 Sora 模型；Veo 请使用 Veo 专用节点")
                if batch_size != 1:
                    raise RuntimeError("“仅提交不等待”当前仅支持 batch_size=1（单任务提交）")
                if enable_character_requested or create_character_only_requested:
                    raise RuntimeError("“仅提交不等待”暂不支持角色功能/仅创建角色，请关闭相关开关后重试")
            if is_sora_model:
                # Key 仅内存缓存（不落盘），用于后台任务中心轮询；仅在 Sora 分支启用
                VIDEO_TASK_MANAGER.set_key("sora", api_key, route_choice=线路, source=key_source)

            display_route = display_route_label(线路, banana_api_key)

            logger.header("🎬 心宝视频生成任务")
            logger.info(f"模型: {model_id}")
            if sora_create_extra_payload is not None:
                seconds = sora_create_extra_payload.get("seconds")
                size = sora_create_extra_payload.get("size")
                logger.info(f"Sora Pro 参数: seconds={seconds}s size={size}")
            logger.info(f"线路: {display_route}")
            logger.info(f"密钥: {masked_key}")
            logger.info(f"批次数量: {batch_size}")
            if is_sora_model:
                logger.info("流式模式: Sora 分支不适用（已忽略）")
            else:
                logger.info(f"流式模式: {'开启' if enable_streaming else '关闭'}")
            if seed != -1:
                logger.info(f"随机种子: {seed}（接口可能忽略）")
            logger.separator()

            # 根据模型类型选择超时时间
            is_pro_model = sora_create_extra_payload is not None
            current_read_timeout = READ_TIMEOUT_PRO if is_pro_model else READ_TIMEOUT

            # 预处理参考图（所有批次共用）
            image_payload = None
            sora_image_url: Optional[str] = None
            if image is not None:
                img_bytes, _ = self._prepare_image_bytes(image)
                if model_type.strip().lower() == "veo3.1":
                    image_url = self._upload_image_to_public_url(session, img_bytes)
                    logger.info(f"已获取公网图链：{_mask_url(image_url)}")
                    image_payload = {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    }
                else:
                    if is_sora_model:
                        sora_image_url = self._upload_image_to_public_url(session, img_bytes)
                        logger.info(f"Sora 参考图已转为公网 URL：{_mask_url(sora_image_url)}")
                    else:
                        logger.info("已准备参考图（将以 multipart 上传）")
            else:
                logger.info("未提供参考图，将按文本生视频")

            sync_character_to_snippets = bool(保存角色到提示词助手)
            prefix_lines: List[str] = []

            enable_character = bool(启用角色)
            create_character_only = bool(仅创建角色)
            if (enable_character or create_character_only) and not is_sora_model:
                logger.warning("已启用角色相关功能，但当前模型不是 Sora 系，功能已忽略")
                enable_character = False
                create_character_only = False

            if create_character_only:
                if 角色参考视频 is None:
                    raise RuntimeError("已启用仅创建角色，但未提供角色参考视频（VIDEO）")
                if batch_size != 1:
                    logger.warning("仅创建角色模式已开启：batch_size 将被忽略（仅创建一次角色）")
                if image is not None:
                    logger.info("仅创建角色模式：参考图将被忽略")

                logger.header("🎭 Sora 仅创建角色")
                username_tag, display_name, _raw_capture = self._create_sora_character(
                    session=session,
                    api_key=api_key,
                    base_url=base_url,
                    character_video=角色参考视频,
                    sync_to_snippets=sync_character_to_snippets,
                )
                if display_name:
                    prefix_lines.append(f"🎭 角色: {username_tag} ({display_name})")
                else:
                    prefix_lines.append(f"🎭 角色: {username_tag}")
                prefix_lines.append("✅ 角色创建完成（未生成新视频）")

                final_text = "\n\n".join([line for line in prefix_lines if line]).strip()
                return (角色参考视频, final_text, [角色参考视频])

            direct_character_generation = False
            if enable_character:
                if 角色参考视频 is None:
                    raise RuntimeError("已启用角色功能，但未提供角色参考视频（VIDEO）")

                if batch_size == 1:
                    # 按上游文档：model=目标视频模型 + prompt + video（角色视频）即可“创建角色并生成视频”
                    # 该路径避免额外创建 char_* 任务，减少调用次数与复杂度。
                    direct_character_generation = True
                    prefix_lines.append("🎭 角色模式: video+prompt 单次请求（同一轮创建+生成）")
                else:
                    # 批量模式下如果每批都走 video+prompt，会重复创建角色且重复上传视频。
                    # 这里先创建一次角色拿到 @username，再复用到每批 prompt（更省时省钱）。
                    logger.header("🎭 Sora 角色创建（批量复用）")
                    try:
                        username_tag, display_name, _raw_capture = self._create_sora_character(
                            session=session,
                            api_key=api_key,
                            base_url=base_url,
                            character_video=角色参考视频,
                            sync_to_snippets=sync_character_to_snippets,
                        )
                        prompt = self._inject_character_into_prompt(prompt, username_tag)

                        if display_name:
                            prefix_lines.append(f"🎭 角色: {username_tag} ({display_name})")
                        else:
                            prefix_lines.append(f"🎭 角色: {username_tag}")
                        prefix_lines.append("✅ 已将角色名注入 prompt（批量模式复用，避免重复创建）")
                        logger.separator()
                    except Exception as exc:
                        # 当前分组可能未开通 `sora-2-characters` 渠道；此时回退为每批次 video+prompt 的“同一轮创建+生成”。
                        error_text = str(exc or "")
                        if "sora-2-characters" in error_text and "无可用渠道" in error_text:
                            logger.warning("当前账号分组未开通 sora-2-characters，将回退为每批次 video+prompt 方式（不注入 @username）")
                            direct_character_generation = True
                            prefix_lines.append("⚠️ 批量角色模式回退：sora-2-characters 不可用，将按每批次 video+prompt 方式生成（不注入 @username）")
                        else:
                            raise

            # 单批次时走简化路径（与原有行为一致）
            if batch_size == 1:
                progress_bar = comfy.utils.ProgressBar(100)

                if is_sora_model:
                    try:
                        create_file_payload = None
                        create_url_payload: Optional[Tuple[str, str]] = (
                            ("image", sora_image_url) if sora_image_url else None
                        )

                        if direct_character_generation:
                            if sora_image_url is not None:
                                logger.info("已启用角色功能：将使用角色参考视频作为 video 输入，参考图将被忽略")
                                create_url_payload = None

                            character_video_bytes = self._prepare_video_bytes(角色参考视频)
                            filename, content_type = self._infer_video_upload_info(角色参考视频, default_filename="character.mp4")
                            logger.info(
                                f"角色参考视频大小：{len(character_video_bytes) / 1024 / 1024:.2f} MB"
                            )
                            create_file_payload = ("video", filename, character_video_bytes, content_type)
                            create_url_payload = None

                        role_info: Dict[str, object] = {"tag": None, "display_name": None, "synced": False}

                        def _capture_role_from_status(payload: Dict[str, object]) -> None:
                            if not direct_character_generation or not enable_character:
                                return
                            if role_info.get("tag"):
                                return
                            username, display_name = self._extract_sora_character_fields(payload)
                            if not username:
                                return
                            username_tag = self._normalize_character_username(username)
                            role_info["tag"] = username_tag
                            role_info["display_name"] = display_name
                            if sync_character_to_snippets:
                                role_info["synced"] = bool(self._try_sync_character_snippet(username_tag))

                        if 仅提交不等待:
                            if enable_character or create_character_only or direct_character_generation:
                                raise RuntimeError(
                                    "已开启“仅提交不等待”，但当前启用了角色相关功能；该模式暂不支持角色/仅创建角色，请关闭后重试"
                                )

                            # 仅提交任务：创建成功后立即写入任务中心并返回 ExecutionBlocker
                            _ensure_not_interrupted()
                            client = SoraVideoClient(session=session, api_key=api_key, base_url=base_url)
                            final_prompt = prompt or ""
                            create_idempotency_key = uuid.uuid4().hex
                            try:
                                create_payload = client.create(
                                    model=model_id,
                                    prompt=final_prompt,
                                    file_payload=create_file_payload,
                                    url_payload=create_url_payload,
                                    extra_payload=sora_create_extra_payload,
                                    idempotency_key=create_idempotency_key,
                                )
                            except requests.exceptions.ConnectTimeout as exc:
                                raise RuntimeError("Sora 视频接口连接超时，请稍后重试") from exc
                            except requests.exceptions.ReadTimeout as exc:
                                raise RuntimeError("Sora 视频接口等待超时，服务端可能仍在处理，请勿重复提交") from exc
                            except requests.ConnectionError as exc:
                                raise RuntimeError("Sora 视频接口连接失败，请检查网络或线路选择") from exc

                            video_id = str(create_payload.get("id") or create_payload.get("video_id") or "").strip()
                            if not video_id:
                                raise RuntimeError("Sora 视频任务创建失败：未返回 video_id")

                            logger.info(f"Sora 任务已创建（仅提交不等待）：{video_id}")
                            VIDEO_TASK_MANAGER.record_task_created(
                                task_id=video_id,
                                provider="sora",
                                model=str(model_id or "").strip(),
                                prompt=str(final_prompt or "").strip(),
                                route_choice=str(线路 or "").strip(),
                                base_url=str(base_url or "").strip(),
                                api_key_source=str(key_source or "").strip() or "node_input",
                                verify_ssl=bool(getattr(session, "verify", True)),
                                bypass_proxy=not bool(getattr(session, "trust_env", True)),
                            )

                            msg = (
                                f"已提交 Sora 任务：{video_id}\n\n"
                                "已启用“仅提交不等待”：不会等待轮询与下载。\n"
                                "请在右下角“🎬 心宝视频任务”面板输入 Key，然后点击刷新查看状态。"
                            )
                            return (make_execution_blocker(None), msg, [])

                        text_content, video_obj = self._generate_sora_video(
                            session=session,
                            api_key=api_key,
                            base_url=base_url,
                            model_id=model_id,
                            prompt=prompt,
                            create_file_payload=create_file_payload,
                            create_url_payload=create_url_payload,
                            create_extra_payload=sora_create_extra_payload,
                            progress_bar=progress_bar,
                            progress_cb=None,
                            final_status_cb=_capture_role_from_status if direct_character_generation else None,
                            route_choice=线路,
                            api_key_source=key_source,
                        )
                        logger.success("视频生成完成")
                        final_text = (text_content or "").strip()
                        if direct_character_generation and enable_character and role_info.get("tag"):
                            username_tag = str(role_info.get("tag") or "").strip()
                            if username_tag and not any(username_tag in line for line in prefix_lines):
                                display_name = role_info.get("display_name")
                                if isinstance(display_name, str) and display_name.strip():
                                    prefix_lines.append(f"🎭 角色: {username_tag} ({display_name.strip()})")
                                else:
                                    prefix_lines.append(f"🎭 角色: {username_tag}")
                            if sync_character_to_snippets:
                                if bool(role_info.get("synced")):
                                    prefix_lines.append("✅ 已保存角色到提示词助手")
                                else:
                                    prefix_lines.append("⚠️ 保存角色到提示词助手失败（详见控制台日志）")
                        if prefix_lines:
                            final_text = ("\n\n".join(prefix_lines) + "\n\n" + final_text).strip()
                        return (video_obj, final_text, [video_obj])
                    except comfy.model_management.InterruptProcessingException:
                        raise
                    except Exception as e:
                        raise RuntimeError(f"视频生成异常: {e}") from e

                try:
                    content_blocks = self._prepare_content_blocks(prompt, image_payload)
                    if not content_blocks:
                        raise RuntimeError("请求体构造失败，请检查输入")

                    payload = {
                        "model": model_id,
                        "messages": [{"role": "user", "content": content_blocks}],
                        "stream": enable_streaming,
                    }

                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    }
                    url = f"{base_url.rstrip('/')}/v1/chat/completions"

                    last_exc: Optional[BaseException] = None
                    for attempt in range(HANDSHAKE_RETRIES + 1):
                        _ensure_not_interrupted()
                        try:
                            response = session.post(
                                url,
                                json=payload,
                                headers=headers,
                                timeout=(CONNECT_TIMEOUT, current_read_timeout),
                                stream=enable_streaming,
                            )
                            response.encoding = "utf-8"
                            self._raise_for_http_error(response)
                            if enable_streaming:
                                text_content, video_url = self._parse_stream(
                                    response, progress_bar, read_timeout=current_read_timeout
                                )
                            else:
                                # 非流式下无法实时更新进度，先占位到 40%
                                _safe_progress_bar_update(progress_bar, 40, 100)
                                text_content, video_url = self._parse_non_stream(response)
                            if not video_url:
                                # 检查是否有服务器错误信息（脱敏处理源站信息）
                                if text_content and "[服务器错误]" in text_content:
                                    err_msg = _mask_text(text_content.split("[服务器错误]")[-1].strip())
                                    raise RuntimeError(err_msg)
                                elif text_content and len(text_content.strip()) > 0:
                                    # 服务器可能在普通文本中返回了错误说明
                                    raise RuntimeError(f"视频生成失败: {_mask_text(text_content.strip()[:200])}")
                                else:
                                    raise RuntimeError("接口未返回视频链接，请稍后重试")
                            _safe_progress_bar_update(progress_bar, 90, 100)
                            video_obj = self._download_video(
                                session,
                                video_url,
                                progress_cb=(lambda pct: progress_bar.update_absolute(int(pct), 100)),
                                progress_min=90,
                                progress_max=100,
                                headers={"Authorization": f"Bearer {api_key}"},
                            )
                            _safe_progress_bar_update(progress_bar, 100, 100)
                            final_text = text_content or f"视频生成成功：{video_url}"
                            logger.success("视频生成完成")
                            return (video_obj, final_text.strip(), [video_obj])
                        except (
                            requests.exceptions.ChunkedEncodingError,
                            requests.exceptions.StreamConsumedError,
                        ) as e:
                            raise RuntimeError(
                                "视频流传输中断 (ChunkedEncodingError)，可能是网络波动导致，请重试"
                            ) from e
                        except requests.exceptions.ConnectTimeout as exc:
                            last_exc = exc
                            if attempt < HANDSHAKE_RETRIES:
                                logger.warning("连接超时，正在重试...")
                                continue
                            raise RuntimeError("视频接口连接超时，请稍后重试") from exc
                        except requests.exceptions.ReadTimeout as exc:
                            raise RuntimeError(
                                "视频接口等待超时，服务端可能仍在处理，请勿重复提交"
                            ) from exc
                        except requests.ConnectionError as exc:
                            last_exc = exc
                            if attempt < HANDSHAKE_RETRIES:
                                logger.warning("连接异常，正在重试...")
                                continue
                            raise RuntimeError("视频接口连接失败，请检查网络或线路选择") from exc
                        except BaseException as exc:
                            last_exc = exc
                            raise

                    if last_exc:
                        raise RuntimeError(f"视频生成失败：{last_exc}") from last_exc
                    raise RuntimeError("视频生成失败：未知错误")

                except comfy.model_management.InterruptProcessingException:
                    raise
                except Exception as e:
                     raise RuntimeError(f"视频生成异常: {e}") from e

            # 多批次并发路径
            stagger_delay = 2.0  # 每个请求间隔 2 秒启动

            # 主进度条（批次 * 100），支持流式更新
            progress_bar = comfy.utils.ProgressBar(batch_size * 100)
            progress_state = [0 for _ in range(batch_size)]
            progress_lock = threading.Lock()

            def _sync_progress(batch_idx: int, pct: float) -> None:
                if batch_idx < 0 or batch_idx >= batch_size:
                    return
                with progress_lock:
                    clamped = max(0, min(100, int(pct)))
                    progress_state[batch_idx] = clamped
                    _safe_progress_bar_update(progress_bar, sum(progress_state), batch_size * 100)

            # 构建种子
            if seed == -1:
                base_seed = random.randint(0, 2_147_483_647)
            else:
                base_seed = seed

            # 构建任务列表
            tasks = []
            batch_character_file_payload: Optional[Tuple[str, str, bytes, str]] = None
            if direct_character_generation and enable_character and batch_size > 1:
                if sora_image_url is not None:
                    logger.warning("批量角色回退模式：将使用角色参考视频作为 video 输入，参考图将被忽略")
                    sora_image_url = None
                character_video_bytes = self._prepare_video_bytes(角色参考视频)
                filename, content_type = self._infer_video_upload_info(角色参考视频, default_filename="character.mp4")
                batch_character_file_payload = ("video", filename, character_video_bytes, content_type)
            for i in range(batch_size):
                current_seed = base_seed + i if seed != -1 else -1
                tasks.append((
                    i,
                    current_seed,
                    session,
                    api_key,
                    base_url,
                    model_id,
                    model_type,
                    prompt,
                    image_payload,
                    sora_image_url,
                    current_read_timeout,
                    stagger_delay,
                    enable_streaming,
                    _sync_progress,
                    batch_character_file_payload,
                    sora_create_extra_payload,
                    线路,
                    key_source,
                ))

            # 计算实际并发数（最大 8）
            actual_workers = min(batch_size, 8)

            # 创建 BatchGenerationRunner
            task_runner = BatchGenerationRunner(
                logger,
                _ensure_not_interrupted,
                lambda total: progress_bar,
            )

            def progress_callback(result: Dict, completed_count: int, total_count: int, _progress_bar: object):
                if result.get("success"):
                    logger.success(f"[{completed_count}/{total_count}] 批次 {result['index'] + 1} 完成")
                else:
                    batch_label = result.get("index", -1)
                    batch_text = "?" if batch_label < 0 else batch_label + 1
                    logger.error(f"[{completed_count}/{total_count}] 批次 {batch_text} 失败")
                idx = result.get("index", -1)
                if idx is not None and 0 <= idx < batch_size:
                    _sync_progress(idx, 100)

            results = task_runner.run(
                tasks,
                self._generate_single_video,
                batch_size,
                actual_workers,
                True,  # continue_on_error
                progress_callback,
            )

            # 结果聚合
            results.sort(key=lambda x: x["index"])
            video_objects = []
            all_texts = []
            success_count = 0

            for result in results:
                if result.get("success"):
                    video_obj = result.get("video_obj")
                    if video_obj is not None:
                        video_objects.append(video_obj)
                        success_count += 1
                    if result.get("text"):
                        all_texts.append(f"[批次 {result['index'] + 1}] ✅ {result['text']}")
                else:
                    error_msg = f"[批次 {result['index'] + 1}] ❌ {result.get('error', '未知错误')}"
                    all_texts.append(error_msg)

            # 无任何成功时报错
            if not video_objects:
                error_text = f"未生成任何视频（共 {batch_size} 批次全部失败）\n\n" + "\n".join(all_texts)
                raise RuntimeError(error_text)

            # 构建摘要
            summary = f"✅ 成功生成 {success_count}/{batch_size} 个视频"
            if success_count < batch_size:
                summary += f" ⚠️ {batch_size - success_count} 个批次失败"
            combined_text = summary + "\n\n" + "\n".join(all_texts)
            if prefix_lines:
                combined_text = ("\n\n".join(prefix_lines) + "\n\n" + combined_text).strip()

            logger.summary("任务完成", {
                "总批次": f"{batch_size} 个",
                "成功生成": f"{success_count} 个",
            })

            # 返回：video 返回第一个成功的视频
            first_video = video_objects[0]
            return (first_video, combined_text.strip(), video_objects)

        finally:
            if session:
                session.close()
