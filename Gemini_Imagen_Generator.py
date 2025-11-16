from __future__ import annotations

import json
import torch
from typing import List, Dict, Optional, Tuple, Any
import re
import random
import time
import threading
import os
import sys
from datetime import datetime

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

# 在非 ComfyUI 运行环境中,server 可能无法正常导入
# 这里做一个兼容处理:导入失败时提供一个占位 PromptServer,
# 仅用于避免测试脚本导入本模块时报错
try:
    from server import PromptServer
except ImportError:
    class _DummyPromptServer:
        instance = None
    PromptServer = _DummyPromptServer()

import comfy.utils
import comfy.model_management

from logger import logger
from config_manager import ConfigManager
from balance_service import BalanceService
from image_codec import ImageCodec, ErrorCanvas
from api_client import GeminiApiClient
from task_runner import BatchGenerationRunner


CONFIG_MANAGER = ConfigManager(MODULE_DIR)
API_CLIENT = GeminiApiClient(CONFIG_MANAGER, logger)
BALANCE_SERVICE = BalanceService(API_CLIENT, CONFIG_MANAGER, logger)

class BananaImageNode:
    """
    ComfyUI节点: NanoBanana图像生成，适配Gemini兼容端点
    支持从config.ini读取API Key
    """

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "text")
    FUNCTION = "generate_images"
    OUTPUT_NODE = True
    CATEGORY = "image/ai_generation"

    def __init__(self):
        self.config_manager = CONFIG_MANAGER
        self.image_codec = ImageCodec(logger, self._ensure_not_interrupted)
        self.error_canvas = ErrorCanvas(logger)
        self.balance_service = BALANCE_SERVICE
        self.task_runner = BatchGenerationRunner(
            logger,
            self._ensure_not_interrupted,
            lambda total: comfy.utils.ProgressBar(total),
        )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {
                    "multiline": True,
                    "default": "Peace and love"
                }),
                "api_key": ("STRING", {
                    "default": "",
                    "multiline": False
                }),
                "model_type": ("STRING", {
                    "default": "gemini-2.5-flash-image"
                }),
                "batch_size": ("INT", {
                    "default": 1, "min": 1, "max": 8
                }),
                "aspect_ratio": (["Auto", "1:1", "9:16", "16:9", "21:9", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4"], {
                    "default": "Auto"
                }),
            },
            "optional": {
                "seed": ("INT", {
                    "default": -1,
                    "min": -1,
                    "max": 102400,
                    "control_after_generate": True
                }),
                "top_p": ("FLOAT", {
                    "default": 0.95, "min": 0.0, "max": 1.0, "step": 0.01
                }),
                "image_1": ("IMAGE",),
                "image_2": ("IMAGE",),
                "image_3": ("IMAGE",),
                "image_4": ("IMAGE",),
                "image_5": ("IMAGE",),
            }
        }
    
    @staticmethod
    def _ensure_not_interrupted():
        """统一的中断检查，复用 ComfyUI 原生取消机制"""
        comfy.model_management.throw_exception_if_processing_interrupted()

    def _build_failure_result(self, index: int, seed: int, error_msg: str) -> Dict[str, Any]:
        """构造统一的失败返回结构，便于上层聚合处理"""
        return {
            "index": index,
            "success": False,
            "error": error_msg,
            "seed": seed,
            "tensor": None,
            "image_count": 0,
        }

    def generate_single_image(self, args):
        """生成单张图片（用于并发）"""
        (
            i,
            current_seed,
            api_key,
            prompt,
            model_type,
            aspect_ratio,
            top_p,
            input_images_b64,
            timeout,
            stagger_delay,
            decode_workers,
        ) = args

        self._ensure_not_interrupted()
        if stagger_delay > 0:
            delay = i * stagger_delay
            if delay > 0:
                time.sleep(delay)

        thread_id = threading.current_thread().name
        logger.info(f"批次 {i+1} 开始请求...")

        try:
            self._ensure_not_interrupted()
            request_data = API_CLIENT.create_request_data(
                prompt,
                current_seed,
                aspect_ratio,
                top_p,
                input_images_b64
            )
            self._ensure_not_interrupted()
            effective_base_url = self.config_manager.get_effective_api_base_url()
            response_data = API_CLIENT.send_request(
                api_key,
                request_data,
                model_type,
                effective_base_url,
                timeout
            )
            self._ensure_not_interrupted()
            base64_images, text_content = API_CLIENT.extract_content(response_data)
            decoded_tensor = None
            decoded_count = 0
            if base64_images:
                self._ensure_not_interrupted()
                decoded_tensor = self.image_codec.base64_to_tensor_parallel(
                    base64_images,
                    log_prefix=f"[{thread_id}] 批次 {i+1}",
                    max_workers=decode_workers
                )
                decoded_count = decoded_tensor.shape[0]

            # 更明显地区分“有图返回”和“未返回任何图片”的情况
            if decoded_count > 0:
                logger.success(f"批次 {i+1} 完成 - 生成 {decoded_count} 张图片")
            else:
                # 简化日志输出,尽可能给出用户能理解的原因说明
                reason = ""
                # 1. 检查 finishReason 信息
                try:
                    if isinstance(response_data, dict):
                        candidates = response_data.get("candidates") or []
                        if candidates and isinstance(candidates[0], dict):
                            finish_reason = candidates[0].get("finishReason") or ""
                            if finish_reason:
                                if finish_reason == "NO_IMAGE":
                                    reason = "模型未生成任何图片（finishReason=NO_IMAGE，一般表示当前提示或参考图不触发图像输出，可能是内容被过滤或未通过安全审查）"
                                else:
                                    reason = f"模型未生成图片（finishReason={finish_reason}）"
                except Exception:
                    # 如果解析 finishReason 失败,忽略即可
                    pass

                # 2. 如果有文本内容,补充展示一小段
                brief_text = (text_content or "").strip().replace("\n", " ")
                if brief_text:
                    if reason:
                        reason = f"{reason}；模型返回文本: {brief_text[:100]}"
                    else:
                        reason = f"模型仅返回文本: {brief_text[:100]}"

                # 3. 都没有就给一个通用说明
                if not reason:
                    reason = "模型未给出图片或说明文本，可能是服务端策略或参数设置导致本次未产出图片"

                logger.warning(f"批次 {i+1} 完成，但未返回任何图片。{reason}")

            return {
                'index': i,
                'success': True,
                'images': base64_images,
                'tensor': decoded_tensor,
                'image_count': decoded_count,
                'text': text_content,
                'seed': current_seed
            }
        except comfy.model_management.InterruptProcessingException:
            logger.warning(f"批次 {i+1} 已取消")
            raise
        except Exception as e:
            error_msg = str(e)[:200]
            logger.error(f"批次 {i+1} 失败")
            logger.error(f"错误: {error_msg}")
            return self._build_failure_result(i, current_seed, error_msg)

    def generate_images(self, prompt, api_key="", model_type="gemini-2.5-flash-image",
                       batch_size=1, aspect_ratio="Auto", seed=-1, top_p=0.95, max_workers=None,
                       image_1=None, image_2=None, image_3=None,
                       image_4=None, image_5=None):

        # 解析 API Key：优先使用节点输入，留空时回退 config
        sanitized_input_key = self.config_manager.sanitize_api_key(api_key)
        resolved_api_key = sanitized_input_key or self.config_manager.sanitize_api_key(
            self.config_manager.load_api_key()
        )

        # 验证API key
        if not resolved_api_key:
            error_msg = "请在 config.ini 中配置 API Key 或在节点中填写"
            logger.error(error_msg)
            error_tensor = self.error_canvas.build_error_tensor_from_text(
                "配置缺失",
                f"{error_msg}\n请在 config.ini 或节点输入中填写有效 API Key"
            )
            return (error_tensor, error_msg)

        # 统一使用内部隐藏的 Base URL（不接受前端传入）
        effective_base_url = self.config_manager.get_effective_api_base_url()

        cost_factor = self.config_manager.load_cost_factor()
        balance_summary = self.balance_service.get_cached_balance_text(effective_base_url, resolved_api_key, cost_factor)

        start_time = time.time()
        raw_input_images = [image_1, image_2, image_3, image_4, image_5]
        input_tensors = [img for img in raw_input_images if img is not None]
        encoded_input_images = self.image_codec.prepare_input_images(input_tensors)

        # 固定配置
        concurrent_mode = True   # 总是开启并发
        # 为网络请求增加轻微交错延迟,减少瞬时请求尖峰
        stagger_delay = 0.2      # 每个批次相对前一个延迟 0.2 秒
        # 拆分网络超时：连接(20s) + 读取(90s)
        # 连接超时设置为20s，在代理/不稳定网络下更宽容
        # 读取超时保持90s，因为图像生成确实需要时间
        connect_timeout = 20
        read_timeout = 90
        request_timeout = (connect_timeout, read_timeout)
        continue_on_error = True  # 总是容错
        configured_workers = self.config_manager.load_max_workers()
        decode_workers = max(1, configured_workers)

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
            tasks.append((i, current_seed, resolved_api_key, prompt, model_type, aspect_ratio,
                          top_p, encoded_input_images, request_timeout, stagger_delay,
                          decode_workers))

        # 显示任务开始信息
        logger.header("🎨 Gemini 图像生成任务")
        logger.info(f"批次数量: {batch_size} 张")
        logger.info(f"图片比例: {aspect_ratio}")
        if seed != -1:
            logger.info(f"随机种子: {seed}")
        if top_p != 0.95:
            logger.info(f"Top-P 参数: {top_p}")
        logger.separator()

        configured_network_cap = self.config_manager.load_network_workers_cap()
        network_workers_cap = min(configured_workers, configured_network_cap)
        actual_workers = min(network_workers_cap, batch_size) if concurrent_mode and batch_size > 1 else 1
        actual_workers = max(1, actual_workers)

        def progress_callback(result: Dict[str, Any], completed_count: int, total_count: int, progress_bar: object):
            if result.get('success'):
                logger.success(
                    f"[{completed_count}/{total_count}] 批次 {result['index']+1} 完成"
                )
            else:
                batch_label = result.get('index', -1)
                batch_text = "?" if batch_label < 0 else batch_label + 1
                logger.error(
                    f"[{completed_count}/{total_count}] 批次 {batch_text} 失败"
                )

            preview_tensor = result.get('tensor')
            if result.get('success') and preview_tensor is not None:
                preview_tuple = self.image_codec.build_preview_tuple(
                    preview_tensor, result['index']
                )
                if preview_tuple is not None:
                    progress_bar.update_absolute(completed_count, total_count, preview_tuple)
                else:
                    progress_bar.update(1)
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

        if not results:
            error_text = f"未生成任何图像\n总耗时: {time.time() - start_time:.2f}s"
            if balance_summary:
                error_text = f"{balance_summary}\n\n{error_text}"
            logger.error(error_text)
            error_tensor = self.error_canvas.build_error_tensor_from_text("生成失败", error_text)
            return (error_tensor, error_text)

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

        total_time = time.time() - start_time

        if not decoded_tensors or total_generated_images == 0:
            error_text = f"未生成任何图像\n总耗时: {total_time:.2f}s\n\n" + "\n".join(all_texts)
            if balance_summary:
                error_text = f"{balance_summary}\n\n{error_text}"
            logger.error(error_text)
            error_tensor = self.error_canvas.build_error_tensor_from_text("生成失败", error_text)
            return (error_tensor, error_text)

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
            logger.warning(f"部分批次未返回图片：请求 {batch_size} 张，实际上只生成 {actual_count} 张，请查看上方各批次日志中的“未返回任何图片”提示")

        combined_text = f"{success_info}\n{time_info}"
        if all_texts:
            combined_text += "\n\n" + "\n".join(all_texts)
        if balance_summary:
            combined_text = f"{balance_summary}\n\n{combined_text}"

        # 显示完成统计
        logger.summary("任务完成", {
            "总批次": f"{batch_size} 个",
            "成功生成": f"{actual_count} 张",
            "总耗时": f"{total_time:.2f}s",
            "平均速度": f"{avg_time:.2f}s/张"
        })

        return (image_tensor, combined_text)

# 注册节点
NODE_CLASS_MAPPINGS = {"BananaImageNode": BananaImageNode}
NODE_DISPLAY_NAME_MAPPINGS = {"BananaImageNode": "心宝❤Banana"}

BALANCE_SERVICE.ensure_route(lambda: getattr(PromptServer, "instance", None))



