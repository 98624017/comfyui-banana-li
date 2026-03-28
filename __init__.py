import sys
import importlib.util
from pathlib import Path

# 获取当前文件夹路径
current_dir = Path(__file__).resolve().parent

# 确保当前目录在 sys.path 中，以便被加载的模块能找到依赖
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))


_SUPPORTED_MODULE_SUFFIXES = {".py", ".pyd", ".so"}


def _module_stem_from_filename(file_name: str) -> str:
    """提取模块基名，兼容 `module.abi3.so` / `module.cp312-win_amd64.pyd` 等格式。"""
    return file_name.split(".", 1)[0]


def _is_supported_module_file(file_path: Path) -> bool:
    return file_path.is_file() and file_path.suffix in _SUPPORTED_MODULE_SUFFIXES


def _module_file_priority(file_path: Path) -> tuple[int, str]:
    """源码优先，其次编译产物；同类中优先无标签文件，再按名称稳定排序。"""
    suffix_priority = {
        ".py": 0,
        ".pyd": 1,
        ".so": 2,
    }
    tagged_priority = 1 if len(file_path.suffixes) > 1 else 0
    return (suffix_priority.get(file_path.suffix, 99), tagged_priority, file_path.name.lower())


def _find_local_module_file(module_stem: str) -> Path:
    matches = []
    for file_path in current_dir.iterdir():
        if not _is_supported_module_file(file_path):
            continue
        if _module_stem_from_filename(file_path.name) != module_stem:
            continue
        matches.append(file_path)

    if not matches:
        raise FileNotFoundError(f"未找到本地模块文件: {module_stem}")

    return min(matches, key=_module_file_priority)


def _load_module_from_file(module_name: str, module_path: Path):
    """按模块基名加载文件，兼容 ABI 标签编译产物。"""
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法创建模块加载 spec: {module_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_local_module(module_stem: str):
    """按模块基名加载当前目录下的本地模块，不依赖固定扩展名。"""
    module_path = _find_local_module_file(module_stem)
    return _load_module_from_file(module_stem, module_path)


# 尝试自动下载二进制文件
try:
    loader_bootstrap = _load_local_module("loader_bootstrap")
    loader_bootstrap.ensure_binaries()
except Exception as e:
    print(f"Banana-Li: Failed to bootstrap binaries: {e}")


def _remove_quarantine_on_macos():
    """macOS: 移除 .so 文件的 com.apple.quarantine 属性，避免 Gatekeeper 拦截。"""
    import platform
    if platform.system() != "Darwin":
        return

    import subprocess
    so_files = list(current_dir.rglob("*.so"))
    if not so_files:
        return

    removed = 0
    for so_file in so_files:
        try:
            result = subprocess.run(
                ["xattr", "-d", "com.apple.quarantine", str(so_file)],
                capture_output=True,
            )
            if result.returncode == 0:
                removed += 1
        except Exception:
            pass

    if removed > 0:
        print(f"Banana-Li: Cleared quarantine flag from {removed} .so file(s)")


_remove_quarantine_on_macos()

# 导入新的日志系统
logger = _load_local_module("logger").logger

# 初始化节点映射字典
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
def get_version_from_toml():
    try:
        toml_path = current_dir / "pyproject.toml"
        if not toml_path.exists():
            return "0.0.0"
        with open(toml_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("version"):
                    # quick parse: version = "0.1.3"
                    parts = line.split("=")
                    if len(parts) == 2:
                        return parts[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "0.0.0"

__version__ = f"V{get_version_from_toml()}"

# 需要跳过的模块基名列表
SKIP_MODULES = {
    "__init__",
    "logger",
    "config_manager",
    "api_client",
    "image_codec",
    "balance_service",
    "task_runner",
    "loader_bootstrap",
    "setup",
    "test_logger",
    "test_enhancements",
    "verify_integration",
    "image_uploader",
    "banana_kv_auth",
    "banana_binding",
    "stress_test_gemini",
    "test_image_compress",
    "workflow_parallel",  # 工具模块，非节点；含 async generator 不可编译
    "xinbao_batch_detail_image_saver",  # 暂未上线，屏蔽节点加载
    # 调试/复现脚本，不应加载
    "reproduce_issue_ms",
    "reproduce_toml_issue",
    "poll_manual",
}

# 不应被编译的模块（含 generator/async generator），删除旧编译产物防止遮蔽 .py 源码
_SOURCE_ONLY_MODULES = {"workflow_parallel", "video_task_manager"}


def _cleanup_stale_binaries():
    """删除不应被编译的模块的旧 .pyd/.so 产物，防止 Python import 优先加载编译版本。"""
    removed = 0
    for file_path in current_dir.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.suffix not in (".pyd", ".so"):
            continue
        stem = _module_stem_from_filename(file_path.name)
        if stem in _SOURCE_ONLY_MODULES:
            try:
                file_path.unlink()
                removed += 1
            except Exception:
                pass
    return removed


_stale_removed = _cleanup_stale_binaries()

# 显示加载器标题（保留方框，只显示心宝❤Banana Loader）
logger.header("心宝❤Banana Loader")
logger.info(f"心宝❤Banana version {__version__}")
if _stale_removed > 0:
    logger.info(f"已清理 {_stale_removed} 个不应存在的旧编译产物")

def _patch_cython_async_nodes(node_mappings):
    """用纯 Python async def 包装 Cython 编译的 async 节点方法。

    Cython Limited API 编译 async def 产生的协程类型（如 _cython_*_limitedcoroutine /
    _cython_*_limitednofinalize.coroutine）可能不被 ComfyUI 的 inspect.iscoroutinefunction
    正确识别，或运行时行为与标准 asyncio 协程不兼容。
    用纯 Python async def 包装，确保 ComfyUI 始终能正确 await。
    """
    import types
    import inspect
    import functools

    patched = 0
    for cls in node_mappings.values():
        func_name = getattr(cls, "FUNCTION", None)
        if not func_name:
            continue
        original = getattr(cls, func_name, None)
        if original is None:
            continue
        # 项目约定：异步节点入口方法名含 "async"
        if "async" not in func_name.lower():
            continue
        # types.FunctionType 仅对纯 Python 函数为 True，Cython 编译产物永远不是
        if isinstance(original, types.FunctionType) and inspect.iscoroutinefunction(original):
            continue

        @functools.wraps(original)
        async def _async_wrapper(self, *args, _orig=original, **kwargs):
            return await _orig(self, *args, **kwargs)

        setattr(cls, func_name, _async_wrapper)
        patched += 1

    if patched > 0:
        logger.info(f"已修补 {patched} 个 Cython async 节点方法")


# 自动查找并加载所有节点文件 (优先加载源码 .py，其次加载编译文件 .pyd/.so)
# 1. 收集所有可能的模块文件
all_files = {}  # module_name -> file_path
for file_path in sorted(current_dir.iterdir(), key=lambda path: path.name.lower()):
    if not _is_supported_module_file(file_path):
        continue

    module_name = _module_stem_from_filename(file_path.name)
    if module_name in SKIP_MODULES:
        continue

    current_selected = all_files.get(module_name)
    if current_selected is None or _module_file_priority(file_path) < _module_file_priority(current_selected):
        all_files[module_name] = file_path

# 2. 加载模块
for module_name, py_file in all_files.items():
    try:
        # 动态导入模块
        # module_name = py_file.stem # 已经在上面获取了
        
        module = _load_module_from_file(module_name, py_file)
    
        # 合并节点映射
        if hasattr(module, 'NODE_CLASS_MAPPINGS'):
            NODE_CLASS_MAPPINGS.update(module.NODE_CLASS_MAPPINGS)
    
        if hasattr(module, 'NODE_DISPLAY_NAME_MAPPINGS'):
            NODE_DISPLAY_NAME_MAPPINGS.update(module.NODE_DISPLAY_NAME_MAPPINGS)
    
        logger.success(f"成功加载节点文件: {py_file.name}")

    except Exception as e:
        # 清理失败的半初始化模块，避免后续 import 读到脏状态
        try:
            sys.modules.pop(module_name, None)
        except Exception:
            pass
        logger.error(f"加载节点文件失败 {py_file.name}: {str(e)}")

# 额外加载子包（如分割节点集合）
try:
    import segment_nodes_li as _segment_nodes_li

    SEG_NODE_CLASS_MAPPINGS = _segment_nodes_li.NODE_CLASS_MAPPINGS
    SEG_NODE_DISPLAY_NAME_MAPPINGS = _segment_nodes_li.NODE_DISPLAY_NAME_MAPPINGS

    NODE_CLASS_MAPPINGS.update(SEG_NODE_CLASS_MAPPINGS)
    NODE_DISPLAY_NAME_MAPPINGS.update(SEG_NODE_DISPLAY_NAME_MAPPINGS)
    logger.success("成功加载子包: segment_nodes_li")
except Exception as e:
    logger.error(f"加载子包 segment_nodes_li 失败: {str(e)}")

# 修补 Cython 编译的 async 节点（仅编译部署时生效）
_patch_cython_async_nodes(NODE_CLASS_MAPPINGS)

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
