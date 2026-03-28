from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


MINIMAL_README = """# Banana Image Generator

这是一个面向 ComfyUI 的 Windows 白牌发布版本，提供 Banana 图像生成、自定义节点加载、余额查询与相关增强能力。

## 安装

1. 将仓库放入 `ComfyUI/custom_nodes/comfyui-banana-li`
2. 重启 ComfyUI
3. 在节点搜索中查找 `香蕉` 相关节点

## 说明

- 本版本仅保留运行所需文件
- 节点名称、提示和错误信息已做白牌化处理
- 如需技术支持，请联系当前版本提供方
"""

CHECK_RULES = {
    "README.md": [
        "李心宝",
        "Bilibili",
        "联系方式",
        "Li_18727107073",
        "space.bilibili.com",
        "feishu.cn",
    ],
    "web/extensions/token-balance.js": [
        "兑换积分",
        "Li_18727107073",
        "buy.xinbaoapi.dpdns.org",
        "添加UP主购买Key",
        "UP主二维码",
        "微信号复制失败",
    ],
    "__init__.py": [
        "心宝❤Banana Loader",
        "({node_name})",
    ],
    "loader_bootstrap.py": [
        "https://github.com/98624017/comfyui-banana-li",
        "https://github.com/98624017/comfyui-banana-li-linux",
        "https://github.com/98624017/comfyui-banana-li-mac",
    ],
    "pyproject.toml": [
        'PublisherId = "xinbao"',
    ],
    "snippet_manager.py": [
        "XinbaoPromptAssistant",
    ],
    "web/extensions/modelscope-channel-switch.js": [
        "Ensure XinbaoModelScopeCaption node definition",
    ],
    "web/extensions/xinbao_file_upload_fix.js": [
        "[Xinbao] File upload fix applied",
    ],
}

OPTIONAL_CHECK_FILES = {
    "snippet_manager.py",
}

RELEASE_TEXT_FILE_SUFFIXES = {".py", ".js", ".toml"}
RELEASE_TEXT_FILE_NAME_SUFFIXES = (".toml.example",)
RELEASE_TEXT_EXCLUDED_DIRS = {
    ".git",
    ".github",
    ".idea",
    ".pytest_cache",
    ".serena",
    ".venv",
    "__pycache__",
    "AIwork",
    "build",
    "dist",
    "docs",
    "example_workflows",
    "openspec",
    "sam_hq",
    "tests",
    "tools",
    "venv",
}

SOURCE_TEXT_REPLACEMENTS = {
    "心宝❤": "香蕉",
    "心宝": "香蕉",
    "新宝": "香蕉",
    "Xinbao/Image": "香蕉/Image",
    "Xinbao/Text": "香蕉/Text",
    "ComfyUI-xinbao/1.0": "ComfyUI-banana/1.0",
    'PublisherId = "xinbao"': 'PublisherId = "banana"',
    "XinbaoPromptAssistantNode": "BananaPromptAssistantNode",
    "XinbaoPromptAssistant": "BananaPromptAssistant",
    "[Xinbao] File upload fix applied (Monkey Patch Mode).": "[Banana] File upload fix applied (Monkey Patch Mode).",
    "Ensure XinbaoModelScopeCaption node definition has banana_models/modao_models.": "Ensure 香蕉多模态LLM反推节点定义包含 banana_models/modao_models。",
}


def _replace_required(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{path} 中未找到预期片段，无法安全替换")
    path.write_text(text.replace(old, new), encoding="utf-8")


def _replace_or_keep_updated(text: str, old: str, new: str, *, path: Path) -> str:
    if old in text:
        return text.replace(old, new)
    if new in text:
        return text
    raise RuntimeError(f"{path} 中未找到预期片段，无法安全替换")


def _rewrite_readme(root: Path) -> None:
    (root / "README.md").write_text(MINIMAL_README, encoding="utf-8")


def _rewrite_token_balance(root: Path) -> None:
    path = root / "web/extensions/token-balance.js"
    text = path.read_text(encoding="utf-8")
    text = _replace_or_keep_updated(
        text,
        'const WECHAT_ID = "Li_18727107073";\nconst QR_IMAGE_URL = new URL("./xinbao.png", import.meta.url).toString();\nconst ACTION_BUTTON_DEFS = [\n  { key: "wechat", label: "兑换积分" },\n  { key: "query", label: "查询余额" },\n  { key: "qr", label: "二维码" },\n];',
        'const WECHAT_ID = "";\nconst QR_IMAGE_URL = "";\nconst ACTION_BUTTON_DEFS = [\n  { key: "query", label: "查询余额" },\n];',
        path=path,
    )
    text = _replace_or_keep_updated(
        text,
        '  title.textContent = "添加UP主购买Key";',
        '  title.textContent = "当前版本未提供二维码";',
        path=path,
    )
    text = _replace_or_keep_updated(
        text,
        '  img.alt = "UP主二维码";',
        '  img.alt = "当前版本未提供二维码";',
        path=path,
    )
    text = _replace_or_keep_updated(
        text,
        '    img.alt = "二维码加载失败，请手动复制微信号";',
        '    img.alt = "当前版本未提供二维码";',
        path=path,
    )
    text = _replace_or_keep_updated(
        text,
        '    flashActionLabel(node, "wechat", "复制成功");',
        '    flashActionLabel(node, "query", "已刷新");',
        path=path,
    )
    text = _replace_or_keep_updated(
        text,
        '    console.error(`[${EXTENSION}] 微信号复制失败`, error);',
        '    console.error(`[${EXTENSION}] 按钮操作失败`, error);',
        path=path,
    )
    text = _replace_or_keep_updated(
        text,
        '    flashActionLabel(node, "wechat", "复制失败", 2400);',
        '    flashActionLabel(node, "query", "操作失败", 2400);',
        path=path,
    )
    text = _replace_or_keep_updated(
        text,
        '    buttonMap.wechat.onClick = () => {\n      window.open("https://buy.xinbaoapi.dpdns.org", "_blank");\n    };\n    buttonMap.query.onClick = () => {\n      void queryBalance(node);\n    };\n    buttonMap.qr.onClick = () => {\n      showQrOverlay();\n    };',
        '    if (buttonMap.query) {\n      buttonMap.query.onClick = () => {\n        void queryBalance(node);\n      };\n    }',
        path=path,
    )
    path.write_text(text, encoding="utf-8")

    qr_path = root / "web/extensions/xinbao.png"
    if qr_path.exists():
        qr_path.unlink()


def _rewrite_init(root: Path) -> None:
    path = root / "__init__.py"
    text = path.read_text(encoding="utf-8")
    text = _replace_or_keep_updated(
        text,
        '# 显示加载器标题（保留方框，只显示心宝❤Banana Loader）',
        '# 显示加载器标题（白牌版本）',
        path=path,
    )
    text = _replace_or_keep_updated(text, 'logger.header("心宝❤Banana Loader")', 'logger.header("香蕉 Loader")', path=path)
    text = _replace_or_keep_updated(
        text,
        'logger.info(f"心宝❤Banana version {__version__}")',
        'logger.info(f"香蕉 version {__version__}")',
        path=path,
    )
    text = _replace_or_keep_updated(
        text,
        'logger.info(f"   - {display_name} ({node_name})")',
        'logger.info(f"   - {display_name}")',
        path=path,
    )
    path.write_text(text, encoding="utf-8")


def _rewrite_loader_bootstrap(root: Path) -> None:
    path = root / "loader_bootstrap.py"
    text = path.read_text(encoding="utf-8")
    text = _replace_or_keep_updated(
        text,
        'REPO_URLS = {\n    "windows": "https://github.com/98624017/comfyui-banana-li",\n    "linux": "https://github.com/98624017/comfyui-banana-li-linux",\n    "darwin": "https://github.com/98624017/comfyui-banana-li-mac"\n}',
        'REPO_URLS = {\n    "windows": "请联系提供方获取 Windows 版本",\n    "linux": "请联系提供方获取 Linux 版本",\n    "darwin": "请联系提供方获取 macOS 版本"\n}',
        path=path,
    )
    path.write_text(text, encoding="utf-8")


def _remove_example_workflows(root: Path) -> None:
    example_dir = root / "example_workflows"
    if example_dir.exists():
        shutil.rmtree(example_dir)


def _is_root_comfyui_workflow_json(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("nodes"), list)


def _remove_root_workflow_jsons(root: Path) -> None:
    for path in root.glob("*.json"):
        if _is_root_comfyui_workflow_json(path):
            path.unlink()


def _iter_release_text_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(root).parts
        if any(part in RELEASE_TEXT_EXCLUDED_DIRS for part in relative_parts[:-1]):
            continue
        if path.suffix in RELEASE_TEXT_FILE_SUFFIXES or path.name.endswith(RELEASE_TEXT_FILE_NAME_SUFFIXES):
            yield path


def _rewrite_user_visible_source_text(root: Path) -> None:
    for path in _iter_release_text_files(root):
        text = path.read_text(encoding="utf-8")
        updated = text
        for old, new in SOURCE_TEXT_REPLACEMENTS.items():
            updated = updated.replace(old, new)
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def prepare_white_label_release(root: Path) -> None:
    _rewrite_readme(root)
    _rewrite_token_balance(root)
    _rewrite_init(root)
    _rewrite_loader_bootstrap(root)
    _remove_example_workflows(root)
    _remove_root_workflow_jsons(root)
    _rewrite_user_visible_source_text(root)


def check_white_label_release(root: Path) -> list[str]:
    errors: list[str] = []
    for relative_path, forbidden_terms in CHECK_RULES.items():
        path = root / relative_path
        if not path.exists():
            if relative_path not in OPTIONAL_CHECK_FILES:
                errors.append(f"缺少校验文件: {relative_path}")
            continue
        content = path.read_text(encoding="utf-8")
        for term in forbidden_terms:
            if term in content:
                errors.append(f"{relative_path} 仍包含敏感内容: {term}")

    if (root / "example_workflows").exists():
        errors.append("example_workflows 目录仍然存在")
    if (root / "web/extensions/xinbao.png").exists():
        errors.append("web/extensions/xinbao.png 仍然存在")
    for path in root.glob("*.json"):
        if _is_root_comfyui_workflow_json(path):
            errors.append(f"{path.relative_to(root)} 仍然存在 ComfyUI 工作流 JSON")

    for path in _iter_release_text_files(root):
        content = path.read_text(encoding="utf-8")
        if "心宝" in content or "新宝" in content:
            errors.append(f"{path.relative_to(root)} 仍包含未替换的中文品牌词")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="准备或校验 Windows 白牌发布工作区")
    parser.add_argument("--root", default=".", help="目标工作区根目录")
    parser.add_argument("--check", action="store_true", help="仅校验，不修改文件")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"目标目录不存在: {root}")

    if not args.check:
        prepare_white_label_release(root)

    errors = check_white_label_release(root)
    if errors:
        for item in errors:
            message = f"[white-label-check] {item}"
            try:
                print(message)
            except UnicodeEncodeError:
                safe_message = message.encode("ascii", errors="backslashreplace").decode("ascii")
                print(safe_message)
        return 1

    print("[white-label-check] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
