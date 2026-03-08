import os
import sys
import platform

# GitHub Repositories
REPO_URLS = {
    "windows": "https://github.com/98624017/comfyui-banana-li",
    "linux": "https://github.com/98624017/comfyui-banana-li-linux",
    "darwin": "https://github.com/98624017/comfyui-banana-li-mac"
}

def ensure_binaries():
    """仅检查当前平台对应的二进制文件是否存在"""
    
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
        print("\n\033[91m" + "="*60)
        print(f" ERROR: Missing binary files for {system}!")
        print("="*60 + "\033[0m")
        
        if has_wrong:
            wrong_platform = "Linux/macOS (.so)" if system == "windows" else "Windows (.pyd)"
            print(f" Detected binaries for other platforms: {wrong_platform}")
            print(" You may have cloned the wrong repository for your OS.")
            
        print(f"\n Please use the correct repository for {system}:")
        print(f" -> {REPO_URLS.get(repo_key, 'Unknown Repo')}")
        print("\n" + "="*60 + "\n")
    else:
        # 正常情况
        pass

if __name__ == "__main__":
    ensure_binaries()
