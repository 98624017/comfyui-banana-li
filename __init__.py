"""
通用ComfyUI自定义节点加载器
支持任何文件夹名称，自动检测并加载节点
"""

import os
import sys
import importlib.util
from pathlib import Path

# 导入新的日志系统
from .logger import logger

# 获取当前文件夹路径
current_dir = Path(__file__).parent

# 确保当前目录在 sys.path 中，以便被加载的模块能找到 logger 等依赖
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

# 初始化节点映射字典
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
__version__ = "V0.03"

# 需要跳过的文件列表
SKIP_FILES = {
    "__init__.py",
    "logger.py",
    "config_manager.py",
    "api_client.py",
    "image_codec.py",
    "balance_service.py",
    "task_runner.py",
    "test_logger.py",
    "test_enhancements.py",
    "verify_integration.py",
}

# 显示加载器标题
logger.header("🍌 Banana Node Loader")
logger.info(f"Banana Gemini version {__version__}")

# 自动查找并加载所有Python文件中的节点
for py_file in current_dir.glob("*.py"):
    # 跳过特殊文件和测试文件
    if py_file.name in SKIP_FILES:
        continue

    try:
        # 动态导入模块
        module_name = py_file.stem
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # 合并节点映射
        if hasattr(module, 'NODE_CLASS_MAPPINGS'):
            NODE_CLASS_MAPPINGS.update(module.NODE_CLASS_MAPPINGS)

        if hasattr(module, 'NODE_DISPLAY_NAME_MAPPINGS'):
            NODE_DISPLAY_NAME_MAPPINGS.update(module.NODE_DISPLAY_NAME_MAPPINGS)

        logger.success(f"成功加载节点文件: {py_file.name}")

    except Exception as e:
        logger.error(f"加载节点文件失败 {py_file.name}: {str(e)}")

# 额外加载子包（如分割节点集合）
try:
    from .segment_nodes_li import (
        NODE_CLASS_MAPPINGS as SEG_NODE_CLASS_MAPPINGS,
        NODE_DISPLAY_NAME_MAPPINGS as SEG_NODE_DISPLAY_NAME_MAPPINGS,
    )

    NODE_CLASS_MAPPINGS.update(SEG_NODE_CLASS_MAPPINGS)
    NODE_DISPLAY_NAME_MAPPINGS.update(SEG_NODE_DISPLAY_NAME_MAPPINGS)
    logger.success("成功加载子包: segment_nodes_li")
except Exception as e:
    logger.error(f"加载子包 segment_nodes_li 失败: {str(e)}")

# 打印加载的节点信息
if NODE_CLASS_MAPPINGS:
    logger.info(f"总共加载了 {len(NODE_CLASS_MAPPINGS)} 个自定义节点")
    for node_name in NODE_CLASS_MAPPINGS.keys():
        display_name = NODE_DISPLAY_NAME_MAPPINGS.get(node_name, node_name)
        logger.info(f"   - {display_name} ({node_name})")
else:
    logger.warning("未找到任何有效的节点")

# ComfyUI需要的变量
__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS', '__version__']
WEB_DIRECTORY = "./web"
