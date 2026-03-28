from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any


def _add_comfyui_to_syspath() -> None:
    """
    让脚本在“非 ComfyUI 进程”中也能导入 comfy/folder_paths/server 等模块。

    目录结构（当前项目）：
      ComfyUI/custom_nodes/comfyui-banana-pro/.test/...
    因此 parents[3] 即为 ComfyUI 根目录。
    """
    here = Path(__file__).resolve()
    plugin_dir = here.parents[1]
    comfyui_dir = here.parents[3]

    sys.path.insert(0, str(plugin_dir))
    sys.path.insert(0, str(comfyui_dir))


def _short(s: str, limit: int = 120) -> str:
    text = (s or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _is_execution_blocker(obj: Any) -> bool:
    try:
        from comfy_execution.graph_utils import ExecutionBlocker  # type: ignore

        return isinstance(obj, ExecutionBlocker)
    except Exception:
        # 离线环境 fallback：尽量通过属性判断
        return obj is not None and obj.__class__.__name__.lower().endswith("executionblocker")


async def _run_one_banana_v2(index: int, api_key: str, t0: float) -> dict[str, Any]:
    from Gemini_Imagen_Generator import BananaImageNodeV2  # noqa: WPS433 (runtime import)

    node = BananaImageNodeV2()
    prompt = f"Workflow parallel test (BananaV2 #{index+1}): A cute orange cat sitting on a sofa, soft lighting, high quality photo."
    started = time.monotonic()

    result = await node.generate_images_async(
        **{
            "启用工作流并发": True,
            "prompt": prompt,
            "banana_api_key": api_key,
            "model_type": "gemini-2.5-flash-image",
            "batch_size": 1,
            "aspect_ratio": "Auto",
            "image_size": "2K",
            "seed": -1,
            "top_p": 0.95,
            "max_workers": 1,
            "image_1": None,
            "image_2": None,
            "image_3": None,
            "image_4": None,
            "image_5": None,
            "联网搜索": False,
            "绕过代理": False,
            "线路": "心宝❤新渠道",
            "禁用SSL验证": False,
            "大于5M限制长边": "禁用",
        }
    )

    finished = time.monotonic()
    elapsed = finished - started
    image, text = result
    ok = not _is_execution_blocker(image)

    shape = None
    if ok:
        try:
            shape = tuple(getattr(image, "shape", None) or ())
        except Exception:
            shape = None

    return {
        "kind": "banana_v2",
        "index": index,
        "ok": ok,
        "started_offset_s": started - t0,
        "elapsed_s": elapsed,
        "shape": shape,
        "text": _short(str(text), 180),
    }


async def _run_one_video(index: int, api_key: str, t0: float) -> dict[str, Any]:
    from Xinbao_Video_Generator import XinbaoVideoGenerator  # noqa: WPS433 (runtime import)

    node = XinbaoVideoGenerator()
    prompt = f"Workflow parallel test (Video #{index+1}): Generate a short landscape nature video, daylight, stable camera."
    started = time.monotonic()

    result = await node.generate_async(
        **{
            "启用工作流并发": True,
            "prompt": prompt,
            "banana_api_key": api_key,
            "model_type": "sora-2-10s",
            "aspect_ratio": "横版",
            "batch_size": 1,
            "seed": -1,
            "image": None,
            "流式模式": True,
            "绕过代理": False,
            "禁用SSL验证": False,
            "线路": "心宝❤新渠道",
            "启用角色": False,
            "仅创建角色": False,
            "保存角色到提示词助手": False,
            "角色参考视频": None,
        }
    )

    finished = time.monotonic()
    elapsed = finished - started
    video, text, videos = result
    ok = not _is_execution_blocker(video)

    # 只做轻量验证：是否像 mp4（ftyp），以及体积
    mp4_magic = None
    size = None
    if ok:
        try:
            data = getattr(video, "data", None)
            if data is not None:
                data.seek(0)
                head = data.read(16)
                mp4_magic = head[4:8].decode("ascii", errors="ignore") if len(head) >= 8 else None
                data.seek(0, 2)
                size = data.tell()
        except Exception:
            pass

    return {
        "kind": "video",
        "index": index,
        "ok": ok,
        "started_offset_s": started - t0,
        "elapsed_s": elapsed,
        "mp4_magic": mp4_magic,
        "size_bytes": size,
        "text": _short(str(text), 180),
        "videos_len": len(videos) if isinstance(videos, list) else None,
    }


async def main() -> None:
    _add_comfyui_to_syspath()

    api_key = (os.environ.get("BANANA_TEST_KEY") or "").strip()
    if not api_key:
        raise SystemExit("缺少环境变量 BANANA_TEST_KEY（请在 PowerShell 中设置后再运行）")

    # 运行前自检：确保 ComfyUI 能识别协程入口
    import inspect
    from Gemini_Imagen_Generator import BananaImageNodeV2
    from Xinbao_Video_Generator import XinbaoVideoGenerator

    assert inspect.iscoroutinefunction(getattr(BananaImageNodeV2, BananaImageNodeV2.FUNCTION))
    assert inspect.iscoroutinefunction(getattr(XinbaoVideoGenerator, XinbaoVideoGenerator.FUNCTION))

    start_ts = time.monotonic()

    tasks = [
        asyncio.create_task(_run_one_banana_v2(0, api_key, start_ts)),
        asyncio.create_task(_run_one_banana_v2(1, api_key, start_ts)),
        asyncio.create_task(_run_one_video(0, api_key, start_ts)),
        asyncio.create_task(_run_one_video(1, api_key, start_ts)),
    ]

    # 并发运行：模拟工作流中 2 个 BananaV2 + 2 个视频节点同时开启并发
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total = time.monotonic() - start_ts
    print(f"[SUMMARY] total_elapsed_s={total:.2f} results={len(results)}")

    for item in results:
        if isinstance(item, BaseException):
            print(f"[RESULT] exception={type(item).__name__} msg={_short(str(item), 200)}")
            continue
        kind = item.get("kind")
        idx = item.get("index")
        ok = item.get("ok")
        started_offset = item.get("started_offset_s")
        elapsed = item.get("elapsed_s")
        extra = ""
        if kind == "banana_v2":
            extra = f" shape={item.get('shape')}"
        elif kind == "video":
            extra = f" mp4_magic={item.get('mp4_magic')} size_bytes={item.get('size_bytes')} videos_len={item.get('videos_len')}"
        print(
            f"[RESULT] kind={kind} idx={idx} ok={ok} started_offset_s={started_offset:.2f} elapsed_s={elapsed:.2f}{extra} text={item.get('text')}"
        )


if __name__ == "__main__":
    asyncio.run(main())
