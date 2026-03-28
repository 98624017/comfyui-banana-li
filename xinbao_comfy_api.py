"""
心宝❤AI应用 — 动态多应用 ComfyUI 节点

通过 TOML 配置驱动多应用切换（高清放大、修手修脸、图片背景等），
前端 JS 扩展负责动态 hide/show widget，Python 端预声明所有泛型插槽。
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
import torch
from PIL import Image

import comfy.model_management

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from logger import logger
from config_manager import ConfigManager
from image_uploader import ImageUploader
from image_codec import ErrorCanvas
from workflow_parallel import make_execution_blocker
from XinbaoVideos.core.video_task_manager import VIDEO_TASK_MANAGER

# ---------------------------------------------------------------------------
# TOML 配置加载 (三级降级: tomllib → tomli → toml)
# ---------------------------------------------------------------------------

_TOML_PATH = os.path.join(MODULE_DIR, "comfy_api_apps.toml")
_TOML_URL = (
    "https://gist.githubusercontent.com/98624017/5b60092764f5979ee35df55f9fd41e25/raw/"
    "comfy_api_apps.toml"
)
_TOML_PROXY_URL = (
    "https://gh-proxy.com/https://gist.githubusercontent.com/98624017/5b60092764f5979ee35df55f9fd41e25/raw/"
    "comfy_api_apps.toml"
)


def _build_cache_busted_url(url: str) -> str:
    ts = int(time.time())
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}_ts={ts}"


def _fetch_text(url: str, timeout: int = 8) -> Optional[str]:
    try:
        req = urllib.request.Request(
            _build_cache_busted_url(url),
            headers={"User-Agent": "ComfyUI-xinbao/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception:
        return None


def _is_valid_toml(text: str) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    try:
        import tomllib
        tomllib.loads(text)
        return True
    except Exception:
        pass
    try:
        import tomli
        tomli.loads(text)
        return True
    except Exception:
        pass
    try:
        import toml
        toml.loads(text)
        return True
    except Exception:
        return False


def _sync_comfy_api_apps_config() -> None:
    """启动时从云端拉取最新配置，失败则静默降级到本地文件。"""
    text = _fetch_text(_TOML_URL)
    if text and _is_valid_toml(text):
        try:
            with open(_TOML_PATH, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        return
    for _ in range(3):
        text = _fetch_text(_TOML_PROXY_URL)
        if not text or not _is_valid_toml(text):
            continue
        try:
            with open(_TOML_PATH, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        break


_sync_comfy_api_apps_config()


def _load_toml(path: str) -> Optional[dict]:
    """三级降级读取 TOML 文件。"""
    if not os.path.exists(path):
        return None
    # Python 3.11+ 内置
    try:
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        pass
    except Exception:
        pass
    # tomli 第三方包
    try:
        import tomli
        with open(path, "rb") as f:
            return tomli.load(f)
    except ImportError:
        pass
    except Exception:
        pass
    # toml 第三方包
    try:
        import toml
        with open(path, "r", encoding="utf-8") as f:
            return toml.load(f)
    except ImportError:
        pass
    except Exception:
        pass
    return None


def _parse_app_registry(data: dict) -> Dict[str, dict]:
    """
    将 TOML 数据解析为 {display_name: app_config} 映射。
    app_config 包含 id, model, fields 列表。
    """
    registry: Dict[str, dict] = {}
    for app in data.get("apps", []):
        display_name = app.get("display_name", "")
        if not display_name:
            continue
        fields = []
        for f in app.get("fields", []):
            field = {
                "slot": f.get("slot", ""),
                "node_id": str(f.get("node_id", "")),
                "field_name": f.get("field_name", ""),
                "type": f.get("type", "text"),
                "label": f.get("label", ""),
                "required": bool(f.get("required", False)),
            }
            # 类型特定属性
            if field["type"] == "float":
                field["default"] = float(f.get("default", 0.0))
                field["min"] = float(f.get("min", 0.0))
                field["max"] = float(f.get("max", 1.0))
                field["step"] = float(f.get("step", 0.01))
            elif field["type"] == "boolean":
                field["default"] = bool(f.get("default", False))
            elif field["type"] == "combo":
                field["options"] = list(f.get("options", []))
                field["default"] = f.get("default", "")
                field["field_data"] = f.get("field_data", "")
            elif field["type"] == "text":
                field["default"] = str(f.get("default", ""))
            fields.append(field)
        registry[display_name] = {
            "id": app.get("id", ""),
            "model": app.get("model", ""),
            "fields": fields,
        }
    return registry


# 模块加载时读取一次
_RAW_TOML = _load_toml(_TOML_PATH)
if _RAW_TOML is None:
    logger.warning(f"[AI应用] 未能加载配置: {_TOML_PATH}")
    _APP_REGISTRY: Dict[str, dict] = {}
else:
    _APP_REGISTRY = _parse_app_registry(_RAW_TOML)
    logger.success(f"[AI应用] 加载 {len(_APP_REGISTRY)} 个应用配置: {', '.join(_APP_REGISTRY.keys())}")

# ---------------------------------------------------------------------------
# 共享实例
# ---------------------------------------------------------------------------

_CONFIG_MANAGER = ConfigManager()
_ERROR_CANVAS = ErrorCanvas(logger)

# ---------------------------------------------------------------------------
# API 常量
# ---------------------------------------------------------------------------

_API_BASE_URL = base64.b64decode("aHR0cHM6Ly94aW5iYW9hcGkuZHBkbnMub3Jn").decode("utf-8")
_POLL_INTERVAL = 10.0     # 秒（文档建议 10-25s）
_POLL_TIMEOUT = 600.0     # 秒（文档建议 300-600s）
_CONNECT_TIMEOUT = 15.0
_READ_TIMEOUT = 90.0

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _ensure_not_interrupted() -> None:
    """检查 ComfyUI 中断标志。"""
    comfy.model_management.throw_exception_if_processing_interrupted()


def _is_ssl_error(exc: BaseException) -> bool:
    """判断异常是否为 SSL 相关错误。"""
    # requests 包装的 SSL 错误
    if isinstance(exc, requests.exceptions.SSLError):
        return True
    # urllib3 / ssl 模块原始错误
    import ssl
    if isinstance(exc, ssl.SSLError):
        return True
    # 某些环境下 SSL 错误被包装在 ConnectionError 内
    if isinstance(exc, requests.exceptions.ConnectionError):
        cause = exc.__cause__ or exc.__context__
        if cause and "ssl" in str(type(cause).__name__).lower():
            return True
        if cause and "SSL" in str(cause):
            return True
    return False


def _interruptible_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    ssl_auto_retry: bool = True,
    **kwargs,
) -> requests.Response:
    """支持中断的请求封装（后台线程 + 轮询中断检查）。

    ssl_auto_retry: 首次遇到 SSL 错误时，自动以 verify=False 重试一次。
    """
    done_event = threading.Event()
    result_holder: dict = {}

    def _target():
        try:
            result_holder["resp"] = session.request(method, url, **kwargs)
        except Exception as e:
            result_holder["exc"] = e
        finally:
            done_event.set()

    t = threading.Thread(target=_target, daemon=True)
    t.start()

    while not done_event.wait(timeout=0.2):
        _ensure_not_interrupted()

    _ensure_not_interrupted()

    if "exc" in result_holder:
        exc = result_holder["exc"]
        # SSL 错误自动重试：禁用 verify 再来一次
        if ssl_auto_retry and _is_ssl_error(exc):
            logger.warning(f"[AI应用] SSL 错误，自动禁用 SSL 验证重试: {exc}")
            retry_kwargs = dict(kwargs)
            retry_kwargs["verify"] = False
            return _interruptible_request(
                session, method, url, ssl_auto_retry=False, **retry_kwargs
            )
        raise exc
    return result_holder["resp"]


def _tensor_to_png_bytes(image_tensor: torch.Tensor) -> bytes:
    """将 Tensor 转为 PNG 字节，不做任何压缩或缩放。"""
    tensor = image_tensor
    if len(tensor.shape) == 4:
        tensor = tensor[0]
    array = tensor.detach().cpu().numpy()
    array = np.clip(array, 0.0, 1.0)
    array = (array * 255).astype(np.uint8)
    img = Image.fromarray(array)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _resolve_api_key(raw_key: str) -> Tuple[str, str]:
    """解析 API Key 并返回 (api_key, base_url)。

    fixsk- 前缀走隐藏线路；否则用固定 base_url。
    """
    candidate = (raw_key or "").strip()

    # fixsk- 隐藏线路
    if candidate.lower().startswith("fixsk-"):
        stripped = candidate[len("fix"):]
        hidden_key = _CONFIG_MANAGER.sanitize_api_key(stripped)
        if hidden_key:
            return hidden_key, _CONFIG_MANAGER.get_hidden_base_url()

    # 节点输入
    sanitized = _CONFIG_MANAGER.sanitize_api_key(candidate)
    if sanitized:
        return sanitized, _API_BASE_URL

    # config.ini 兜底
    config_key = _CONFIG_MANAGER.sanitize_api_key(_CONFIG_MANAGER.load_api_key())
    if config_key:
        return config_key, _API_BASE_URL

    raise RuntimeError(
        '缺少 Banana API Key：请在节点或"心宝❤全局密钥管理"中填写，或在 config.ini 中配置后重试'
    )


def _mask_key(api_key: str) -> str:
    """对 API key 脱敏显示。"""
    if not api_key:
        return "<empty>"
    if len(api_key) <= 6:
        return f"{api_key[:3]}...{api_key[-1:]}"
    return f"{api_key[:4]}...{api_key[-2:]}"


# ---------------------------------------------------------------------------
# ComfyUI 节点类
# ---------------------------------------------------------------------------


class XinbaoComfyApiApp:
    """心宝❤AI应用 — 动态多应用节点"""

    RETURN_TYPES = ("IMAGE", "STRING",)
    RETURN_NAMES = ("image", "message",)
    FUNCTION = "execute"
    OUTPUT_NODE = True
    CATEGORY = "❤️‍🔥心宝专用/AI应用"

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        """绕过 combo 等后端校验，值由前端 JS 动态控制。"""
        return True

    @classmethod
    def INPUT_TYPES(cls):
        # 应用显示名列表
        display_names = list(_APP_REGISTRY.keys()) if _APP_REGISTRY else ["(无可用应用)"]

        # 收集所有应用中 combo 类型的选项并集（用于声明）
        all_combo_options: Dict[str, list] = {}
        for app_config in _APP_REGISTRY.values():
            for field in app_config.get("fields", []):
                if field["type"] == "combo" and field.get("options"):
                    slot = field["slot"]
                    existing = all_combo_options.get(slot, [])
                    for opt in field["options"]:
                        if opt not in existing:
                            existing.append(opt)
                    all_combo_options[slot] = existing

        # 构建 app_registry 传给 JS 前端
        app_registry_for_js = {}
        for name, config in _APP_REGISTRY.items():
            app_registry_for_js[name] = {
                "model": config["model"],
                "fields": config["fields"],
            }

        inputs = {
            "required": {
                "app_select": (
                    display_names,
                    {
                        "default": display_names[0],
                        "app_registry": app_registry_for_js,
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
                "仅提交不等待": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": '仅提交任务并立即返回，不等待轮询与下载；进度请在右下角"🎬 心宝视频任务"查看',
                    },
                ),
            },
            "optional": {
                # === IMAGE 插槽 ===
                "image_1": ("IMAGE", {"tooltip": "图片输入 1"}),
                "image_2": ("IMAGE", {"tooltip": "图片输入 2"}),

                # === STRING 插槽 ===
                "text_1": (
                    "STRING",
                    {"default": "", "multiline": True, "tooltip": "文本输入 1"},
                ),
                "text_2": (
                    "STRING",
                    {"default": "", "multiline": True, "tooltip": "文本输入 2"},
                ),

                # === FLOAT 插槽 ===
                "float_1": (
                    "FLOAT",
                    {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "浮点输入 1"},
                ),
                "float_2": (
                    "FLOAT",
                    {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "浮点输入 2"},
                ),
                "float_3": (
                    "FLOAT",
                    {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "浮点输入 3"},
                ),
                "float_4": (
                    "FLOAT",
                    {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": "浮点输入 4"},
                ),

                # === BOOLEAN 插槽 ===
                "bool_1": (
                    "BOOLEAN",
                    {"default": False, "tooltip": "布尔输入 1"},
                ),
                "bool_2": (
                    "BOOLEAN",
                    {"default": False, "tooltip": "布尔输入 2"},
                ),

                # === COMBO 插槽 ===
                "combo_1": (
                    all_combo_options.get("combo_1", ["默认"]),
                    {"default": all_combo_options.get("combo_1", ["默认"])[0], "tooltip": "选项输入 1"},
                ),
                "combo_2": (
                    all_combo_options.get("combo_2", ["默认"]),
                    {"default": all_combo_options.get("combo_2", ["默认"])[0], "tooltip": "选项输入 2"},
                ),
            },
        }
        return inputs

    def execute(
        self,
        app_select: str = "",
        banana_api_key: str = "",
        仅提交不等待: bool = False,
        **kwargs,
    ):
        """节点执行入口。"""
        # 1. 查找应用配置
        app_config = _APP_REGISTRY.get(app_select)
        if not app_config:
            return (
                _ERROR_CANVAS.build_error_tensor_from_text(
                    "应用配置错误",
                    f"未找到应用 '{app_select}'，请检查 comfy_api_apps.toml 配置。"
                ),
                f"应用配置错误: 未找到应用 '{app_select}'",
            )

        # 2. 解析 API Key
        try:
            api_key, base_url = _resolve_api_key(banana_api_key)
        except RuntimeError as e:
            return (
                _ERROR_CANVAS.build_error_tensor_from_text("API Key 错误", str(e)),
                f"API Key 错误: {e}",
            )

        masked_key = _mask_key(api_key)
        logger.info(f"[AI应用] 应用={app_select}, model={app_config['model']}, key={masked_key}")

        # 3. 构建 nodeInfoList
        try:
            node_info_list = self._build_node_info_list(app_config, kwargs)
        except Exception as e:
            return (
                _ERROR_CANVAS.build_error_tensor_from_text("参数构建失败", str(e)),
                f"参数构建失败: {e}",
            )

        # 5. 提交任务
        session = requests.Session()
        try:
            task_id = self._submit_task(
                session, base_url, api_key, app_config["model"], node_info_list
            )
        except Exception as e:
            return (
                _ERROR_CANVAS.build_error_tensor_from_text("任务提交失败", str(e)),
                f"任务提交失败: {e}",
            )

        logger.success(f"[AI应用] 任务已提交: {task_id}")

        # ── 仅提交不等待：缓存 Key + 注册到任务中心后立即返回 ──
        if 仅提交不等待:
            key_source = "node_input" if banana_api_key.strip() else "config.ini"
            VIDEO_TASK_MANAGER.set_key("comfy_api", api_key, route_choice="", source=key_source)
            VIDEO_TASK_MANAGER.record_task_created(
                task_id=task_id,
                provider="comfy_api",
                model=str(app_config.get("model") or "").strip(),
                prompt=".",
                route_choice="",
                base_url=str(base_url or "").strip(),
                api_key_source=key_source,
            )
            msg = (
                f"✅ 任务已提交至视频任务中心（ID: {task_id}）\n\n"
                '已启用"仅提交不等待"：不会等待轮询与下载。\n'
                '请在右下角"🎬 心宝视频任务"面板查看进度。'
            )
            logger.info(f"[AI应用] 仅提交不等待: {task_id}")
            return (make_execution_blocker(None), msg)

        # 6. 轮询等待
        try:
            result_urls = self._poll_task(session, base_url, api_key, task_id)
        except comfy.model_management.InterruptProcessingException:
            raise
        except Exception as e:
            return (
                _ERROR_CANVAS.build_error_tensor_from_text("轮询失败", str(e)),
                f"轮询失败: {e}",
            )

        # 7. 下载结果图片
        try:
            tensor = self._download_results(session, result_urls)
        except Exception as e:
            return (
                _ERROR_CANVAS.build_error_tensor_from_text("下载失败", str(e)),
                f"下载失败: {e}",
            )

        return (tensor, f"任务完成: {task_id}")

    def _build_node_info_list(
        self,
        app_config: dict,
        kwargs: dict,
    ) -> List[Dict[str, Any]]:
        """根据应用配置和用户输入构建 API 的 nodeInfoList。"""
        node_info_list = []
        session = requests.Session()
        uploader = ImageUploader(
            session=session,
            logger=logger,
            interrupt_checker=_ensure_not_interrupted,
        )

        for field in app_config.get("fields", []):
            slot = field["slot"]
            value = kwargs.get(slot)
            field_type = field["type"]

            node_info: Dict[str, Any] = {
                "nodeId": field["node_id"],
                "fieldName": field["field_name"],
            }

            if field_type == "image":
                if value is None:
                    if field.get("required"):
                        raise RuntimeError(f"必填图片输入 '{field['label']}' 未连接")
                    continue  # 可选图片未提供，跳过

                # tensor → PNG 字节（不压缩）→ 上传图床 → URL
                _ensure_not_interrupted()
                image_bytes = _tensor_to_png_bytes(value)
                size_kb = len(image_bytes) / 1024
                logger.info(f"[AI应用] 图片 {slot}: {size_kb:.1f}KB (image/png)")

                url = uploader.upload(image_bytes)
                node_info["fieldValue"] = url

            elif field_type == "boolean":
                bool_val = bool(value) if value is not None else field.get("default", False)
                node_info["fieldValue"] = "true" if bool_val else "false"

            elif field_type == "float":
                float_val = float(value) if value is not None else field.get("default", 0.0)
                node_info["fieldValue"] = str(float_val)

            elif field_type == "text":
                text_val = str(value) if value is not None else field.get("default", "")
                node_info["fieldValue"] = text_val

            elif field_type == "combo":
                combo_val = str(value) if value is not None else field.get("default", "")
                node_info["fieldValue"] = combo_val
                # field_data 原样附加
                field_data = field.get("field_data", "")
                if field_data:
                    try:
                        node_info["fieldData"] = json.loads(field_data)
                    except (json.JSONDecodeError, TypeError):
                        node_info["fieldData"] = field_data
            else:
                node_info["fieldValue"] = str(value) if value is not None else ""

            node_info_list.append(node_info)

        return node_info_list

    def _submit_task(
        self,
        session: requests.Session,
        base_url: str,
        api_key: str,
        model: str,
        node_info_list: List[Dict[str, Any]],
    ) -> str:
        """POST /v1/videos 提交任务，返回 task_id。"""
        url = f"{base_url}/v1/videos"
        payload = {
            "model": model,
            "prompt": ".",
            "nodeInfoList": node_info_list,
        }

        _ensure_not_interrupted()
        resp = _interruptible_request(
            session,
            "POST",
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )

        if resp.status_code != 200:
            body = (resp.text or "")[:500]
            raise RuntimeError(f"提交失败 HTTP {resp.status_code}: {body}")

        data = resp.json()
        task_id = data.get("id") or data.get("task_id") or data.get("data", {}).get("id", "")
        if not task_id:
            raise RuntimeError(f"响应中未找到 task_id: {json.dumps(data, ensure_ascii=False)[:300]}")

        return str(task_id)

    def _poll_task(
        self,
        session: requests.Session,
        base_url: str,
        api_key: str,
        task_id: str,
    ) -> List[str]:
        """轮询 GET /v1/videos/{task_id}，返回结果 URL 列表。"""
        import comfy.utils

        url = f"{base_url}/v1/videos/{task_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        start_time = time.time()
        last_status = ""
        progress_bar = comfy.utils.ProgressBar(100)

        while True:
            _ensure_not_interrupted()
            elapsed = time.time() - start_time
            if elapsed > _POLL_TIMEOUT:
                raise RuntimeError(f"轮询超时 ({_POLL_TIMEOUT:.0f}s)")

            try:
                resp = _interruptible_request(
                    session, "GET", url,
                    headers=headers,
                    timeout=(_CONNECT_TIMEOUT, 30.0),
                )
            except comfy.model_management.InterruptProcessingException:
                raise
            except Exception as e:
                logger.warning(f"[AI应用] 轮询请求异常: {e}")
                time.sleep(_POLL_INTERVAL)
                continue

            if resp.status_code != 200:
                logger.warning(f"[AI应用] 轮询 HTTP {resp.status_code}")
                time.sleep(_POLL_INTERVAL)
                continue

            try:
                data = resp.json()
            except Exception:
                time.sleep(_POLL_INTERVAL)
                continue

            status = str(data.get("status", "")).lower()
            progress = int(data.get("progress", 0) or 0)

            # 更新 ComfyUI 进度条
            try:
                progress_bar.update_absolute(min(progress, 100), 100)
            except Exception:
                pass

            if status != last_status:
                last_status = status
            logger.info(f"[AI应用] 轮询中: {status}, 进度: {progress}% ({elapsed:.0f}s/{_POLL_TIMEOUT:.0f}s)")

            # 完成
            if status in ("completed", "succeeded", "success", "done"):
                try:
                    progress_bar.update_absolute(100, 100)
                except Exception:
                    pass
                return self._extract_result_urls(data)

            # 失败: error 是对象 {"code": "...", "message": "..."}
            if status in ("failed", "error", "cancelled", "canceled"):
                error_obj = data.get("error")
                if isinstance(error_obj, dict):
                    error_msg = error_obj.get("message") or error_obj.get("code") or "未知错误"
                elif error_obj:
                    error_msg = str(error_obj)
                else:
                    error_msg = data.get("errorMessage") or data.get("message") or "未知错误"
                raise RuntimeError(f"任务失败 ({status}): {error_msg}")

            time.sleep(_POLL_INTERVAL)

    def _extract_result_urls(self, data: dict) -> List[str]:
        """从轮询响应中提取结果图片 URL。"""
        urls = []

        # 尝试 results 数组
        results = data.get("results") or data.get("data", {}).get("results") or []
        for item in results:
            if isinstance(item, dict):
                url = item.get("url") or item.get("image_url") or ""
                if url:
                    urls.append(url)
            elif isinstance(item, str) and item.startswith("http"):
                urls.append(item)

        # 尝试 output.images
        if not urls:
            output = data.get("output") or {}
            images = output.get("images") or []
            for img in images:
                if isinstance(img, dict):
                    url = img.get("url") or ""
                    if url:
                        urls.append(url)

        # 尝试顶层 url
        if not urls:
            top_url = data.get("url") or data.get("image_url") or ""
            if top_url:
                urls.append(top_url)

        if not urls:
            raise RuntimeError(f"未找到结果图片: {json.dumps(data, ensure_ascii=False)[:300]}")

        return urls

    def _download_results(
        self,
        session: requests.Session,
        urls: List[str],
    ) -> torch.Tensor:
        """下载结果图片并转为 Tensor。"""
        from image_codec import ImageCodec
        codec = ImageCodec(logger, ensure_not_interrupted=_ensure_not_interrupted)

        images: List[Optional[np.ndarray]] = []
        shape_counts: Dict[Tuple[int, int, int], int] = {}
        for i, url in enumerate(urls):
            _ensure_not_interrupted()
            logger.info(f"[AI应用] 下载结果 {i + 1}/{len(urls)}")
            try:
                resp = _interruptible_request(
                    session, "GET", url,
                    timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                )
                if resp.status_code != 200:
                    logger.warning(f"[AI应用] 下载失败 HTTP {resp.status_code}")
                    images.append(None)
                    continue
                arr = codec.base64_to_tensor_single(resp.content)
                images.append(arr)
                if arr.ndim == 3:
                    shape = (int(arr.shape[0]), int(arr.shape[1]), int(arr.shape[2]))
                    shape_counts[shape] = shape_counts.get(shape, 0) + 1
            except comfy.model_management.InterruptProcessingException:
                raise
            except Exception as e:
                logger.warning(f"[AI应用] 下载异常: {e}")
                images.append(None)

        if not images:
            raise RuntimeError("未能下载任何结果图片")

        target_shape = (64, 64, 3)
        if shape_counts:
            target_shape = max(
                shape_counts.items(),
                key=lambda item: (item[1], item[0][0] * item[0][1]),
            )[0]

        normalized_images: List[np.ndarray] = []
        for idx, arr in enumerate(images):
            if arr is None:
                normalized_images.append(np.zeros(target_shape, dtype=np.float32))
                continue
            if arr.shape != target_shape:
                logger.warning(
                    f"[AI应用] 结果图 {idx + 1} 尺寸 {arr.shape} 与目标尺寸 {target_shape} 不一致，"
                    "已使用占位图避免堆叠失败"
                )
                normalized_images.append(np.zeros(target_shape, dtype=np.float32))
                continue
            normalized_images.append(arr.astype(np.float32, copy=False))

        logger.success(f"[AI应用] 成功下载 {len(images)} 张结果图片")
        return torch.from_numpy(np.stack(normalized_images))


# ---------------------------------------------------------------------------
# 节点注册
# ---------------------------------------------------------------------------

NODE_CLASS_MAPPINGS = {
    "XinbaoComfyApiApp": XinbaoComfyApiApp,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "XinbaoComfyApiApp": "心宝❤AI应用",
}
