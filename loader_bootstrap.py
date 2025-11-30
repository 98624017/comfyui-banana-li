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

def download_file(url, target_path):
    """下载文件并显示进度"""
    print(f"Downloading {url} to {target_path}...")
    
    # 忽略 SSL 证书验证 (防止某些环境报错)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    try:
        with urllib.request.urlopen(url, context=ctx) as response, open(target_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
        print("Download complete.")
        return True
    except Exception as e:
        print(f"Failed to download {url}: {e}")
        return False

def get_modules_manifest(base_url):
    """下载并解析 modules.json"""
    manifest_url = f"{base_url}/modules.json"
    print(f"Fetching manifest from {manifest_url}...")
    
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

def ensure_binaries():
    """检查并下载缺失的二进制文件"""
    print("Banana-Li: Checking for binary extensions...")
    
    base_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/download/{RELEASE_TAG}"
    
    # 尝试获取动态模块列表
    modules_list = get_modules_manifest(base_url)
    if not modules_list:
        print("WARNING: Could not fetch modules.json. Using fallback list (empty).")
        modules_list = MODULES
    
    platform_suffix = get_platform_suffix()
    target_suffix = get_target_suffix()
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
    for module in modules_list:
        # 构建本地目标路径
        module_path = module.replace("/", os.sep)
        target_filename = f"{module_path}{target_suffix}"
        target_full_path = os.path.join(current_dir, target_filename)
        
        # 检查文件是否存在
        if os.path.exists(target_full_path):
            continue
            
        # [NEW] 检查源码是否存在。如果存在源码（开发环境），则不需要下载二进制文件
        source_filename = f"{module_path}.py"
        source_full_path = os.path.join(current_dir, source_filename)
        if os.path.exists(source_full_path):
            # print(f"Source file found for {module}, skipping binary download.")
            continue
            
        print(f"Missing binary: {target_filename}")
        
        # 确保目录存在
        os.makedirs(os.path.dirname(target_full_path), exist_ok=True)
        
        # 构建下载 URL
        # GitHub Release 中的文件名是扁平的 (没有目录)，且基于 basename
        # 例如 segment_nodes_li/imagefunc -> imagefunc.pyd
        
        module_basename = module.split("/")[-1]
        download_filename = f"{module_basename}.{platform_suffix}"
        url = f"{base_url}/{download_filename}"
        
        success = download_file(url, target_full_path)
        if not success:
            print(f"WARNING: Failed to download binary for {module}. The node may not work.")

if __name__ == "__main__":
    ensure_binaries()
