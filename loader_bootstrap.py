import os
import sys
import platform
import urllib.request
import ssl
import shutil
import hashlib
import json

# GitHub Release 配置
REPO_OWNER = "98624017"
REPO_NAME = "comfyui-banana-li"
RELEASE_TAG = "latest" # 或者指定版本

# 默认模块列表 (Fallback)
MODULES = []

def get_platform_suffix():
    """获取当前平台的后缀"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    
    if system == "windows":
        return "pyd" # 根据日志，Windows 构建生成的是 .pyd
    elif system == "linux":
        return "abi3.so" # Linux 通常使用 abi3.so
    else:
        raise RuntimeError(f"Unsupported platform: {system} {machine}")

def get_target_suffix():
    """获取目标文件的后缀"""
    system = platform.system().lower()
    if system == "windows":
        return ".pyd"
    elif system == "linux":
        return ".so"
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
        # Ensure bar doesn't exceed width
        bar = bar[:self.width]
        
        # Clear line and print progress
        # Pad with spaces to ensure we overwrite previous longer lines
        message = f"\r[{bar}] {self.current}/{self.total} - {filename}"
        sys.stdout.write(f"{message:<100}") # Pad to 100 chars
        sys.stdout.flush()

    def finish(self):
        sys.stdout.write("\n")

import time

def download_file(url, target_path, retries=3):
    """下载文件 (带重试机制)"""
    # print(f"Downloading {url} to {target_path}...") # Suppress verbose logging
    
    # 忽略 SSL 证书验证 (防止某些环境报错)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, context=ctx, timeout=30) as response, open(target_path, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
            return True
        except Exception as e:
            if attempt < retries - 1:
                # print(f"\nRetrying {url} ({attempt + 1}/{retries})... Error: {e}")
                time.sleep(1) # Wait 1 second before retrying
            else:
                print(f"\nFailed to download {url} after {retries} attempts: {e}")
                return False

def get_modules_manifest(base_url):
    """下载并解析 modules.json"""
    manifest_url = f"{base_url}/modules.json"
    # print(f"Fetching manifest from {manifest_url}...") # Suppress verbose logging
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    try:
        with urllib.request.urlopen(manifest_url, context=ctx) as response:
import os
import sys
import platform
import urllib.request
import ssl
import shutil
import hashlib
import json

# GitHub Release 配置
REPO_OWNER = "98624017"
REPO_NAME = "comfyui-banana-li"
RELEASE_TAG = "latest" # 或者指定版本

# 默认模块列表 (Fallback)
MODULES = []

def get_platform_suffix():
    """获取当前平台的后缀"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    
    if system == "windows":
        return "pyd" # 根据日志，Windows 构建生成的是 .pyd
    elif system == "linux":
        return "abi3.so" # Linux 通常使用 abi3.so
    else:
        raise RuntimeError(f"Unsupported platform: {system} {machine}")

def get_target_suffix():
    """获取目标文件的后缀"""
    system = platform.system().lower()
    if system == "windows":
        return ".pyd"
    elif system == "linux":
        return ".so"
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
        # Ensure bar doesn't exceed width
        bar = bar[:self.width]
        
        # Clear line and print progress
        # Pad with spaces to ensure we overwrite previous longer lines
        message = f"\r[{bar}] {self.current}/{self.total} - {filename}"
        sys.stdout.write(f"{message:<100}") # Pad to 100 chars
        sys.stdout.flush()

    def finish(self):
        sys.stdout.write("\n")

import time

def download_file(url, target_path, retries=3):
    """下载文件 (带重试机制)"""
    # print(f"Downloading {url} to {target_path}...") # Suppress verbose logging
    
    # 忽略 SSL 证书验证 (防止某些环境报错)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, context=ctx, timeout=30) as response, open(target_path, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
            return True
        except Exception as e:
            if attempt < retries - 1:
                # print(f"\nRetrying {url} ({attempt + 1}/{retries})... Error: {e}")
                time.sleep(1) # Wait 1 second before retrying
            else:
                print(f"\nFailed to download {url} after {retries} attempts: {e}")
                return False

def get_modules_manifest(base_url):
    """下载并解析 modules.json"""
    manifest_url = f"{base_url}/modules.json"
    # print(f"Fetching manifest from {manifest_url}...") # Suppress verbose logging
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    try:
        with urllib.request.urlopen(manifest_url, context=ctx) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data
    except Exception as e:
        print(f"Failed to fetch manifest: {e}")
        return None

def get_remote_release_info(repo_owner, repo_name, release_tag):
    """获取远程 Release 信息 (用于检查更新)"""
    api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/releases/latest"
    if release_tag != "latest":
        api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/releases/tags/{release_tag}"
    
    try:
        # 使用简单的 urllib 请求，不依赖 requests
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        req = urllib.request.Request(api_url)
        req.add_header("User-Agent", "ComfyUI-Banana-Li-Updater")
        
        with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data.get("id")
    except Exception as e:
        print(f"Banana-Li: Failed to check for updates: {e}")
        return None

def cleanup_linux_binaries(directory):
    """递归清理目录下的 .pyd 文件 (Linux 专用)"""
    print("Banana-Li: Cleaning up Windows binaries (.pyd) for Linux environment...")
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
    
    # Windows 平台直接跳过下载 (假设已随仓库克隆)
    if platform.system() == "Windows":
        # print("Banana-Li: Windows detected. Using local binaries.")
        return

    print("Banana-Li: Checking for binary extensions (Linux)...")
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Linux 平台清理 .pyd 文件
    cleanup_linux_binaries(current_dir)
    
    # 自动更新逻辑
    release_id_file = os.path.join(current_dir, ".banana_release_id")
    remote_id = get_remote_release_info(REPO_OWNER, REPO_NAME, RELEASE_TAG)
    
    if remote_id:
        local_id = None
        if os.path.exists(release_id_file):
            try:
                with open(release_id_file, "r") as f:
                    local_id = int(f.read().strip())
            except:
                pass
        
        if local_id != remote_id:
            print(f"Banana-Li: New version detected (Release ID: {remote_id}). Updating binaries...")
            # 强制清理所有 .so 文件以触发重新下载
            for root, dirs, files in os.walk(current_dir):
                for file in files:
                    if file.endswith(".so"):
                        try:
                            os.remove(os.path.join(root, file))
                        except:
                            pass
            
            # 更新本地 ID
            try:
                with open(release_id_file, "w") as f:
                    f.write(str(remote_id))
            except Exception as e:
                print(f"Failed to write release ID: {e}")
    
    base_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/download/{RELEASE_TAG}"
    
    # 尝试获取动态模块列表
    modules_list = get_modules_manifest(base_url)
    if not modules_list:
        print("WARNING: Could not fetch modules.json. Using fallback list (empty).")
        modules_list = MODULES
    
    platform_suffix = get_platform_suffix()
    target_suffix = get_target_suffix()
    
    missing_files = []
    
    for module in modules_list:
        # 构建本地目标路径
        module_path = module.replace("/", os.sep)
        target_filename = f"{module_path}{target_suffix}"
        target_full_path = os.path.join(current_dir, target_filename)
        
        # 检查文件是否存在
        if os.path.exists(target_full_path):
            continue
            
        # 检查源码是否存在。如果存在源码（开发环境），则不需要下载二进制文件
        source_filename = f"{module_path}.py"
        source_full_path = os.path.join(current_dir, source_filename)
        if os.path.exists(source_full_path):
            # print(f"Source file found for {module}, skipping binary download.")
            continue
            
        # print(f"Missing binary: {target_filename}")
        missing_files.append({
            "module": module,
            "target_path": target_full_path,
            "filename": target_filename
        })
        
    if not missing_files:
        return

    print("心宝❤Banana正在下载必要组件，请耐心等待... (首次运行需要下载，后续无需等待)")
    
    # 使用 ThreadPoolExecutor 并发下载
    import concurrent.futures
    max_workers = 8 
    total_files = len(missing_files)
    
    # 初始化进度条
    progress_bar = SimpleProgressBar(total_files)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_file = {}
        
        for item in missing_files:
            # 确保目录存在 (在主线程执行，避免竞争)
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
