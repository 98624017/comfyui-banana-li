"""入口加载冒烟测试（发布仓库版）。

说明：
1) 发布仓库会刻意删除 tests/ 目录，避免暴露测试源码与增加仓库体积。
2) 该脚本放在 .github/scripts/ 下，供 GitHub Actions 执行。
3) 目标：在不依赖 ComfyUI 运行时的情况下，模拟其“路径式模块名”加载行为，
   验证插件入口 __init__.py 可被稳定导入，并且关键导出存在。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


# .github/scripts/<file>.py -> parents[2] 才是仓库根目录
REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "__init__.py"


def main() -> None:
    # 使用“路径字符串”作为模块名，模拟 ComfyUI 在某些环境下的加载方式。
    module_name = str(REPO_ROOT / "custom_nodes" / "comfyui-banana-li")

    spec = importlib.util.spec_from_file_location(module_name, ENTRYPOINT)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法为插件入口创建加载 spec")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    node_map = getattr(module, "NODE_CLASS_MAPPINGS", None)
    display_map = getattr(module, "NODE_DISPLAY_NAME_MAPPINGS", None)

    assert isinstance(node_map, dict), "NODE_CLASS_MAPPINGS 必须是 dict"
    assert isinstance(display_map, dict), "NODE_DISPLAY_NAME_MAPPINGS 必须是 dict"
    assert "logger" in sys.modules, "应注册 logger 模块别名，供后续 import logger 复用"

    print("smoke entrypoint import passed")


if __name__ == "__main__":
    main()

