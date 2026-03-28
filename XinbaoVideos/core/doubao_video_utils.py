"""
即梦豆包系（Seedance）视频节点的纯工具函数。

职责边界：
- 仅做 prompt 追加参数与 input_reference 字符串构造
- 不依赖 ComfyUI、不做网络请求
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple


SUPPORTED_RATIOS = ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
SUPPORTED_RESOLUTIONS = ["480p", "720p"]


def build_doubao_prompt(
    prompt: str,
    *,
    ratio: Optional[str],
    resolution: Optional[str],
    generate_audio: bool,
    camera_fixed: bool,
) -> str:
    """
    将豆包文档定义的高级参数以 `-key=value` 追加到用户 prompt 末尾。

    规则：
    - 不改变用户原 prompt 的文本顺序，仅追加
    - 不支持 watermark 参数，永不注入
    """
    base = (prompt or "").strip()
    parts = [base] if base else []

    if ratio:
        cleaned = str(ratio).strip()
        if cleaned not in SUPPORTED_RATIOS:
            raise ValueError(f"不支持的视频比例：{cleaned}")
        parts.append(f"-ratio={cleaned}")

    if resolution:
        cleaned = str(resolution).strip()
        if cleaned not in SUPPORTED_RESOLUTIONS:
            raise ValueError(f"不支持的视频分辨率：{cleaned}")
        parts.append(f"-resolution={cleaned}")

    parts.append(f"-generate_audio={'true' if bool(generate_audio) else 'false'}")
    parts.append(f"-camera_fixed={'true' if bool(camera_fixed) else 'false'}")

    return " ".join([p for p in parts if p])


def build_input_reference(
    first: Optional[str],
    last: Optional[str],
) -> Optional[str]:
    """
    构造 `input_reference` 字段：
    - 0 图：None（不提交字段）
    - 1 图：字符串
    - 2 图：仍是字符串，但字符串内容为 JSON 数组文本
    """
    first_value = (first or "").strip()
    last_value = (last or "").strip()

    if not first_value and not last_value:
        return None

    if first_value and not last_value:
        return first_value

    if last_value and not first_value:
        # 允许只给“尾帧图”，按单图处理，避免用户因顺序输入受阻
        return last_value

    # 两图：要求为字符串，字符串内容是 JSON 数组
    return json.dumps([first_value, last_value], ensure_ascii=False)


def aggregate_batch_results(results: List[Dict[str, Any]], batch_size: int) -> Tuple[List[Any], str]:
    """
    聚合批量执行结果，生成 videos 列表与汇总文本。

    约定：results 中每项包含：
    - success: bool
    - index: int（0-based）
    - video_obj: object（success 时存在）
    - text/error: str（可选）
    """
    if batch_size <= 0:
        raise ValueError("batch_size 必须大于 0")

    sorted_results = sorted(results or [], key=lambda item: int(item.get("index", -1)))
    videos: List[Any] = []
    lines: List[str] = []

    for item in sorted_results:
        idx = int(item.get("index", -1))
        batch_label = "?" if idx < 0 else str(idx + 1)
        if bool(item.get("success")):
            video_obj = item.get("video_obj")
            if video_obj is not None:
                videos.append(video_obj)
            text = str(item.get("text") or "").strip()
            lines.append(f"[批次 {batch_label}] ✅ {text or '成功'}")
        else:
            error = str(item.get("error") or "未知错误").strip()
            lines.append(f"[批次 {batch_label}] ❌ {error}")

    if not videos:
        error_text = f"未生成任何视频（共 {batch_size} 批次全部失败）"
        if lines:
            error_text += "\n\n" + "\n".join(lines)
        raise RuntimeError(error_text)

    success_count = len(videos)
    summary = f"✅ 成功生成 {success_count}/{batch_size} 个视频"
    if success_count < batch_size:
        summary += f" ⚠️ {batch_size - success_count} 个批次失败"

    combined = summary
    if lines:
        combined += "\n\n" + "\n".join(lines)
    return videos, combined.strip()


def is_undefined_task_id(task_id: str) -> bool:
    """服务端偶发返回 `undefined::...` 的异常任务 ID；这种任务后续无法正常轮询。"""
    cleaned = (task_id or "").strip().lower()
    return cleaned.startswith("undefined")
