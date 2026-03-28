"""验证 XinbaoBatchToPSD 在 `PixelLayer.frompil(parent=...)` 新签名下可正常工作。

该测试通过 stub `psd_tools`/`torch`/`folder_paths`，避免依赖真实运行环境。
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import types
from pathlib import Path

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]


def _install_stub_modules(output_dir: Path) -> None:
    # stub: torch（避免安装真实 torch；仅满足 write_psd 生成 preview 的调用）
    torch_stub = types.ModuleType("torch")

    class FakeTensor:  # noqa: N801 - 与 torch.Tensor 命名保持一致
        def __init__(self, array):
            self._array = array

        def unsqueeze(self, _dim: int):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self._array

    def from_numpy(array):
        return FakeTensor(array)

    torch_stub.Tensor = FakeTensor  # type: ignore[attr-defined]
    torch_stub.from_numpy = from_numpy  # type: ignore[attr-defined]
    sys.modules["torch"] = torch_stub

    # stub: folder_paths（避免依赖 ComfyUI）
    folder_paths_stub = types.ModuleType("folder_paths")
    folder_paths_stub.get_output_directory = lambda: str(output_dir)  # type: ignore[attr-defined]
    sys.modules["folder_paths"] = folder_paths_stub

    # stub: psd_tools（模拟新版本要求 parent）
    psd_tools_stub = types.ModuleType("psd_tools")
    psd_tools_api_stub = types.ModuleType("psd_tools.api")
    psd_tools_api_layers_stub = types.ModuleType("psd_tools.api.layers")

    class FakePSDImage(list):
        last_created = None

        def __init__(self, mode: str, size: tuple[int, int], depth: int):
            super().__init__()
            self.mode = mode
            self.size = size
            self.depth = depth

        @classmethod
        def new(cls, mode: str, size: tuple[int, int], depth: int = 8):
            instance = cls(mode=mode, size=size, depth=depth)
            cls.last_created = instance
            return instance

        def save(self, path: str) -> None:
            Path(path).write_bytes(b"FAKE_PSD")

    class FakePixelLayer:
        calls: list[tuple] = []

        def __init__(self, name: str):
            self.name = name
            self.unicode_name = ""
            self.left = 0
            self.top = 0

        @classmethod
        def frompil(cls, pil_image, parent, name=None):
            layer = cls(name=name or "layer")
            cls.calls.append((pil_image.size, parent, name))
            assert parent is FakePSDImage.last_created, "parent 应为 PSD 根对象"
            # 模拟 psd-tools 新版行为：frompil(parent=...) 会自动 append
            parent.append(layer)
            return layer

    psd_tools_stub.PSDImage = FakePSDImage  # type: ignore[attr-defined]
    psd_tools_api_layers_stub.PixelLayer = FakePixelLayer  # type: ignore[attr-defined]

    sys.modules["psd_tools"] = psd_tools_stub
    sys.modules["psd_tools.api"] = psd_tools_api_stub
    sys.modules["psd_tools.api.layers"] = psd_tools_api_layers_stub


def _import_fresh(module_name: str):
    sys.path.insert(0, str(REPO_ROOT))
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        out_dir = Path(temp_dir) / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

        _install_stub_modules(out_dir)
        module = _import_fresh("xinbao_psd_tool")

        node = module.XinbaoBatchToPSD()
        img1 = Image.new("RGBA", (16, 16), (255, 0, 0, 255))
        img2 = Image.new("RGBA", (16, 16), (0, 255, 0, 255))

        result = node.write_psd([img1, img2], "test_psd")
        psd_path, preview = result["result"]

        assert Path(psd_path).exists(), "应生成 psd 文件"
        assert getattr(preview, "unsqueeze", None) is not None, "应返回可用的 preview 张量对象（stub）"

        # 2 张图 -> 2 个图层
        fake_pixel_layer = sys.modules["psd_tools.api.layers"].PixelLayer  # type: ignore[attr-defined]
        assert len(fake_pixel_layer.calls) == 2, "应创建与输入图片数量一致的图层"
        assert len(sys.modules["psd_tools"].PSDImage.last_created) == 2, "不应重复 append 图层"

    print("psd write_psd(parent required) passed")


if __name__ == "__main__":
    main()
