"""
模块名称: Banana Failed URL Aggregator
功能描述:
    汇总多个 BananaV2 节点的下载失败链接，支持可选的重新下载尝试。

    1. 连接多个 BananaV2 的 failed_urls 输出，二度汇总并去重。
    2. 可选"下载尝试"开关：开启后会对失败链接进行并发重新下载。
    3. 动态输入接口：起始 2 个，占满后自动新增，上限 20 个（需前端 JS 配合）。
"""
from __future__ import annotations

import io
import os
import sys
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
import torch
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import comfy.model_management
import comfy.utils
from logger import logger

# ── 常量 ──────────────────────────────────────────────
_MAX_INPUTS = 20
_MIN_INPUTS = 2
_CONNECT_TIMEOUT = 15
_DEFAULT_READ_TIMEOUT = 60


class BananaFailedUrlAggregator:
    """
    汇总多个 BananaV2 节点的下载失败链接。

    输出端口：
        - 汇总文本 (STRING): 所有失败链接的编号列表
        - 下载图片 (IMAGE): 重新下载成功的图片列表（保持原始分辨率）
        - 下载结果 (STRING): 重新下载的执行结果摘要
    """

    RETURN_TYPES = ("STRING", "IMAGE", "STRING")
    RETURN_NAMES = ("汇总文本", "下载图片", "下载结果")
    OUTPUT_IS_LIST = (False, True, False)
    FUNCTION = "execute"
    CATEGORY = "❤️‍🔥心宝专用"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        inputs: Dict[str, Any] = {
            "required": {
                "下载尝试": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "开启后会对下载失败的链接进行再度并发下载尝试",
                    },
                ),
                "下载超时": (
                    "INT",
                    {
                        "default": _DEFAULT_READ_TIMEOUT,
                        "min": 5,
                        "max": 600,
                        "step": 5,
                        "tooltip": "下载读取超时时间（秒），默认 60s 与 BananaV2 节点一致",
                    },
                ),
            },
            "optional": {},
        }
        for i in range(1, _MAX_INPUTS + 1):
            inputs["optional"][f"failed_urls_{i}"] = (
                "STRING",
                {
                    "forceInput": True,
                    "tooltip": f"连接 BananaV2 的 failed_urls 输出 #{i}",
                },
            )
        return inputs

    # ── 主入口 ────────────────────────────────────────

    def execute(
        self,
        下载尝试: bool = False,
        下载超时: int = _DEFAULT_READ_TIMEOUT,
        **kwargs,
    ) -> Tuple:
        log = logger
        placeholder = torch.zeros(1, 64, 64, 3, dtype=torch.float32)

        # 1. 收集所有非空的失败 URL
        unique_urls = self._collect_urls(kwargs)

        # 2. 无失败链接
        if not unique_urls:
            log.info("失败链接汇总：无失败链接")
            return ("无失败链接", [placeholder], "")

        # 3. 构建汇总文本
        summary_text = self._build_summary(unique_urls, log)

        # 4. 仅汇总模式
        if not 下载尝试:
            return (summary_text, [placeholder], "仅汇总模式（未尝试下载）")

        # 5. 尝试重新下载
        image_list, download_result = self._retry_downloads(
            unique_urls, int(下载超时), log
        )
        return (summary_text, image_list, download_result)

    # ── 内部方法 ──────────────────────────────────────

    @staticmethod
    def _collect_urls(kwargs: Dict[str, Any]) -> List[str]:
        """从所有 failed_urls_N 输入中收集并去重 URL。"""
        all_urls: List[str] = []
        for i in range(1, _MAX_INPUTS + 1):
            value = kwargs.get(f"failed_urls_{i}")
            if not value or not isinstance(value, str):
                continue
            for line in value.strip().split("\n"):
                line = line.strip()
                if line.startswith("http"):
                    all_urls.append(line)

        # 去重（保持顺序）
        seen: set[str] = set()
        unique: List[str] = []
        for url in all_urls:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        return unique

    @staticmethod
    def _build_summary(urls: List[str], log) -> str:
        """构建汇总文本并打印日志。"""
        lines = [f"[{i}] {url}" for i, url in enumerate(urls, 1)]
        summary = f"⚠️ 失败链接汇总 ({len(urls)} 个):\n" + "\n".join(lines)

        log.separator()
        log.warning(f"⚠️ 失败链接汇总 ({len(urls)} 个):")
        for line in lines:
            log.warning(f"  {line}")
        log.separator()
        return summary

    def _retry_downloads(
        self, urls: List[str], read_timeout: int, log
    ) -> Tuple[List[torch.Tensor], str]:
        """并发重新下载失败链接，返回 (image_list, result_text)。"""
        from PIL import Image

        log.info(f"开始重新下载 {len(urls)} 个失败链接（超时: {read_timeout}s）...")
        download_timeout = (_CONNECT_TIMEOUT, max(5, read_timeout))

        downloaded: List[Tuple[bytes, str]] = []
        still_failed: List[str] = []
        progress = comfy.utils.ProgressBar(len(urls))

        def _download(url: str) -> Tuple[Optional[bytes], str]:
            try:
                resp = requests.get(url, timeout=download_timeout, verify=False)
                resp.raise_for_status()
                return resp.content, url
            except Exception as exc:
                log.debug(f"  下载失败 {url[:60]}: {type(exc).__name__}: {exc}")
                return None, url

        executor = ThreadPoolExecutor(
            max_workers=min(8, len(urls)),
            thread_name_prefix="BananaFailedUrlRetry",
        )
        try:
            future_to_url = {
                executor.submit(_download, url): url for url in urls
            }
            not_done = set(future_to_url.keys())
            completed = 0

            while not_done:
                comfy.model_management.throw_exception_if_processing_interrupted()
                done, not_done = wait(
                    not_done, timeout=0.5, return_when=FIRST_COMPLETED
                )
                for future in done:
                    completed += 1
                    data, url = future.result()
                    if data is not None:
                        downloaded.append((data, url))
                        log.info(
                            f"  ✅ [{completed}/{len(urls)}] 重新下载成功: {url[:80]}"
                        )
                    else:
                        still_failed.append(url)
                        log.warning(
                            f"  ❌ [{completed}/{len(urls)}] 仍然失败: {url[:80]}"
                        )
                    progress.update_absolute(completed, len(urls))
        except comfy.model_management.InterruptProcessingException:
            raise
        except Exception as exc:
            log.error(f"重新下载异常中断: {exc}")
        finally:
            executor.shutdown(wait=False)

        # 转换下载成功的图片为 tensor
        tensors: List[torch.Tensor] = []
        for data, url in downloaded:
            try:
                img = Image.open(io.BytesIO(data)).convert("RGB")
                arr = np.array(img).astype(np.float32) / 255.0
                tensors.append(torch.from_numpy(arr).unsqueeze(0))  # (1,H,W,3)
            except Exception as e:
                still_failed.append(url)
                log.warning(f"  图片解码失败: {url[:80]} ({e})")

        # 构建图片列表输出（保持原始分辨率，不做缩放）
        if tensors:
            image_list = tensors
        else:
            image_list = [torch.zeros(1, 64, 64, 3, dtype=torch.float32)]

        # 构建结果文本
        result_parts: List[str] = []
        if tensors:
            result_parts.append(f"✅ 成功下载 {len(tensors)} 张图片")
        if still_failed:
            result_parts.append(f"❌ 仍然失败 {len(still_failed)} 个链接:")
            for i, url in enumerate(still_failed, 1):
                result_parts.append(f"  [{i}] {url}")
        download_result = "\n".join(result_parts) if result_parts else "下载完成"

        log.separator()
        log.info(
            f"重新下载完成：成功 {len(tensors)} 张，失败 {len(still_failed)} 个"
        )
        log.separator()

        return image_list, download_result


NODE_CLASS_MAPPINGS = {
    "BananaFailedUrlAggregator": BananaFailedUrlAggregator,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BananaFailedUrlAggregator": "心宝❤失败链接汇总",
}
