"""验证 psd-tools PixelLayer.frompil 的 parent 参数兼容逻辑。

该测试不依赖真实的 ComfyUI / psd-tools / torch 安装：
- 通过 stub `torch`、`folder_paths` 让模块可被导入
- 通过 fake PixelLayer 类模拟不同版本的 `frompil` 签名
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]


def _install_stub_modules() -> None:
    """安装最小 stub，确保 `xinbao_psd_tool` 可被导入。"""

    if "torch" not in sys.modules:
        torch_stub = types.ModuleType("torch")

        class _Tensor:  # noqa: N801 - 与 torch.Tensor 命名保持一致
            pass

        torch_stub.Tensor = _Tensor  # type: ignore[attr-defined]
        sys.modules["torch"] = torch_stub

    if "folder_paths" not in sys.modules:
        folder_paths_stub = types.ModuleType("folder_paths")

        def get_output_directory() -> str:
            return str(REPO_ROOT / "output")

        folder_paths_stub.get_output_directory = get_output_directory  # type: ignore[attr-defined]
        sys.modules["folder_paths"] = folder_paths_stub


class _FakePixelLayerNoParent:
    calls: list[tuple] = []

    @classmethod
    def frompil(cls, pil_image, name=None):
        cls.calls.append(("no_parent", pil_image.size, name))
        return {"variant": "no_parent", "name": name}


class _FakePixelLayerWithParent:
    calls: list[tuple] = []

    @classmethod
    def frompil(cls, pil_image, parent, name=None):
        cls.calls.append(("with_parent", pil_image.size, parent, name))
        return {"variant": "with_parent", "parent": parent, "name": name}


class _FakePixelLayerWithParentPosOnly:
    calls: list[tuple] = []

    @classmethod
    def frompil(cls, pil_image, parent, /, name=None):
        cls.calls.append(("with_parent_posonly", pil_image.size, parent, name))
        return {"variant": "with_parent_posonly", "parent": parent, "name": name}


def _assert_no_parent_path(module) -> None:
    module.PixelLayer = _FakePixelLayerNoParent  # type: ignore[attr-defined]
    _FakePixelLayerNoParent.calls.clear()

    parent = object()
    image = Image.new("RGBA", (4, 5), (0, 0, 0, 0))
    layer, auto_attached = module._pixel_layer_frompil_compat(image, parent, name="layer01")  # noqa: SLF001

    assert layer["variant"] == "no_parent"
    assert auto_attached is False
    assert _FakePixelLayerNoParent.calls == [("no_parent", (4, 5), "layer01")]


def _assert_parent_keyword_path(module) -> None:
    module.PixelLayer = _FakePixelLayerWithParent  # type: ignore[attr-defined]
    _FakePixelLayerWithParent.calls.clear()

    parent = object()
    image = Image.new("RGBA", (6, 7), (0, 0, 0, 0))
    layer, auto_attached = module._pixel_layer_frompil_compat(image, parent, name="layer02")  # noqa: SLF001

    assert layer["variant"] == "with_parent"
    assert auto_attached is True
    assert _FakePixelLayerWithParent.calls == [("with_parent", (6, 7), parent, "layer02")]


def _assert_parent_posonly_path(module) -> None:
    module.PixelLayer = _FakePixelLayerWithParentPosOnly  # type: ignore[attr-defined]
    _FakePixelLayerWithParentPosOnly.calls.clear()

    parent = object()
    image = Image.new("RGBA", (8, 9), (0, 0, 0, 0))
    layer, auto_attached = module._pixel_layer_frompil_compat(image, parent, name="layer03")  # noqa: SLF001

    assert layer["variant"] == "with_parent_posonly"
    assert auto_attached is True
    assert _FakePixelLayerWithParentPosOnly.calls == [
        ("with_parent_posonly", (8, 9), parent, "layer03")
    ]


def main() -> None:
    _install_stub_modules()
    sys.path.insert(0, str(REPO_ROOT))

    import xinbao_psd_tool as module

    _assert_no_parent_path(module)
    _assert_parent_keyword_path(module)
    _assert_parent_posonly_path(module)

    print("psd frompil compatibility passed")


if __name__ == "__main__":
    main()
