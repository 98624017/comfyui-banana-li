import os
import sys
import importlib.util
from pathlib import Path

# 尝试自动下载二进制文件
try:
    from . import loader_bootstrap
    loader_bootstrap.ensure_binaries()
except Exception as e:
    print(f"Banana-Li: Failed to bootstrap binaries: {e}")

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
__version__ = "V0.09"

# 需要跳过的文件列表
SKIP_FILES = {
    "__init__.py",
    "logger.py",
    "config_manager.py",
    "api_client.py",
    "image_codec.py",
    "balance_service.py",
    "task_runner.py",
    "loader_bootstrap.py",
    "install.py",
    "check_files.py",
    "setup.py",
    "test_logger.py",
    "test_enhancements.py",
    "verify_integration.py",
}

# 显示加载器标题（保留方框，只显示心宝❤Banana Loader）
logger.header("心宝❤Banana Loader")
logger.info(f"心宝❤Banana version {__version__}")

# 自动查找并加载所有节点文件 (优先加载源码 .py，其次加载编译文件 .pyd/.so)
# 1. 收集所有可能的模块文件
all_files = {} # module_name -> file_path
for pattern in ["*.py", "*.pyd", "*.so"]:
    for file_path in current_dir.glob(pattern):
        if file_path.name in SKIP_FILES:
            continue
        module_name = file_path.stem
        # 如果是 .py，优先级最高，直接覆盖
        if file_path.suffix == ".py":
            all_files[module_name] = file_path
        # 如果是编译文件，且字典里还没有（即没有对应的 .py），则添加
        elif module_name not in all_files:
            all_files[module_name] = file_path

# 2. 加载模块
for module_name, py_file in all_files.items():
    try:
        # 动态导入模块
        # module_name = py_file.stem # 已经在上面获取了
        
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    
            # 合并节点映射
            if hasattr(module, 'NODE_CLASS_MAPPINGS'):
                NODE_CLASS_MAPPINGS.update(module.NODE_CLASS_MAPPINGS)
    
            if hasattr(module, 'NODE_DISPLAY_NAME_MAPPINGS'):
                NODE_DISPLAY_NAME_MAPPINGS.update(module.NODE_DISPLAY_NAME_MAPPINGS)
    
            logger.success(f"成功加载节点文件: {py_file.name}")
        else:
            logger.warning(f"无法为文件创建 spec: {py_file.name}")

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
