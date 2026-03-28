from __future__ import annotations

import os
from typing import List

import folder_paths
from comfy.cli_args import args


class XinbaoBatchSaveVideo:
    """
    批量保存视频并在节点内预览。
    支持接收列表（INPUT_IS_LIST），会自动扁平化。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "videos": ("VIDEO", {"tooltip": "单个视频或视频列表", "forceInput": True}),
                "filename_prefix": ("STRING", {"default": "video/ComfyUI", "tooltip": "文件名前缀"}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ()
    OUTPUT_NODE = True
    CATEGORY = "❤️‍🔥心宝专用/视频"
    FUNCTION = "execute"
    INPUT_IS_LIST = True

    @staticmethod
    def _normalize_videos(videos: object) -> List[object]:
        if videos is None:
            return []
        if isinstance(videos, (list, tuple)):
            flattened: List[object] = []
            for item in videos:
                if isinstance(item, (list, tuple)):
                    flattened.extend(item)
                elif item is not None:
                    flattened.append(item)
            return flattened
        return [videos]

    def execute(
        self,
        videos,
        filename_prefix: str = "video/ComfyUI",
        prompt=None,
        extra_pnginfo=None,
    ):
        video_list = self._normalize_videos(videos)
        if not video_list:
            raise RuntimeError("未收到任何可保存的视频数据，请检查上游节点输出")

        # 处理可能传递下来的 list[list] 结构（因为 INPUT_IS_LIST=True）
        # _normalize_videos 已经处理了一层，但如果上游本身就是 list，这里已经是扁平的 list 了?
        # ComfyUI INPUT_IS_LIST: input argument receives the whole list of outputs from the connected node.
        # If upstream outputs [A, B], we get [A, B].
        # If upstream outputs just A, but we have multiple links?
        # 通常我们得到的 `videos` list 包含来自每个 loop/batch 的 item。

        # 取第一个视频来确定路径（如果有的话）
        first_video = video_list[0]
        # 检查是否有效对象
        if not hasattr(first_video, "save_to"):
            raise RuntimeError("输入类型无效：需要支持 save_to 的视频对象")

        # 尝试获取尺寸，如果没有则默认 0
        width, height = 0, 0
        if hasattr(first_video, "get_dimensions"):
            try:
                width, height = first_video.get_dimensions()
            except Exception:
                pass

        (
            full_output_folder,
            filename,
            counter,
            subfolder,
            prefix,
        ) = folder_paths.get_save_image_path(
            filename_prefix[0] if isinstance(filename_prefix, list) else filename_prefix,
            folder_paths.get_output_directory(),
            width,
            height,
        )

        saved_metadata = None
        if not args.disable_metadata:
            metadata = {}
            if extra_pnginfo is not None and len(extra_pnginfo) > 0:
                # INPUT_IS_LIST 导致 extra_pnginfo 也是 list
                info = extra_pnginfo[0] if isinstance(extra_pnginfo, list) else extra_pnginfo
                if info:
                    metadata.update(info)
            if prompt is not None:
                # 同理 prompt 也是 list
                p = prompt[0] if isinstance(prompt, list) else prompt
                if p:
                    metadata["prompt"] = p
            if metadata:
                saved_metadata = metadata

        ui_results = []
        # format/codec 暂时忽略，直接保存原始流，后缀默认为 mp4
        extension = "mp4"

        saved_files = []
        for idx, video in enumerate(video_list):
            if not hasattr(video, "save_to"):
                continue

            file_name = f"{filename}_{counter + idx:05}_.{extension}"
            target_path = os.path.join(full_output_folder, file_name)

            video.save_to(
                target_path,
                metadata=saved_metadata,
            )
            saved_files.append(target_path)

            # 返回给前端预览
            ui_results.append({"filename": file_name, "subfolder": subfolder, "type": "output"})

        feedback_lines = ["✅ 视频批量保存成功", f"📂 保存目录: {full_output_folder}", "📄 文件列表:"]
        for i, path in enumerate(saved_files, 1):
            feedback_lines.append(f"{i}. {os.path.basename(path)}")

        return {"ui": {"videos": ui_results, "text": ["\n".join(feedback_lines)]}}

