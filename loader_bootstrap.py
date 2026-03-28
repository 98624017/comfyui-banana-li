import os
import sys
import platform

# GitHub Repositories
REPO_URLS = {
    "windows": "https://github.com/98624017/comfyui-banana-li",
    "linux": "https://github.com/98624017/comfyui-banana-li-linux",
    "darwin": "https://github.com/98624017/comfyui-banana-li-mac"
}

def _read_repo_platform(current_dir):
    """从 pyproject.toml 的 name 字段推断仓库所属平台。

    CI 构建时会将 name 修改为：
      - comfyui-banana-li       → windows
      - comfyui-banana-li-linux → linux
      - comfyui-banana-li-mac   → darwin
    返回 None 表示无法判断（源码仓库或格式异常）。
    """
    toml_path = os.path.join(current_dir, "pyproject.toml")
    if not os.path.isfile(toml_path):
        return None
    try:
        with open(toml_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("name"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        name = parts[1].strip().strip('"').strip("'")
                        if name.endswith("-mac"):
                            return "darwin"
                        if name.endswith("-linux"):
                            return "linux"
                        return "windows"
    except Exception:
        pass
    return None


def ensure_binaries():
    """检查当前平台对应的二进制文件是否存在，并检测跨平台混装。"""

    current_dir = os.path.dirname(os.path.abspath(__file__))
    system = platform.system().lower()

    # 期望的后缀（macOS 和 Linux 统一使用 .so）
    if system == "windows":
        expected_suffix = ".pyd"
        wrong_suffix = ".so"
        repo_key = "windows"
    elif system in ("linux", "darwin"):
        expected_suffix = ".so"
        wrong_suffix = ".pyd"
        repo_key = system
    else:
        # 其它平台暂不支持检查，直接跳过
        return

    # 通过 pyproject.toml 名称检测 Linux/macOS 混装
    repo_platform = _read_repo_platform(current_dir)
    if repo_platform is not None and repo_platform != system:
        platform_names = {"windows": "Windows", "linux": "Linux", "darwin": "macOS"}
        print("\n\033[91m" + "=" * 60)
        print(f" ERROR: Wrong repository for your platform!")
        print("=" * 60 + "\033[0m")
        print(f" Current OS: {platform_names.get(system, system)}")
        print(f" Repo built for: {platform_names.get(repo_platform, repo_platform)}")
        print(f"\n Please use the correct repository:")
        print(f" -> {REPO_URLS.get(repo_key, 'Unknown Repo')}")
        print("\n" + "=" * 60 + "\n")
        return

    # 检查是否存在期望的文件
    has_expected = False
    has_wrong = False

    for root, dirs, files in os.walk(current_dir):
        # 排除部分目录
        if ".git" in root or "__pycache__" in root:
            continue

        for file in files:
            if file.endswith(expected_suffix):
                has_expected = True
            elif file.endswith(wrong_suffix):
                has_wrong = True

        if has_expected:
            break

    if not has_expected:
        print("\n\033[91m" + "=" * 60)
        print(f" ERROR: Missing binary files for {system}!")
        print("=" * 60 + "\033[0m")

        if has_wrong:
            wrong_platform = "Linux/macOS (.so)" if system == "windows" else "Windows (.pyd)"
            print(f" Detected binaries for other platforms: {wrong_platform}")
            print(" You may have cloned the wrong repository for your OS.")

        print(f"\n Please use the correct repository for {system}:")
        print(f" -> {REPO_URLS.get(repo_key, 'Unknown Repo')}")
        print("\n" + "=" * 60 + "\n")

if __name__ == "__main__":
    ensure_binaries()
