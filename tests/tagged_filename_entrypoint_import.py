"""模拟编译发布目录，仅保留带 ABI 风格后缀的模块文件名，验证入口仍可加载。"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = REPO_ROOT / "__init__.py"


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        plugin_dir = Path(temp_dir) / "comfyui-banana-li"
        plugin_dir.mkdir()

        shutil.copy2(ENTRYPOINT, plugin_dir / "__init__.py")
        _write_text(plugin_dir / "pyproject.toml", '[project]\nversion = "0.0.0"\n')
        _write_text(
            plugin_dir / "loader_bootstrap.py",
            "def ensure_binaries():\n    pass\n",
        )
        _write_text(
            plugin_dir / "logger.abi3.py",
            "\n".join(
                [
                    "class DummyLogger:",
                    "    def header(self, *args, **kwargs):",
                    "        pass",
                    "    def info(self, *args, **kwargs):",
                    "        pass",
                    "    def success(self, *args, **kwargs):",
                    "        pass",
                    "    def warning(self, *args, **kwargs):",
                    "        pass",
                    "    def error(self, *args, **kwargs):",
                    "        pass",
                    "",
                    "logger = DummyLogger()",
                    "",
                ]
            ),
        )
        _write_text(
            plugin_dir / "demo_node.abi3.py",
            "\n".join(
                [
                    "from logger import logger",
                    "",
                    'MARKER = "tagged-ok"',
                    'NODE_CLASS_MAPPINGS = {"DemoNode": object}',
                    'NODE_DISPLAY_NAME_MAPPINGS = {"DemoNode": "DemoNode"}',
                    'logger.info("demo node loaded")',
                    "",
                ]
            ),
        )

        module_name = str(plugin_dir)
        spec = importlib.util.spec_from_file_location(module_name, plugin_dir / "__init__.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("无法为临时插件入口创建加载 spec")

        module = importlib.util.module_from_spec(spec)
        snapshot = {name: sys.modules.get(name) for name in ("logger", "demo_node", module_name)}

        try:
            spec.loader.exec_module(module)

            assert "DemoNode" in module.NODE_CLASS_MAPPINGS, "应加载 ABI 风格命名的节点模块"

            logger_module = sys.modules.get("logger")
            assert logger_module is not None, "应注册 logger 基础模块名"
            assert hasattr(logger_module, "logger"), "logger 模块应暴露 logger 实例"

            demo_module = sys.modules.get("demo_node")
            assert demo_module is not None, "应注册 demo_node 基础模块名"
            assert getattr(demo_module, "MARKER", None) == "tagged-ok", "应可通过基础模块名访问节点模块"
        finally:
            for name, original in snapshot.items():
                if original is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original

    print("tagged filename entrypoint import passed")


if __name__ == "__main__":
    main()
