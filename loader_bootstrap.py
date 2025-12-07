import os
import sys
import platform
import urllib.request
import ssl
import shutil
import json
import time

# GitHub Release 配置
REPO_OWNER = "98624017"
REPO_NAME = "comfyui-banana-li"

# 默认模块列表 (Fallback)
MODULES = []

def get_plugin_version():
    """从 pyproject.toml 读取插件版本"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    pyproject_path = os.path.join(current_dir, "pyproject.toml")

    try:
        with open(pyproject_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("version"):
                    parts = line.split("=")
                    if len(parts) == 2:
                        version = parts[1].strip().strip('"').strip("'")
                        return version
    except Exception as e:
        print(f"Banana-Li: Failed to read version from pyproject.toml: {e}")

    return "0.0.0"  # Fallback

def get_platform_suffix():
    """获取当前平台的后缀"""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        return "pyd"  # 根据日志，Windows 构建生成的是 .pyd
    elif system == "linux":
        return "so"  # Linux release assets are named .so
    elif system == "darwin":
        return "darwin.so"  # Mac 使用 .darwin.so
    else:
        raise RuntimeError(f"Unsupported platform: {system} {machine}")

def get_target_suffix():
    """获取目标文件的后缀"""
    system = platform.system().lower()
    if system == "windows":
        return ".pyd"
    elif system == "linux":
        return ".so"
    elif system == "darwin":
        return ".darwin.so"
    else:
        return ".so"

class SimpleProgressBar:
    def __init__(self, total, width=30):
        self.total = total
        self.width = width
        self.current = 0

    def update(self, filename=""):
        self.current += 1
        percent = self.current / self.total
        filled_length = int(self.width * percent)
        bar = "=" * filled_length + ">" + " " * (self.width - filled_length - 1)
        bar = bar[:self.width]

        message = f"\r[{bar}] {self.current}/{self.total} - {filename}"
        sys.stdout.write(f"{message:<100}")
        sys.stdout.flush()

    def finish(self):
        sys.stdout.write("\n")

def download_file(url, target_path, retries=3):
    """下载文件 (带重试机制)"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, context=ctx, timeout=30) as response, open(target_path, "wb") as out_file:
                shutil.copyfileobj(response, out_file)
            return True
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                print(f"\nFailed to download {url} after {retries} attempts: {e}")
                return False

def get_modules_manifest(base_url):
    """下载并解析 modules.json"""
    manifest_url = f"{base_url}/modules.json"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(manifest_url, context=ctx) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data
    except Exception as e:
        print(f"Failed to fetch manifest: {e}")
        return None

def cleanup_linux_binaries(directory):
    """递归清理目录下的 .pyd 文件 (Linux/Mac 专用)"""
    print("Banana-Li: Cleaning up Windows binaries (.pyd) for non-Windows environment...")
    count = 0
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(".pyd"):
                file_path = os.path.join(root, file)
                try:
                    os.remove(file_path)
                    count += 1
                except Exception as e:
                    print(f"Failed to remove {file_path}: {e}")
    if count > 0:
        print(f"Banana-Li: Removed {count} .pyd files.")

def ensure_binaries():
    """检查并下载缺失的二进制文件"""
    if platform.system() == "Windows":
        return

    print(f"Banana-Li: Checking for binary extensions ({platform.system()})...")

    current_dir = os.path.dirname(os.path.abspath(__file__))

    cleanup_linux_binaries(current_dir)

    current_version = get_plugin_version()
    release_tag = f"v{current_version}"

    version_file = os.path.join(current_dir, ".banana_version")
    installed_version = None
    if os.path.exists(version_file):
        try:
            with open(version_file, "r", encoding="utf-8") as f:
                installed_version = f.read().strip()
        except Exception as e:
            print(f"Failed to read version file: {e}")

    if installed_version != current_version:
        print(f"Banana-Li: Version mismatch (Installed: {installed_version}, Current: {current_version}). Cleaning up old binaries...")
        for root, dirs, files in os.walk(current_dir):
            for file in files:
                if file.endswith(".so"):
                    try:
                        os.remove(os.path.join(root, file))
                    except Exception:
                        pass

        try:
            with open(version_file, "w", encoding="utf-8") as f:
                f.write(current_version)
        except Exception as e:
            print(f"Failed to write version file: {e}")

    base_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/download/{release_tag}"

    modules_list = get_modules_manifest(base_url)
    if not modules_list:
        print(f"WARNING: Could not fetch modules.json from {release_tag}. Using fallback list (empty).")
        modules_list = MODULES

    platform_suffix = get_platform_suffix()
    target_suffix = get_target_suffix()

    missing_files = []

    for module in modules_list:
        module_path = module.replace("/", os.sep)
        target_filename = f"{module_path}{target_suffix}"
        target_full_path = os.path.join(current_dir, target_filename)

        if os.path.exists(target_full_path):
            continue

        source_filename = f"{module_path}.py"
        source_full_path = os.path.join(current_dir, source_filename)
        if os.path.exists(source_full_path):
            continue

        missing_files.append({
            "module": module,
            "target_path": target_full_path,
            "filename": target_filename,
        })

    if not missing_files:
        return

    print("心宝❤Banana正在下载必要组件，请耐心等待... (首次运行需要下载，后续无需等待)")

    import concurrent.futures
    max_workers = 8
    total_files = len(missing_files)

    progress_bar = SimpleProgressBar(total_files)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_file = {}

        for item in missing_files:
            os.makedirs(os.path.dirname(item["target_path"]), exist_ok=True)

            module_basename = item["module"].split("/")[-1]
            download_filename = f"{module_basename}.{platform_suffix}"
            url = f"{base_url}/{download_filename}"

            future = executor.submit(download_file, url, item["target_path"])
            future_to_file[future] = item["filename"]

        for future in concurrent.futures.as_completed(future_to_file):
            filename = future_to_file[future]
            try:
                success = future.result()
                if success:
                    progress_bar.update(filename)
                else:
                    progress_bar.update(f"Failed: {filename}")
                    print(f"\nWARNING: Failed to download binary for {filename}. The node may not work.")
            except Exception as exc:
                progress_bar.update(f"Error: {filename}")
                print(f"\nGenerated an exception for {filename}: {exc}")

    progress_bar.finish()
    print("Download complete.")

if __name__ == "__main__":
    ensure_binaries()
