"""
视频任务中心启动器。

说明：
- 本文件用于在 ComfyUI 加载插件时启动视频任务守护线程并注册后端 API 路由。
- 具体实现位于 `XinbaoVideos/core/video_task_manager.py`，此处仅做一次导入触发初始化。
"""

from XinbaoVideos.core import video_task_manager as _video_task_manager  # noqa: F401

