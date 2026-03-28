# xinbao_text_utils.py
# 心宝文本工具节点 - 从 comfyui-text-splitter 融合并优化稳定性
# 分类：心宝❤工具

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import json

from aiohttp import web
try:
    import comfy.model_management as model_management
except Exception:  # pragma: no cover - 非 ComfyUI 环境兼容
    model_management = None

try:
    from server import PromptServer
except Exception:  # pragma: no cover - 非 ComfyUI 环境兼容
    class _DummyPromptServer:
        instance = None

    PromptServer = _DummyPromptServer()


_JSON_DECODER = json.JSONDecoder()


def _try_extract_json_prompts(text: str) -> Optional[List[str]]:
    """尝试从文本中提取 JSON 格式的 prompts 数组。返回 None 表示非 JSON 格式。

    使用 json.JSONDecoder.raw_decode 逐个 '{' 尝试解析，找到第一个包含
    "prompts" 数组的合法 JSON 对象即返回。天然支持嵌套大括号，且不受
    LLM 在 JSON 前后附加杂文本的影响。

    JSON 路径命中后，内部固定执行 strip + 过滤空串（等价于"移除空行"+"修剪首尾空白"
    均为 True 的行为），不受节点 UI 上的对应开关控制——因为结构化 JSON 的语义已明确，
    无需用户额外干预。
    """
    text_stripped = text.strip()
    if '{' not in text_stripped:
        return None

    for i, ch in enumerate(text_stripped):
        if ch != '{':
            continue
        try:
            obj, _ = _JSON_DECODER.raw_decode(text_stripped, i)
        except (json.JSONDecodeError, ValueError):
            continue

        if not isinstance(obj, dict):
            continue

        prompts = obj.get("prompts")
        if not isinstance(prompts, list):
            continue

        result = [str(p).strip() for p in prompts if isinstance(p, str) and p.strip()]
        # 区分"解析成功但为空"与"非 JSON"：返回 []（空列表）而非 None，由调用方决定报错
        return result

    return None


@dataclass
class _PromptSplitSession:
    node_id: str
    prompts: List[str]
    segment_limit: int = 0
    original_count: int = 0
    truncated: bool = False
    created_at: float = field(default_factory=time.time)
    edited_prompts: Optional[List[str]] = None
    confirmed: bool = False
    closed: bool = False
    event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)


_SESSIONS_LOCK = threading.Lock()
_SESSIONS: Dict[str, _PromptSplitSession] = {}
_SESSION_LIMIT = 32
_SESSION_TTL = 300.0
_DEFAULT_WAIT_SECONDS = 60
_DEFAULT_SEGMENT_LIMIT = 16


def _coerce_int(value: Any, default: int, min_value: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number < min_value:
        return min_value
    return number


def _prune_sessions() -> None:
    now = time.time()
    with _SESSIONS_LOCK:
        expired = [k for k, v in _SESSIONS.items() if (now - v.created_at) > _SESSION_TTL]
        for key in expired:
            _SESSIONS.pop(key, None)
        if len(_SESSIONS) <= _SESSION_LIMIT:
            return
        items = sorted(_SESSIONS.items(), key=lambda kv: kv[1].created_at)
        while len(items) > _SESSION_LIMIT:
            key, _ = items.pop(0)
            _SESSIONS.pop(key, None)


def _get_session(node_id: str) -> Optional[_PromptSplitSession]:
    if not node_id:
        return None
    with _SESSIONS_LOCK:
        return _SESSIONS.get(node_id)


def _set_session(session: _PromptSplitSession) -> None:
    if not session.node_id:
        return
    with _SESSIONS_LOCK:
        existing = _SESSIONS.get(session.node_id)
        if existing:
            with existing.lock:
                existing.closed = True
                existing.event.set()
        _SESSIONS[session.node_id] = session
    _prune_sessions()


def _close_session(node_id: str) -> None:
    if not node_id:
        return
    with _SESSIONS_LOCK:
        session = _SESSIONS.pop(node_id, None)
    if session:
        with session.lock:
            session.closed = True
            session.event.set()


def _json_ok(data: Any) -> web.Response:
    return web.json_response({"success": True, "data": data})


def _json_fail(message: str, status: int = 400) -> web.Response:
    return web.json_response({"success": False, "message": message}, status=status)


_ROUTE_REGISTERED = False
_ROUTE_TIMER: threading.Timer | None = None


def _ensure_prompt_split_routes(prompt_server_provider) -> None:
    global _ROUTE_REGISTERED, _ROUTE_TIMER
    if _ROUTE_REGISTERED:
        return

    prompt_server = prompt_server_provider()
    if prompt_server is None:
        if _ROUTE_TIMER is None or not _ROUTE_TIMER.is_alive() or threading.current_thread() is _ROUTE_TIMER:
            timer = threading.Timer(1.0, lambda: _ensure_prompt_split_routes(prompt_server_provider))
            timer.daemon = True
            _ROUTE_TIMER = timer
            timer.start()
        return

    @prompt_server.routes.post("/banana/prompt_split/confirm")
    async def handle_prompt_split_confirm(request):
        try:
            payload = await request.json()
        except Exception:
            return _json_fail("无效的 JSON 请求", status=400)

        node_id = str(payload.get("node_id") or "").strip()
        prompts = payload.get("prompts")
        if not node_id:
            return _json_fail("缺少 node_id", status=400)
        if not isinstance(prompts, list):
            return _json_fail("prompts 必须为列表", status=400)

        session = _get_session(node_id)
        if session is None:
            return _json_fail("等待已结束或节点未在等待状态", status=404)

        with session.lock:
            if session.closed:
                return _json_fail("等待已结束或节点未在等待状态", status=404)
            edited = [str(item) if item is not None else "" for item in prompts]
            if session.segment_limit > 0 and len(edited) > session.segment_limit:
                edited = edited[:session.segment_limit]
            session.edited_prompts = edited
            session.confirmed = True
            session.event.set()
        return _json_ok({"count": len(session.edited_prompts)})

    _ROUTE_REGISTERED = True


_ensure_prompt_split_routes(lambda: getattr(PromptServer, "instance", None))


def _send_wait_event(node_id: str, prompts: List[str], timeout_sec: int, wait_enabled: bool = True) -> None:
    prompt_server = getattr(PromptServer, "instance", None)
    if prompt_server is None:
        return
    payload = {
        "node_id": node_id,
        "prompts": list(prompts),
        "timeout_sec": int(timeout_sec),
        "wait_enabled": wait_enabled,
    }
    prompt_server.send_sync("xinbao_prompt_split_wait", payload)


def _check_interrupted() -> None:
    if model_management is None:
        return
    model_management.throw_exception_if_processing_interrupted()


class XinbaoTextSplitter:
    """
    根据换行符将长文本分割为多段文本，形成文本批次
    """
    
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {
                    "multiline": True, 
                    "default": "",
                    "placeholder": "输入长文本，每行将作为独立的提示词"
                }),
                "移除空行": ("BOOLEAN", {
                    "default": True,
                }),
                "修剪首尾空白": ("BOOLEAN", {
                    "default": True,
                }),
                "暂停等待": ("BOOLEAN", {
                    "default": True,
                }),
                "等待秒数": ("INT", {
                    "default": _DEFAULT_WAIT_SECONDS,
                    "min": 1,
                    "max": 600,
                    "step": 1,
                }),
                "分割上限": ("INT", {
                    "default": _DEFAULT_SEGMENT_LIMIT,
                    "min": 1,
                    "max": 256,
                    "step": 1,
                }),
            },
            "hidden": {
                "split_node_id": "UNIQUE_ID",
            },
        }
    
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("提示词批次",)
    FUNCTION = "split_text"
    CATEGORY = "心宝❤工具"
    OUTPUT_NODE = True
    OUTPUT_IS_LIST = (True,)
    
    def split_text(
        self,
        text: str,
        移除空行: bool,
        修剪首尾空白: bool,
        暂停等待: bool,
        等待秒数: int,
        分割上限: int,
        split_node_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        分割文本并返回批次
        """
        # 处理空输入
        if text is None:
            return {"ui": {}, "result": ([""],)}
        
        # 确保是字符串类型
        if not isinstance(text, str):
            text = str(text)
        
        # 自动检测 JSON 格式：尝试提取 { "prompts": [...] } 结构
        json_prompts = _try_extract_json_prompts(text)
        if json_prompts is not None:
            if not json_prompts:
                raise ValueError("JSON 解析成功但 prompts 为空：模型未生成有效提示词内容，请检查输入或重试。")
            processed_lines = json_prompts
        else:
            # 原有逻辑：按换行符分割
            text = text.replace('\r\n', '\n').replace('\r', '\n')
            lines = text.split('\n')

            processed_lines = []
            for line in lines:
                if 修剪首尾空白:
                    line = line.strip()
                if 移除空行 and not line:
                    continue
                processed_lines.append(line)

            if not processed_lines:
                processed_lines = [""]

        wait_seconds = _coerce_int(等待秒数, _DEFAULT_WAIT_SECONDS, min_value=1)
        segment_limit = _coerce_int(分割上限, _DEFAULT_SEGMENT_LIMIT, min_value=1)
        original_count = len(processed_lines)
        truncated = original_count > segment_limit
        if truncated:
            processed_lines = processed_lines[:segment_limit]

        node_id = str(split_node_id or getattr(self, "id", "") or getattr(self, "unique_id", "") or "")

        if node_id:
            # 即使不暂停等待，也发送 UI 事件
            # 如果不暂停等待，timeout_sec 设置为 0，wait_enabled 设置为 False
            effective_wait_enabled = bool(暂停等待)
            effective_timeout = wait_seconds if effective_wait_enabled else 0
            _send_wait_event(node_id, processed_lines, timeout_sec=effective_timeout, wait_enabled=effective_wait_enabled)

        if 暂停等待 and node_id:
            session = _PromptSplitSession(
                node_id=node_id,
                prompts=processed_lines,
                segment_limit=segment_limit,
                original_count=original_count,
                truncated=truncated,
            )
            _set_session(session)

            deadline = time.time() + float(wait_seconds)
            try:
                while True:
                    remaining = max(0.0, deadline - time.time())
                    if session.event.wait(timeout=min(0.2, remaining)):
                        break
                    _check_interrupted()
                    if remaining <= 0:
                        break
                with session.lock:
                    final_prompts = session.edited_prompts or session.prompts
            finally:
                _close_session(node_id)
        else:
            final_prompts = processed_lines

        ui_payload = {}
        if node_id:
            ui_payload = {
                "xinbao_prompt_split": {
                    "node_id": node_id,
                    "segments": list(final_prompts),
                    "segment_limit": segment_limit,
                    "original_count": original_count,
                    "truncated": truncated,
                    "wait_seconds": wait_seconds,
                }
            }

        return {"ui": ui_payload, "result": (final_prompts,)}


# ComfyUI 节点映射
NODE_CLASS_MAPPINGS = {
    "XinbaoTextSplitter": XinbaoTextSplitter,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "XinbaoTextSplitter": "心宝❤提示词分割",
}
