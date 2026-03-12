"""ComfyUI 启动初始化级测试：调用官方 nodes.load_custom_node 加载本插件（发布仓库版）。"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load plugin via ComfyUI nodes.load_custom_node")
    parser.add_argument("--comfy-root", required=True, help="ComfyUI 仓库根目录")
    parser.add_argument("--plugin-name", default="comfyui-banana-li", help="custom_nodes 下插件目录名")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    comfy_root = Path(args.comfy_root).resolve()
    plugin_path = comfy_root / "custom_nodes" / args.plugin_name

    if not (comfy_root / "nodes.py").exists():
        raise RuntimeError(f"无效的 ComfyUI 目录: {comfy_root}")
    if not plugin_path.exists():
        raise RuntimeError(f"插件目录不存在: {plugin_path}")

    # 让 Python 能导入 ComfyUI 顶层模块
    sys.path.insert(0, str(comfy_root))

    # comfy.cli_args 默认关闭参数解析；需先开启，再通过 --cpu 强制 CPU 模式。
    comfy_options = importlib.import_module("comfy.options")
    comfy_options.enable_args_parsing(True)

    argv_backup = sys.argv[:]
    sys.argv = [sys.argv[0], "--cpu"]
    try:
        nodes = importlib.import_module("nodes")
    finally:
        sys.argv = argv_backup

    base_node_names = set(nodes.NODE_CLASS_MAPPINGS.keys())
    success = asyncio.run(
        nodes.load_custom_node(
            str(plugin_path),
            base_node_names,
            module_parent="custom_nodes",
        )
    )
    if not success:
        raise RuntimeError("ComfyUI load_custom_node 返回 False，插件导入失败")

    # 至少应出现一个稳定基础节点，证明 mappings 已注入。
    expected_any = {"XinbaoApiKeyPurge", "BananaBindingGenerate"}
    loaded_names = set(nodes.NODE_CLASS_MAPPINGS.keys())
    if not (expected_any & loaded_names):
        raise RuntimeError("插件导入后未发现预期基础节点，疑似加载不完整")

    print("comfyui load_custom_node passed")


if __name__ == "__main__":
    main()

