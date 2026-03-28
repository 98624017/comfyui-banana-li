from __future__ import annotations

import json
import base64
import torch
from typing import List, Dict, Optional, Tuple, Any
import re
import random
import time
import threading
import os
import sys
import datetime
import asyncio
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from image_uploader import ImageUploader

"""
模块名称: Gemini Imagen Generator
功能描述: 
    本模块实现了适配 ComfyUI 的 Gemini 图像生成节点。
    核心功能包括：
    1. 调用 Google Gemini API 进行图像生成 (支持 gemini-3-pro-image 等模型)。
    2. 支持多线程并发生成，加速批量任务。
    3. 实现智能缓存机制，参数未变时自动复用上次结果，节省 Token 和时间。
    4. 集成 config.ini 配置管理、余额查询、API Key 隐藏与优先级处理。
    5. 提供完善的错误处理与前端交互（错误画板 ErrorCanvas）。
    6. V2 版本节点额外增加了 System Instruction (系统提示词) 等高级功能。

维护指南:
    - 核心逻辑入口在 `generate_images` 方法。
    - API 通信逻辑封装在 `api_client.py`，本模块主要负责参数组装与流程控制。
    - 新增模型类型时，需同步更新 `build_required_inputs` 中的 `model_type` 列表。
"""

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

# 在非 ComfyUI 运行环境中,server 可能无法正常导入
# 这里做一个兼容处理:导入失败时提供一个占位 PromptServer,
# 仅用于避免测试脚本导入本模块时报错
try:
    from server import PromptServer
    from aiohttp import web
except ImportError as e:
    # 仅在非 ComfyUI 环境（或路径配置错误）下触发
    # 打印明确警告，便于排查“本地运行为何失败”的问题
    from logger import logger
    logger.warning(f"BananaV2: 无法导入 ComfyUI server 或 aiohttp，将使用 Dummy 模式。错误详情: {e}")
    
    class _DummyPromptServer:
        instance = None
    PromptServer = _DummyPromptServer()
    web = None

import comfy.utils
import comfy.model_management

from logger import logger
from config_manager import ConfigManager
from balance_service import BalanceService
from image_codec import ImageCodec, ErrorCanvas
from api_client import GeminiApiClient
from task_runner import BatchGenerationRunner
from banana_kv_auth import EdgeKVAuthClient
from banana_binding import (
    BINDING_TYPE,
    BindingError,
    activate_binding,
    build_missing_hint,
    validate_pending,
)
from workflow_parallel import get_non_parallel_workflow_lock, get_workflow_parallel_shared_lock, get_workflow_parallel_shared_lock_with_url_collect, make_execution_blocker


CONFIG_MANAGER = ConfigManager(MODULE_DIR)
API_CLIENT = GeminiApiClient(
    CONFIG_MANAGER,
    logger,
    interrupt_checker=comfy.model_management.throw_exception_if_processing_interrupted,
)
BALANCE_SERVICE = BalanceService(API_CLIENT, CONFIG_MANAGER, logger)


def build_required_inputs() -> Dict[str, Tuple[Any, Dict[str, Any]]]:
    """构建节点必填参数描述，便于测试与节点展示共用"""
    return {
        "prompt": (
            "STRING",
            {
                "multiline": True,
                "default": "Peace and love",
                "tooltip": "生成图像的文本提示词，可多行描述内容、风格等",
            },
        ),
        "banana_api_key": (
            "STRING",
            {
                "default": "",
                "multiline": False,
                "tooltip": "调用服务的 API Key；留空则优先使用 config.ini 中的配置",
            },
        ),
        "model_type": (
            [
                "gemini-2.5-flash-image",
                "gemini-3-pro-image-preview",
                "gemini-3-pro-image-preview-vip",
                "gemini-3-pro-image-preview-svip",
                "gemini-3.1-flash-image-preview",
            ],
            {
                "default": "gemini-3-pro-image-preview",
                "tooltip": "选择要使用的模型",
            },
        ),
        "batch_size": (
            "INT",
            {
                "default": 1,
                "min": 1,
                "max": 8,
                "tooltip": "一次请求中要生成的图片数量，范围 1~8",
            },
        ),
        "aspect_ratio": (
            ["Auto", "1:1", "9:16", "16:9", "21:9", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "1:4", "4:1", "1:8", "8:1"],
            {
                "default": "Auto",
                "tooltip": "生成图像的宽高比例，Auto 为由服务端自动决定；1:4/4:1/1:8/8:1 仅 gemini-3.1-flash-image-preview 支持",
            },
        ),
    }


def build_optional_inputs() -> Dict[str, Tuple[Any, Dict[str, Any]]]:
    """构建节点可选参数描述，保持与 UI 展示一致"""
    return {
        "seed": (
            "INT",
            {
                "default": -1,
                "min": -1,
                "max": 102400,
                "control_after_generate": True,
                "tooltip": "随机种子，-1 为自动随机；固定种子可复现同一输出",
            },
        ),
        "top_p": (
            "FLOAT",
            {
                "default": 0.95,
                "min": 0.0,
                "max": 1.0,
                "step": 0.01,
                "tooltip": "采样参数 Top-P，数值越低越保守，越高多样性越强",
            },
        ),
        "image_size": (
            ["1K", "2K", "4K"],
            {
                "default": "2K",
                "tooltip": "仅 gemini-3-pro-image 系列生效的分辨率选项",
            },
        ),
        "image_1": (
            "IMAGE",
            {
                "tooltip": "参考图像 1，可为空；用于图生图或多图融合",
            },
        ),
        "image_2": (
            "IMAGE",
            {
                "tooltip": "参考图像 2，可为空；用于图生图或多图融合",
            },
        ),
        "image_3": (
            "IMAGE",
            {
                "tooltip": "参考图像 3，可为空；用于图生图或多图融合",
            },
        ),
        "image_4": (
            "IMAGE",
            {
                "tooltip": "参考图像 4，可为空；用于图生图或多图融合",
            },
        ),
        "image_5": (
            "IMAGE",
            {
                "tooltip": "参考图像 5，可为空；用于图生图或多图融合",
            },
        ),

        "绕过代理": (
            "BOOLEAN",
            {
                "default": False,
                "tooltip": "梯子速度不佳、不可靠时开启",
            },
        ),
        "禁用SSL验证": (
            "BOOLEAN",
            {
                "default": False,
                "tooltip": "禁用提高成功率，但流量被第三人劫持状况下可能泄露密钥",
            },
        ),
        "线路": (
            ConfigManager.VALID_ROUTE_LABELS + ["心宝❤新渠道"],
            {
                "default": "心宝❤新渠道",
                "tooltip": "固定线路选择，生成与余额共享",
            },
        ),
        "binding_context": (
            BINDING_TYPE,
            {
                "tooltip": "（可选）来自“心宝❤绑定生成/透传”的绑定上下文，仅在搭配绑定增强节点时需要",
            },
        ),
    }


def build_input_types() -> Dict[str, Dict[str, Tuple[Any, Dict[str, Any]]]]:

    """统一输入描述，便于节点与单测复用"""

    return {

        "required": build_required_inputs(),

        "optional": build_optional_inputs(),

    }





class _StageProgressTracker:

    """测试渠道阶段进度追踪，仅用于批量任务的阶段级反馈。"""



    def __init__(self, progress_bar: object, batch_size: int, stages: Tuple[str, ...]):

        self._progress_bar = progress_bar

        self._stages = list(stages)

        self._stage_set = set(self._stages)

        self._total = batch_size * len(self._stages)

        self._current = 0

        self._batch_counts = [0] * batch_size

        self._batch_marks = [set() for _ in range(batch_size)]

        self._lock = threading.Lock()



    def mark(self, batch_index: int, stage: str) -> None:

        if stage not in self._stage_set:

            return

        with self._lock:

            if self._batch_counts[batch_index] >= len(self._stages):

                return

            marks = self._batch_marks[batch_index]

            if stage in marks:

                return

            marks.add(stage)

            self._batch_counts[batch_index] += 1

            self._current += 1

            self._progress_bar.update_absolute(self._current, self._total)



    def complete_batch(self, batch_index: int, preview=None) -> None:

        with self._lock:

            remaining = len(self._stages) - self._batch_counts[batch_index]

            if remaining <= 0:

                if preview is not None:

                    self._progress_bar.update_absolute(self._current, self._total, preview)

                return

            self._batch_counts[batch_index] += remaining

            self._current += remaining

            self._progress_bar.update_absolute(self._current, self._total, preview)





class BananaImageNode:
    """
    [核心节点类] BananaImageNode
    
    功能:
        实现基础的 Gemini 图像生成流程。
    
    主要职责:
        1. 接收 ComfyUI 输入参数 (Prompt, API Key, 分辨率等)。
        2. 解析 API Key (支持 config.ini 兜底与隐藏 Key)。
        3. 执行 "智能缓存" 检查 (Smart Caching)。
        4. 把批量任务拆分为单张生成任务，并行调度执行。
        5. 收集生成结果，解码 Base64 为 Tensor，合并后返回。
    
    继承关系:
        被 BananaImageNodeV2 继承，V2 添加了系统提示词等高级特性。
    """

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "text")
    FUNCTION = "generate_images"
    OUTPUT_NODE = True
    CATEGORY = "❤️‍🔥心宝专用"
    _ERR_AUTH_UNAVAILABLE = "服务暂不可用，请稍后重试或检查密钥配置"
    _ERR_AUTH_FORBIDDEN = "密钥不可用或已失效，请检查后再试"

    # [兼容性] 接受旧工作流中残留的旧渠道名称，跳过 ComfyUI 默认 combo 校验
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
        api_client: Optional[GeminiApiClient] = None,
        config_manager: Optional[ConfigManager] = None,
        image_codec: Optional[ImageCodec] = None,
        error_canvas: Optional[ErrorCanvas] = None,
        balance_service: Optional[BalanceService] = None,
        task_runner: Optional[BatchGenerationRunner] = None,
        kv_auth_client: Optional[EdgeKVAuthClient] = None,
        logger_instance=logger,
    ):
        # 允许注入 mock 以便离线测试，同时保持默认依赖不变
        self.logger = logger_instance or logger
        self.config_manager = config_manager or CONFIG_MANAGER
        self.api_client = api_client or API_CLIENT
        self.image_codec = image_codec or ImageCodec(self.logger, self._ensure_not_interrupted)
        self.error_canvas = error_canvas or ErrorCanvas(self.logger)
        self.balance_service = balance_service or BALANCE_SERVICE
        self.task_runner = task_runner or BatchGenerationRunner(
            self.logger,
            self._ensure_not_interrupted,
            lambda total: comfy.utils.ProgressBar(total),
        )
        self.kv_auth_client = kv_auth_client or EdgeKVAuthClient(self.config_manager, self.logger)
        self.cancel_event = threading.Event()

    # -----------------------------------------------------
    # ComfyUI 节点配置方法
    # -----------------------------------------------------

    @classmethod
    def INPUT_TYPES(cls):
        """
        定义节点的输入参数结构 (ComfyUI 标准方法)。
        返回字典包含 'required' (必填) 和 'optional' (可选) 字段。
        """
        return build_input_types()
    
    def _ensure_not_interrupted(self):
        """
        统一的中断检查，复用 ComfyUI 原生取消机制。
        在耗时操作前/后调用此方法，可响应用户点击 ComfyUI 界面上的 "Cancel" 按钮。
        同时也检查本地 cancel_event 信号。
        """
        if self.cancel_event.is_set():
            raise comfy.model_management.InterruptProcessingException()
        
        try:
            comfy.model_management.throw_exception_if_processing_interrupted()
        except comfy.model_management.InterruptProcessingException:
            # 只要检测到一次取消，就设置本地信号，通知所有 worker 线程
            self.cancel_event.set()
            raise

    def _resolve_api_key_and_base(self, raw_input_key: str, route_choice: str) -> Tuple[str, str, str]:
        """
        统一解析 Banana API Key，遵循 节点/全局 > config.ini 的优先级。
        返回 (api_key, base_url, source_label)。
        """
        candidate = (raw_input_key or "").strip()
        normalized_route = self.config_manager.normalize_xinbao_test_route_label(route_choice)
        effective_base_url = self.config_manager.get_effective_api_base_url(normalized_route)

        lowered_input_key = candidate.lower()
        if lowered_input_key.startswith("fixsk-"):
            stripped_key = candidate[len("fix") :]
            hidden_key = self.config_manager.sanitize_api_key(stripped_key)
            if hidden_key:
                return hidden_key, self.config_manager.get_hidden_base_url(), "隐藏线路临时密钥"

        sanitized_input_key = self.config_manager.sanitize_api_key(candidate)
        if sanitized_input_key:
            return sanitized_input_key, effective_base_url, "节点/全局输入"

        config_key = self.config_manager.sanitize_api_key(self.config_manager.load_api_key())
        if config_key:
            return config_key, effective_base_url, "config.ini 兜底"

        raise RuntimeError("缺少 Banana API Key：请在节点或“心宝❤全局密钥管理”中填写，或在 config.ini 中配置后重试")

    def _build_failure_result(self, index: int, seed: int, error_msg: str, generated_urls: List[str] = None, failed_download_urls: List[str] = None) -> Dict[str, Any]:
        """
        构造统一的失败返回结构。

        用途:
            当某个批次生成失败时，返回此结构，以便主流程能统一处理（记录日志、通过 ErrorCanvas 显示错误等），
            而不是直接崩溃导致整个 Batch 丢失。
        """
        return {
            "index": index,
            "success": False,
            "error": error_msg,
            "seed": seed,
            "tensor": None,
            "image_count": 0,
            "generated_urls": generated_urls,
            "failed_download_urls": failed_download_urls or [],
        }

    def _halt_with_error(self, error_tensor: torch.Tensor, error_msg: str):
        """
        向前端发送错误图片预览，并抛出异常中止工作流。
        这样既能让用户直观看到错误原因，又能防止错误图片被下游节点误用。
        """
        # 尝试构建预览并发送给前端
        try:
            # 这里的 batch_index 填 0 即可，因为是整体错误
            preview_tuple = self.image_codec.build_preview_tuple(error_tensor, 0)
            if preview_tuple is not None:
                # 使用 ComfyUI 的 ProgressBar 发送预览
                # update_absolute(current, total, preview)
                comfy.utils.ProgressBar(1).update_absolute(1, 1, preview_tuple)
        except Exception as e:
            self.logger.error(f"发送错误预览失败: {e}")

        # 抛出异常中止执行
        # 抛出异常中止执行，ComfyUI 界面将显示红色错误条
        raise RuntimeError(error_msg)

    def _apply_stagger_delay(self, batch_index: int, stagger_delay: float) -> None:
        """
        按批次增加交错延迟，减少瞬时请求尖峰。
        例如 stagger_delay=0.2s:
            Batch 0: 延时 0s
            Batch 1: 延时 0.2s
            Batch 2: 延时 0.4s
        """
        if stagger_delay <= 0:
            return
        delay = batch_index * stagger_delay
        if delay > 0:
            time.sleep(delay)

    def _prepare_request_payload(
        self,
        prompt: str,
        seed: int,
        aspect_ratio: str,
        top_p: float,
        input_images_b64: List[str],
        model_type: str,
        image_size: str,
        enable_google_search: bool = False,
    ) -> Dict[str, Any]:
        """封装请求参数构造，便于单元测试直接调用"""
        self._ensure_not_interrupted()
        return self.api_client.create_request_data(
            prompt=prompt,
            seed=seed,
            aspect_ratio=aspect_ratio,
            top_p=top_p,
            input_images_b64=input_images_b64,
            model_type=model_type,
            image_size=image_size,
            enable_google_search=enable_google_search,
        )

    def _record_request_start(
        self,
        request_start_event: threading.Event,
        request_start_time_holder: List[Optional[float]],
        request_start_lock: threading.Lock,
    ) -> None:
        """仅记录首个请求的开始时间，便于耗时统计"""
        if request_start_event.is_set():
            return
        with request_start_lock:
            if request_start_event.is_set():
                return
            request_start_time_holder[0] = time.time()
            request_start_event.set()

    def _decode_images(
        self,
        base64_images: List[str],
        thread_id: str,
        batch_index: int,
        decode_workers: int,
    ) -> Tuple[Optional[torch.Tensor], int]:
        """拆分解码逻辑，便于 mock ImageCodec"""
        if not base64_images:
            return None, 0
        self._ensure_not_interrupted()
        tensor = self.image_codec.base64_to_tensor_parallel(
            base64_images,
            log_prefix=f"[{thread_id}] 批次 {batch_index+1}",
            max_workers=decode_workers,
        )
        return tensor, tensor.shape[0]

    def _build_no_image_reason(self, response_data: Any, text_content: Optional[str]) -> str:
        """根据 finishReason 或文本给出未返回图片的原因说明"""
        reason = ""
        try:
            if isinstance(response_data, dict):
                candidates = response_data.get("candidates") or []
                if candidates and isinstance(candidates[0], dict):
                    finish_reason = candidates[0].get("finishReason") or ""
                    if finish_reason:
                        if finish_reason == "NO_IMAGE":
                            return (
                                "模型未生成任何图片（finishReason=NO_IMAGE，一般表示当前提示或参考图不触发图像输出，可能是内容被过滤或未通过安全审查）"
                            )
                        return f"模型未生成图片（finishReason={finish_reason}）"
        except Exception:
            # 解析失败时忽略即可
            pass

        brief_text = (text_content or "").strip().replace("\n", " ")
        if brief_text:
            return f"模型仅返回文本: {brief_text[:100]}" if not reason else f"{reason}；模型返回文本: {brief_text[:100]}"

        return reason or "模型未给出图片或说明文本，可能是服务端策略或参数设置导致本次未产出图片"

    def generate_single_image(self, args):
        """
        [单任务执行者] 生成单张图片的完整工作流。
        
        注意：
            此方法设计为在独立线程中运行，因此接收一个 tuple 类型的 `args` 参数，
            而非多个独立参数（这是 `multiprocessing` 或某些线程池实现的常见模式，这里沿用）。
            
        Args:
            args: 包含所有必要参数的元组 (index, seed, api_key, prompt...)
            
        Returns:
            Dict: 包含 success, images(base64), tensor, text 等信息的字典。
        """
        # 解包参数（Python 3.8+ 支持这种自动解包，但为了兼容性和清晰度，这里显式解包）
        (
            i,
            current_seed,
            api_key,
            prompt,
            model_type,
            aspect_ratio,
            image_size,
            top_p,
            input_images_b64,
            timeout,
            stagger_delay,
            decode_workers,
            bypass_proxy,
            request_start_event,
            request_start_time_holder,
            request_start_lock,
            effective_base_url,
            verify_ssl,
            enable_google_search,
            route_choice,
            stage_tracker,
            cancel_event,
            limit_long_edge_str,
        ) = args

        # 闭包辅助函数：检查是否已被取消
        def _check_canceled():
            if cancel_event.is_set():
                raise comfy.model_management.InterruptProcessingException()
            self._ensure_not_interrupted()

        _check_canceled()
        self._apply_stagger_delay(i, stagger_delay)

        thread_id = threading.current_thread().name
        self.logger.info(f"批次 {i+1} 开始请求...")
        is_test_route = route_choice == "心宝❤新渠道"
        if stage_tracker is not None and is_test_route:
            stage_tracker.mark(i, "request")

        # 初始化 url_list，确保在异常捕获时也能访问
        url_list = []
        failed_download_urls = []

        try:
            # [新增] 高速渠道特殊处理：上传图片 + 模型映射
            mapped_model_type = model_type
            
            # 处理 VIP 模型名称映射
            if model_type in (
                "gemini-3-pro-image-preview-vip(高成本)",  # 兼容旧工作流
                "gemini-3-pro-image-preview-vip",
            ):
                if route_choice != "心宝❤新渠道":
                    raise RuntimeError("该 VIP 模型仅支持在“心宝❤新渠道”下使用，请切换线路或更换模型。")
                mapped_model_type = "gemini-3-pro-image-preview-vip"
            elif model_type == "gemini-3-pro-image-preview-svip":
                if route_choice != "心宝❤新渠道":
                    raise RuntimeError("该 SVIP 模型仅支持在“心宝❤新渠道”下使用，请切换线路或更换模型。")
                mapped_model_type = "gemini-3-pro-image-preview-svip"

            if route_choice == "心宝❤新渠道":
                # 1. 上传图片到公网

                if input_images_b64:
                    try:
                        # 解析 resize_limit
                        resize_limit = None
                        if limit_long_edge_str and limit_long_edge_str != "禁用":
                            try:
                                resize_limit = int(limit_long_edge_str)
                            except ValueError:
                                resize_limit = None

                        uploader = ImageUploader(logger=self.logger, interrupt_checker=self._ensure_not_interrupted)
                        new_input_images = []
                        for b64 in input_images_b64:
                            # 简单的判断：如果是 URL 则直接使用，否则上传
                            if b64.strip().startswith("http"):
                                new_input_images.append(b64)
                            elif resize_limit:
                                # [修改] 开启“限制长边”策略时：
                                # 仍然调用图床上传，但传入 resize_limit 以便在上传前进行缩放
                                # 并不调用 URL 缓存 (因为 limit 变了，hash 也会变)
                                img_bytes = base64.b64decode(b64)
                                url = uploader.upload(img_bytes, resize_limit=resize_limit)
                                new_input_images.append(url)
                            else:
                                # 未开启限制：保持原有逻辑 (上传图床)
                                img_bytes = base64.b64decode(b64)
                                url = uploader.upload(img_bytes, resize_limit=None)
                                new_input_images.append(url)
                        input_images_b64 = new_input_images
                    except Exception as e:
                        raise RuntimeError(f"高速渠道图片上传失败: {e}")



            request_data = self._prepare_request_payload(
                prompt=prompt,
                seed=current_seed,
                aspect_ratio=aspect_ratio,
                top_p=top_p,
                input_images_b64=input_images_b64,
                model_type=model_type, # 使用原模型名称进行 payload 预处理（保留尺寸检查等逻辑）
                image_size=image_size,
                enable_google_search=enable_google_search,
            )
            if is_test_route:
                self.logger.info(f"测试渠道-批次 {i+1}: 请求已发送，正在等待服务器响应...")
                sys.stdout.flush()
            self._record_request_start(request_start_event, request_start_time_holder, request_start_lock)

            # [修正] 实测 newapi 会过滤 URL 查询参数（如 ?output=url），因此改为写入请求体中。
            # 目标字段：generationConfig.imageConfig.output = "url"
            if route_choice == "心宝❤新渠道":
                generation_config = request_data.setdefault("generationConfig", {})
                image_config = generation_config.setdefault("imageConfig", {})
                image_config["output"] = "url"


            response_data = self.api_client.send_request(
                api_key,
                request_data,
                mapped_model_type,
                effective_base_url,
                timeout,
                bypass_proxy=bypass_proxy,
                verify_ssl=verify_ssl,
                verbose_error=(route_choice == "心宝❤新渠道"),
            )
            if is_test_route:
                self.logger.info(f"测试渠道-批次 {i+1}: 已收到返回体")
            if stage_tracker is not None and is_test_route:
                stage_tracker.mark(i, "response")
            _check_canceled()
            base64_images, text_content = self.api_client.extract_content(response_data)
            url_list = [] # 清空或重置，虽然主要是 test route 用
            if is_test_route:
                url_list = [
                    item for item in base64_images
                    if isinstance(item, str) and item.startswith("http")
                ]
                if url_list:
                    self.logger.info(f"测试渠道-批次 {i+1}: 解析到 {len(url_list)} 个图片链接")
                    for idx, url in enumerate(url_list, start=1):
                        self.logger.info(f"  -> 链接 {idx}: {url}")
                else:
                    self.logger.info(f"测试渠道-批次 {i+1}: 未解析到图片链接")
            if stage_tracker is not None and is_test_route:
                stage_tracker.mark(i, "download")

            # [新增] 高速渠道响应处理：下载 URL 图片
            if route_choice == "心宝❤新渠道" and base64_images:
                from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

                download_connect_timeout = 15
                download_read_timeout = 60
                if isinstance(timeout, (list, tuple)):
                    if len(timeout) >= 1 and isinstance(timeout[0], (int, float)) and timeout[0] > 0:
                        download_connect_timeout = timeout[0]
                    if len(timeout) >= 2 and isinstance(timeout[1], (int, float)) and timeout[1] > 0:
                        download_read_timeout = timeout[1]
                elif isinstance(timeout, (int, float)) and timeout > 0:
                    download_read_timeout = timeout
                download_timeout = (download_connect_timeout, download_read_timeout)

                def _download_worker(item_url_or_b64, worker_idx: int):
                    if isinstance(item_url_or_b64, str) and item_url_or_b64.startswith("http"):
                        try:
                            # 明确指定连接/读取超时，防止无限挂起
                            # 宽容校验 verify=False，适应部分代理/内网环境
                            resp = requests.get(item_url_or_b64, timeout=download_timeout, verify=False)
                            resp.raise_for_status()
                            return resp.content, worker_idx
                        except Exception as e:
                            # 包装异常以便主线程捕获
                            raise RuntimeError(f"下载失败[{worker_idx+1}]: {item_url_or_b64} (错误: {e})")
                    return item_url_or_b64, worker_idx

                # 获取需要下载的数量，用于进度提示
                total_to_download = len(base64_images)
                self.logger.info(f"测试渠道-批次 {i+1}: 开始下载 {total_to_download} 张图片")

                # 预分配结果列表，保证顺序
                downloaded_results = [None] * total_to_download

                # [关键修复] 手动管理线程池生命周期，配合 wait 及时退出
                # 使用 wait(timeout=0.2) 循环检查，确保能敏捷响应 ComfyUI 的取消信号
                executor = ThreadPoolExecutor(max_workers=min(8, total_to_download))
                try:
                    future_to_idx = {
                        executor.submit(_download_worker, url, idx): idx
                        for idx, url in enumerate(base64_images)
                    }

                    not_done_futures = set(future_to_idx.keys())
                    completed_count = 0

                    # 只要还有未完成的任务，就循环检查
                    while not_done_futures:
                        # 1. 优先检查取消信号 (最重要)
                        _check_canceled()

                        # 2. 非阻塞等待一小段时间 (0.2s)
                        done, not_done_futures = wait(not_done_futures, timeout=0.2, return_when=FIRST_COMPLETED)

                        # 3. 处理已完成的任务（per-future 容错，单张失败不中断整批）
                        for future in done:
                            idx = future_to_idx[future]
                            try:
                                data, _ = future.result()
                                downloaded_results[idx] = data
                            except Exception as dl_exc:
                                # 记录失败 URL，继续下载其余图片
                                failed_src = base64_images[idx] if idx < len(base64_images) else "unknown"
                                failed_download_urls.append(failed_src)
                                self.logger.warning(f"  -> 下载失败[{idx+1}]: {failed_src} ({dl_exc})")
                            completed_count += 1

                            if total_to_download > 1:
                                self.logger.info(f"  -> 下载进度: {completed_count}/{total_to_download}")

                except comfy.model_management.InterruptProcessingException:
                    raise
                except Exception as exc:
                    # 非下载异常（如取消信号包装），向上抛出
                    raise RuntimeError(f"高速渠道下载中断: {exc}")
                finally:
                    # 异步关闭，不等待未完成的任务（因为我们可能已经主动报错或点击了取消）
                    executor.shutdown(wait=False)

                # 过滤掉下载失败的 None 项，仅解码成功下载的图片
                base64_images = [r for r in downloaded_results if r is not None]

            decoded_tensor, decoded_count = self._decode_images(
                base64_images,
                thread_id,
                i,
                decode_workers,
            )

            # 更明显地区分"有图返回"和"未返回任何图片"的情况
            if decoded_count > 0:
                self.logger.success(f"批次 {i+1} 完成 - 生成 {decoded_count} 张图片")
            else:
                reason = self._build_no_image_reason(response_data, text_content)
                self.logger.warning(f"批次 {i+1} 完成，但未返回任何图片。{reason}")

            if failed_download_urls:
                self.logger.warning(f"批次 {i+1}: {len(failed_download_urls)} 张图片下载失败")

            return {
                "index": i,
                "success": True,
                "images": base64_images,
                "tensor": decoded_tensor,
                "image_count": decoded_count,
                "text": text_content,
                "seed": current_seed,
                "generated_urls": url_list,
                "failed_download_urls": failed_download_urls,
            }
        except comfy.model_management.InterruptProcessingException:
            cancel_event.set() # 确保信号被设置
            # 不再打印警告日志，保持控制台整洁，由主流程统一处理
            raise
        except Exception as e:
            error_msg = str(e)
            self.logger.error(f"批次 {i+1} 失败")
            self.logger.error(f"错误: {error_msg}")
            return self._build_failure_result(i, current_seed, error_msg, generated_urls=url_list, failed_download_urls=failed_download_urls)

    def generate_images(
        self,
        binding_context=None,
        prompt="Peace and love",
        banana_api_key="",
        model_type="gemini-3-pro-image-preview",
        batch_size=1,
        aspect_ratio="Auto",
        image_size="2K",
        seed=-1,
        top_p=0.95,
        max_workers=None,
        image_1=None,
        image_2=None,
        image_3=None,
        image_4=None,
        image_5=None,
        联网搜索=False,
        绕过代理=None,
        线路="香港专线",
        禁用SSL验证=False,
        大于5M限制长边="禁用",
        _suppress_url_summary_log=False,
        _return_url_metadata=False,
    ):
        """
        [主入口] ComfyUI 调用此方法开始执行节点逻辑。
        
        Parameters:
            binding_context: 上游传递的绑定信息，用于权限校验。
            banana_api_key: 用户输入的 Key，为空则使用 Config。
            batch_size: 批次大小 (1~8)。
            seed: 随机种子，-1 表示随机。
            ... (其他参数对应 UI 控件)
            
        Returns:
            Tuple(IMAGE, STRING): 生成的图片 Tensor、汇总日志文本。
            当 _return_url_metadata=True 时返回 5-tuple，额外包含 generated_urls 和 failed_download_urls 列表。
            _suppress_url_summary_log 仅用于控制是否在日志中打印 URL 汇总，不影响返回结构。
        """

        log = self.logger
        route_choice = 线路

        # 重置/初始化节点执行周期的取消信号
        self.cancel_event.clear()

        # [兼容性处理] 旧渠道名称自动映射到新渠道（静默兼容旧工作流）
        normalized_route_choice = self.config_manager.normalize_xinbao_test_route_label(route_choice)
        if normalized_route_choice != route_choice:
            log.warning(f"检测到旧渠道名称 '{route_choice}'，已自动重定向至 '心宝❤新渠道'")
            route_choice = normalized_route_choice

        try:
            resolved_api_key, effective_base_url, key_source = self._resolve_api_key_and_base(
                banana_api_key, route_choice
            )
            log.info(f"API Key 已解析（来源: {key_source}）")
        except RuntimeError as exc:
            error_msg = str(exc)
            log.error(error_msg)
            error_tensor = self.error_canvas.build_error_tensor_from_text(
                "配置缺失",
                error_msg,
            )
            self._halt_with_error(error_tensor, error_msg)

        # 若提供了绑定上下文，先在本地校验签名与有效期，
        # 确保上下文确实来自官方绑定生成节点。
        if binding_context is not None:
            try:
                validate_pending(binding_context)
            except BindingError as exc:
                error_msg = f"绑定校验失败：{exc}"
                log.error(error_msg)
                error_tensor = self.error_canvas.build_error_tensor_from_text(
                    "绑定失败",
                    f"{exc}\n{build_missing_hint()}",
                )
                self._halt_with_error(error_tensor, error_msg)
            except Exception:
                error_msg = "绑定校验失败，请使用“心宝❤绑定生成”节点重新连接。"
                log.error(error_msg)
                error_tensor = self.error_canvas.build_error_tensor_from_text(
                    "绑定失败",
                    f"{error_msg}\n{build_missing_hint()}",
                )
                self._halt_with_error(error_tensor, error_msg)

        # 绕过代理完全由节点开关控制，不再读取 config.ini
        bypass_proxy_flag = bool(绕过代理)
        disable_ssl_flag = bool(禁用SSL验证)
        verify_ssl_flag = not disable_ssl_flag
        if disable_ssl_flag:
            log.warning("已禁用 SSL 证书验证，请确保你信任当前网络环境，以免密钥被中间人窃取")

        # Edge KV 预检：先检查 10 分钟缓存，再视配置调用远端
        # [新增] 心宝测试渠道跳过 KV 验证
        if route_choice == "心宝❤新渠道":
            self.logger.info("测试渠道， KV 验证通过")
            kv_result = type("KVResult", (), {"allowed": True, "fail_open": False, "user_message": ""})()
        else:
            kv_result = self.kv_auth_client.check(
                resolved_api_key,
                verify_ssl=verify_ssl_flag,
                disable_ssl_override=disable_ssl_flag,
            )
        if not kv_result.allowed:
            error_msg = kv_result.user_message or self._ERR_AUTH_UNAVAILABLE
            log.error(error_msg)
            error_tensor = self.error_canvas.build_error_tensor_from_text(
                "访问受限",
                error_msg,
            )
            # 直接中断工作流，避免后续节点继续执行浪费时间
            self._halt_with_error(error_tensor, error_msg)
        if kv_result.fail_open:
            log.warning("Edge KV 校验异常已按 fail-open 放行，请确认服务状态")
        balance_summary = None
        if resolved_api_key:
            balance_summary = self.balance_service.get_cached_balance_text(
                effective_base_url,
                resolved_api_key,
            )

        # --- 智能缓存逻辑开始 ---
        # 计算实质性参数的哈希值（排除 binding_context，因为它会随时间自动刷新）
        # 目的：当上游绑定节点因过期而强制刷新时，如果本节点的生成参数没变，
        # 则直接复用上一次的生成结果，避免不必要的扣费和等待。
        import hashlib
        
        # 提取用于计算哈希的参数字典
        cache_key_params = {
            "prompt": prompt,
            "banana_api_key": resolved_api_key, # 使用解析后的 Key，确保一致性
            "model_type": model_type,
            "batch_size": batch_size,
            "aspect_ratio": aspect_ratio,
            "image_size": image_size,
            "seed": seed,
            "top_p": top_p,
            "enable_google_search": bool(联网搜索),
            "bypass_proxy": bypass_proxy_flag,
            "route": route_choice,
            "disable_ssl": disable_ssl_flag,
            "limit_long_edge": 大于5M限制长边, # [新增] 纳入缓存计算
            # 图片输入也需要纳入哈希，这里简单使用对象的 id 或形状信息
            # 更严谨的做法是计算 tensor 的哈希，但考虑到性能，
            # 若用户没换图片节点，id 通常不变；若换了图片内容，通常会触发上游重跑导致 id 变化。
            # 这里为了保险，尝试读取 tensor 的 shape 和 sum（轻量级校验）
            # 这样既能感知图片内容的大幅变化，又不会像 sha256 整个 tensor 那样耗时
            "image_inputs": [] 
        }
        
        raw_input_images = [image_1, image_2, image_3, image_4, image_5]
        input_tensors = []
        for img in raw_input_images:
            if img is not None:
                input_tensors.append(img)
                # 记录形状和简单的校验和，防止图片内容微调但对象没变的情况（虽然 ComfyUI 通常会生成新对象）
                cache_key_params["image_inputs"].append(
                    (img.shape, float(img.sum().item()))
                )

        # 序列化并计算哈希
        try:
            # sort_keys=True 保证字典顺序一致
            serialized_params = json.dumps(cache_key_params, sort_keys=True, default=str)
            current_hash = hashlib.sha256(serialized_params.encode("utf-8")).hexdigest()
        except Exception as e:
            log.warning(f"计算缓存哈希失败，将跳过缓存逻辑: {e}")
            current_hash = None

        # 检查缓存
        # 当 seed 为 -1 (自动随机) 时跳过缓存，强制重新生成
        if seed != -1 and current_hash and getattr(self, "_LAST_GENERATION_CACHE", None):
            last_cache = self._LAST_GENERATION_CACHE
            if last_cache.get("hash") == current_hash:
                log.success("♻️ 检测到参数未变，复用上一次生成结果（智能缓存）")
                
                # [关键逻辑说明]
                # 即使复用缓存，也必须激活绑定上下文！
                # 原因：ComfyUI 的某些后置节点（如“绑定发送”）可能会校验上下文是否处于 "activated" 状态。
                # 如果我们直接返回缓存而不激活 Binding，下游节点就会报错。
                if binding_context is not None:
                    try:
                        activate_binding(binding_context)
                        log.info("已为缓存结果激活新的绑定上下文")
                    except Exception as exc:
                        log.warning(f"绑定激活失败: {exc}")

                if _return_url_metadata:
                    cached_failed_urls_only = last_cache.get("failed_urls_only", "")
                    cached_failed_urls = [
                        line.strip()
                        for line in cached_failed_urls_only.split("\n")
                        if line.strip()
                    ]
                    return (
                        last_cache["tensor"],
                        last_cache["text"],
                        [],
                        cached_failed_urls,
                        cached_failed_urls_only,
                    )

                return (last_cache["tensor"], last_cache["text"])

        # --- 智能缓存逻辑结束 ---

        start_time = time.time()
        encoded_input_images = self.image_codec.prepare_input_images(input_tensors)

        # [新增] 图床上传优化 (针对高速渠道)
        # 1. 批次复用：在此处统一上传，生成得到的 URL 会被所有子任务共享，避免 ImageUploader 在每个线程中重复上传
        # 2. 全局复用：ImageUploader 内部已实现 MD5 缓存，跨批次/跨节点的相同图片也会命中缓存
        if route_choice == "心宝❤新渠道" and encoded_input_images:
            try:
                # 解析 resize_limit
                resize_limit = None
                if 大于5M限制长边 and 大于5M限制长边 != "禁用":
                    try:
                        resize_limit = int(大于5M限制长边)
                    except ValueError:
                        resize_limit = None
                
                # 即使 limit 没开启，resize_limit 为 None，也照样上传，保持原有逻辑

                # 实例化上传器 (复用 logger)
                uploader = ImageUploader(logger=self.logger, interrupt_checker=self._ensure_not_interrupted)
                
                # 并发上传以提高多图场景下的速度
                from concurrent.futures import ThreadPoolExecutor
                
                def _do_upload(b64_str):
                    if b64_str.strip().startswith("http"):
                        return b64_str
                    try:
                        img_bytes = base64.b64decode(b64_str)
                        # 传入 resize_limit
                        return uploader.upload(img_bytes, resize_limit=resize_limit)
                    except Exception as exc:
                        raise RuntimeError(f"图片上传失败: {exc}")

                with ThreadPoolExecutor(max_workers=5) as executor:
                    # map 会按输入顺序返回结果，保证图片顺序不变
                    results = executor.map(_do_upload, encoded_input_images)
                    new_encoded_images = list(results)
                    
                encoded_input_images = new_encoded_images
                log.info(f"高速渠道：已并发完成 {len(encoded_input_images)} 张图片的上传/缓存获取")
            except Exception as e:
                # 若上传整体失败，记录错误但尝试继续（generate_single_image 内有兜底，虽然可能会再次失败）
                log.warning(f"高速渠道批量上传失败，将回退到单任务上传尝试: {e}")

        # 固定配置
        concurrent_mode = True   # 总是开启并发，提高批量生成效率
        # 为网络请求增加轻微交错延迟, 避免瞬间向 API 发起大量请求导致触发限流 (Rate Limit)
        stagger_delay = 1.0      # 每个批次相对前一个延迟 1.0 秒
        # 拆分网络超时：连接(15s) + 读取(420s)，保持统一策略以简化分支
        connect_timeout = 15
        # gemini-3-pro-image 和 gemini-3.1-flash-image-preview 系列生成时长较久，统一放宽读取超时到 420s
        read_timeout = 420
        request_timeout = (connect_timeout, read_timeout)
        continue_on_error = True  # 总是容错
        configured_workers = self.config_manager.load_max_workers()
        decode_workers = max(1, configured_workers)
        request_start_event = threading.Event()
        request_start_time_holder: List[Optional[float]] = [None]
        request_start_lock = threading.Lock()
        stage_tracker = None

        if route_choice == "心宝❤新渠道":

            stage_names = ("request", "response", "download", "complete")

            stage_tracker = _StageProgressTracker(

                comfy.utils.ProgressBar(batch_size * len(stage_names)),

                batch_size,

                stage_names,

            )


        if seed == -1:
            base_seed = random.randint(0, 102400)
        else:
            base_seed = seed

        decoded_tensors: List[torch.Tensor] = []
        total_generated_images = 0
        all_texts: List[str] = []
        results: List[Dict[str, Any]] = []
        tasks: List[Tuple[Any, ...]] = []

        for i in range(batch_size):
            current_seed = base_seed + i if seed != -1 else -1
            tasks.append((
                i,
                current_seed,
                resolved_api_key,
                prompt,
                model_type,
                aspect_ratio,
                image_size,
                top_p,
                encoded_input_images,
                request_timeout,
                stagger_delay,
                decode_workers,
                bypass_proxy_flag,
                request_start_event,
                request_start_time_holder,
                request_start_lock,
                effective_base_url,
                verify_ssl_flag,
                bool(联网搜索),
                route_choice,
                stage_tracker,
                self.cancel_event, # 传入本地取消信号
                大于5M限制长边,   # [新增] 传入配置字符串
            ))

        # 显示任务开始信息
        log.header("🎨 Gemini 图像生成任务")
        log.info(f"批次数量: {batch_size} 张")
        log.info(f"图片比例: {aspect_ratio}")
        if seed != -1:
            log.info(f"随机种子: {seed}")
        if top_p != 0.95:
            log.info(f"Top-P 参数: {top_p}")
        log.separator()

        configured_network_cap = self.config_manager.load_network_workers_cap()
        network_workers_cap = min(configured_workers, configured_network_cap)
        actual_workers = min(network_workers_cap, batch_size) if concurrent_mode and batch_size > 1 else 1
        actual_workers = max(1, actual_workers)

        def progress_callback(result: Dict[str, Any], completed_count: int, total_count: int, progress_bar: object):
            if result.get('success'):
                log.success(
                    f"[{completed_count}/{total_count}] 批次 {result['index']+1} 完成"
                )
            else:
                batch_label = result.get('index', -1)
                batch_text = "?" if batch_label < 0 else batch_label + 1
                log.error(
                    f"[{completed_count}/{total_count}] 批次 {batch_text} 失败"
                )

            preview_tensor = result.get('tensor')
            if result.get('success') and preview_tensor is not None:
                preview_tuple = self.image_codec.build_preview_tuple(
                    preview_tensor, result['index']
                )
            else:
                preview_tuple = None

            if stage_tracker is not None:
                batch_index = result.get('index', -1)
                if not isinstance(batch_index, int) or batch_index < 0 or batch_index >= batch_size:
                    batch_index = 0
                stage_tracker.complete_batch(batch_index, preview_tuple)
                return

            if preview_tuple is not None:
                progress_bar.update_absolute(completed_count, total_count, preview_tuple)
            else:
                progress_bar.update(1)

        results = self.task_runner.run(
            tasks,
            self.generate_single_image,
            batch_size,
            actual_workers,
            continue_on_error,
            progress_callback,
        )
        request_start_time = request_start_time_holder[0] or start_time

        if not results:
            elapsed = time.time() - request_start_time
            if self.cancel_event.is_set():
                error_text = f"任务已由用户取消\n总耗时: {elapsed:.2f}s"
            else:
                error_text = f"未生成任何图像\n总耗时: {elapsed:.2f}s"
            
            if balance_summary:
                error_text = f"{balance_summary}\n\n{error_text}"
            log.error(error_text)
            error_tensor = self.error_canvas.build_error_tensor_from_text("生成失败", error_text)
            self._halt_with_error(error_tensor, error_text)

        results.sort(key=lambda x: x['index'])

        for result in results:
            if result.get('success'):
                tensor = result.get('tensor')
                if tensor is not None:
                    decoded_tensors.append(tensor)
                    total_generated_images += result.get('image_count', tensor.shape[0])
                if result.get('text'):
                    all_texts.append(f"[批次 {result['index']+1}] {result['text']}")
            else:
                error_msg = f"[批次 {result['index']+1}] ❌ {result.get('error', '未知错误')}"
                all_texts.append(error_msg)
                if not continue_on_error:
                    break

        total_time = time.time() - request_start_time

        if not decoded_tensors or total_generated_images == 0:
            error_text = f"未生成任何图像\n总耗时: {total_time:.2f}s\n\n" + "\n".join(all_texts)
            if balance_summary:
                error_text = f"{balance_summary}\n\n{error_text}"
            log.error(error_text)
            error_tensor = self.error_canvas.build_error_tensor_from_text("生成失败", error_text)
            self._halt_with_error(error_tensor, error_text)

        # 至少有一张图像成功生成时，如提供了绑定上下文，则在此时标记会话已激活，
        # 确保只有在本节点真实成功调用服务的前提下，后置增强节点才能通过 require_activated 校验。
        if binding_context is not None:
            try:
                activate_binding(binding_context)
            except Exception as exc:  # 绑定激活失败不影响本次生成结果，只影响后置节点能否使用
                log.warning(f"绑定会话激活失败，将仅影响后置增强节点：{exc}")

        if len(decoded_tensors) == 1:
            image_tensor = decoded_tensors[0]
        else:
            image_tensor = torch.cat(decoded_tensors, dim=0)

        actual_count = total_generated_images
        ratio_text = "自动" if aspect_ratio == "Auto" else aspect_ratio
        success_info = f"✅ 成功生成 {actual_count} 张图像（比例: {ratio_text}）"
        avg_time = total_time / actual_count if actual_count > 0 else 0
        time_info = f"总耗时: {total_time:.2f}s，平均 {avg_time:.2f}s/张"
        if actual_count != batch_size:
            time_info += f" ⚠️ 请求{batch_size}张，实际生成{actual_count}张"
            # 若实际生成数量少于请求数量，在日志中额外给出明显提示
            log.warning(f"部分批次未返回图片：请求 {batch_size} 张，实际上只生成 {actual_count} 张，请查看上方各批次日志中的“未返回任何图片”提示")

        combined_text = f"{success_info}\n{time_info}"
        if all_texts:
            combined_text += "\n\n" + "\n".join(all_texts)
        if balance_summary:
            combined_text = f"{balance_summary}\n\n{combined_text}"

        # 显示完成统计
        log.summary("任务完成", {
            "总批次": f"{batch_size} 个",
            "成功生成": f"{actual_count} 张",
            "总耗时": f"{total_time:.2f}s",
            "平均速度": f"{avg_time:.2f}s/张"
        })

        # [新增] 汇总输出所有生成的图片链接
        all_generated_urls = []
        all_failed_download_urls = []
        for result in results:
            # 无论成功与否，只要解析到了 URL 都收集
            urls = result.get("generated_urls")
            if urls:
                all_generated_urls.extend(urls)
            failed = result.get("failed_download_urls")
            if failed:
                all_failed_download_urls.extend(failed)

        if all_generated_urls:
            if not _suppress_url_summary_log:
                # 非并发模式：直接打印本次调用的 URL 汇总
                log.separator()
                log.info(f"🔗 本次任务生成的图片链接汇总 ({len(all_generated_urls)} 个):")
                for idx, url in enumerate(all_generated_urls, start=1):
                    log.info(f"  [{idx}] {url}")
                log.separator()

            # [新增] 将链接汇总也添加到输出文本中（前端输出不受 suppress 影响）
            url_text = f"🔗 图片链接汇总 ({len(all_generated_urls)} 个):\n" + "\n".join(
                [f"[{idx}] {url}" for idx, url in enumerate(all_generated_urls, start=1)]
            )
            combined_text += "\n\n" + url_text

        # 构建下载失败 URL 的纯文本（用于新输出端口）
        if all_failed_download_urls:
            if not _suppress_url_summary_log:
                log.separator()
                log.warning(f"⚠️ 下载失败图片链接汇总 ({len(all_failed_download_urls)} 个):")
                for idx, url in enumerate(all_failed_download_urls, start=1):
                    log.warning(f"  [{idx}] {url}")
                log.separator()

            failed_url_text = f"⚠️ 下载失败图片链接 ({len(all_failed_download_urls)} 个):\n" + "\n".join(
                [f"[{idx}] {url}" for idx, url in enumerate(all_failed_download_urls, start=1)]
            )
            combined_text += "\n\n" + failed_url_text
        else:
            failed_url_text = ""

        # 下载失败 URL 纯列表（仅 URL，每行一个，供新输出端口使用）
        failed_urls_only = "\n".join(all_failed_download_urls)

        # 更新缓存
        if current_hash:
            self._LAST_GENERATION_CACHE = {
                "hash": current_hash,
                "tensor": image_tensor,
                "text": combined_text,
                "failed_urls_only": failed_urls_only,
            }

        # [修复] 给前端足够时间接收最后的进度条/预览更新
        # 并发模式下，progress_callback 的 WebSocket 消息可能还在传输中，
        # 若此时立即 return，ComfyUI 主循环会截断消息队列，导致前端卡住
        time.sleep(0.3)

        if _return_url_metadata:
            # 通过扩展元组返回 URL 元信息，避免依赖实例属性引入竞态。
            return (image_tensor, combined_text, all_generated_urls, all_failed_download_urls, failed_urls_only)

        return (image_tensor, combined_text)

# 注册节点
class BananaImageNodeV2(BananaImageNode):
    """
    [V2 节点] 用于支持更高级的 Gemini 特性。
    
    变更点:
        1. 继承自 BananaImageNode，复用大部分核心逻辑。
        2. UI 层面增加了 SYSTEM_INSTRUCTION (系统提示词) 的预设和输入框。
        3. 可能会开启 Google Search 等高级工具（视 API 支持情况）。
        4. 保持 V1 节点不变，确保旧工作流兼容性。
    """
    CATEGORY = "❤️‍🔥心宝专用"
    FUNCTION = "generate_images_async"
    RETURN_TYPES = ("IMAGE", "STRING", "STRING")
    RETURN_NAMES = ("images", "text", "failed_urls")

    @classmethod
    def INPUT_TYPES(cls):
        # 复用 V1 的基础配置
        inputs = super().INPUT_TYPES()
        # 深度复制一份，避免污染父类引用
        import copy
        inputs = copy.deepcopy(inputs)
        
        # 注入 V2 独有的“联网搜索”开关
        inputs["optional"]["联网搜索"] = (
            "BOOLEAN",
            {
                "default": False,
                "tooltip": "开启后模型可联网搜索辅助生成（注意：生图模型可能会忽略此工具）",
            },
        )

        inputs["optional"]["启用工作流并发"] = (
            "BOOLEAN",
            {
                "default": False,
                "tooltip": "启用后，多个 BananaV2/视频节点可在同一工作流中并发发起 API 调用；默认关闭以保持更保守的资源占用。开启并发时单节点失败不会终止整图。",
            },
        )
        
        # [新增] 图片压缩配置 (仅针对测试渠道+大图)
        inputs["optional"]["大于5M限制长边"] = (
            ["禁用", "2048", "3072"],
            {
                "default": "禁用",
                "tooltip": "上传图片超过5MB时的处理策略：禁用则仅转格式(JPEG q90)，开启则强制缩放长边至指定像素",
            },
        )
        return inputs

    async def generate_images_async(self, 启用工作流并发: bool = False, **kwargs):
        """
        ComfyUI 并发 API 节点入口（协程）。

        - 通过 `await asyncio.to_thread(...)` 让出事件循环，触发 ComfyUI pending_async_nodes 并发调度。
        - 默认关闭并发时，使用跨节点互斥锁使同类节点更保守（串行启动）。
        - 开启并发时，捕获异常并阻断自身输出，避免单节点失败终止整图。

        Returns:
            (IMAGE, STRING, STRING): 图片 Tensor、汇总文本、下载失败 URL 文本
        """
        enable_parallel = bool(启用工作流并发)
        payload = dict(kwargs)
        payload.pop("启用工作流并发", None)

        if not enable_parallel:
            # 让出一次事件循环 tick，尽量让开启并发的节点先启动（语义 A：先开始）。
            await asyncio.sleep(0)
            async with get_non_parallel_workflow_lock():
                # 明确请求 URL 元信息，确保 non-parallel 也能输出 failed_urls。
                raw = await asyncio.to_thread(
                    self.generate_images, _return_url_metadata=True, **payload
                )
                if isinstance(raw, tuple) and len(raw) == 5:
                    return (raw[0], raw[1], raw[4])
                if isinstance(raw, tuple) and len(raw) == 3:
                    return (raw[0], raw[1], raw[2])
                # fallback：兼容异常路径
                return (raw[0], raw[1], "") if isinstance(raw, tuple) else (raw, "", "")

        try:
            self.logger.info("已启用工作流并发：BananaV2 将并发发起 API 调用")
            async with get_workflow_parallel_shared_lock_with_url_collect() as ctx:
                raw_result = await asyncio.to_thread(
                    self.generate_images,
                    _suppress_url_summary_log=True,
                    _return_url_metadata=True,
                    **payload
                )
                # 并发模式返回 5-tuple: (tensor, text, gen_urls, failed_urls, failed_urls_only)
                if isinstance(raw_result, tuple) and len(raw_result) == 5:
                    result = (raw_result[0], raw_result[1], raw_result[4])
                    gen_urls = raw_result[2]
                    failed_urls = raw_result[3]
                else:
                    result = (raw_result[0], raw_result[1], "") if isinstance(raw_result, tuple) else (raw_result, "", "")
                    gen_urls = []
                    failed_urls = []
                if gen_urls and ctx["collector"] is not None:
                    ctx["collector"].add(gen_urls)
                if failed_urls and ctx["collector"] is not None:
                    ctx["collector"].add_failed(failed_urls)

            # 退出 shared lock 后，检查是否为最后一个完成的调用
            consolidated = ctx.get("consolidated_urls")
            consolidated_failed = ctx.get("consolidated_failed_urls")
            log = self.logger

            if consolidated:
                log.separator()
                log.info(f"🔗 工作流并发任务图片链接汇总 ({len(consolidated)} 个):")
                for idx, url in enumerate(consolidated, start=1):
                    log.info(f"  [{idx}] {url}")
                log.separator()

            if consolidated_failed:
                log.separator()
                log.warning(f"⚠️ 工作流并发任务下载失败链接汇总 ({len(consolidated_failed)} 个):")
                for idx, url in enumerate(consolidated_failed, start=1):
                    log.warning(f"  [{idx}] {url}")
                log.separator()

            return result
        except comfy.model_management.InterruptProcessingException:
            raise
        except Exception as exc:
            # 并发模式：失败隔离，不让异常冒泡导致整图失败
            brief = str(exc or "").strip()
            if len(brief) > 300:
                brief = brief[:300] + "…"
            msg = f"BananaV2 并发模式执行失败：{brief or '未知错误'}"

            # [修改] 不再返回 blocker，而是返回错误图片，保证 SaveImage 不会停止
            self.logger.error(msg)
            error_tensor = self.error_canvas.build_error_tensor_from_text("并发执行失败", msg)
            return (error_tensor, msg, "")


NODE_CLASS_MAPPINGS = {
    "BananaImageNode": BananaImageNode,
    "BananaImageNodeV2": BananaImageNodeV2,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BananaImageNode": "心宝❤Banana",
    "BananaImageNodeV2": "心宝❤BananaV2",
}

BALANCE_SERVICE.ensure_route(lambda: getattr(PromptServer, "instance", None))
