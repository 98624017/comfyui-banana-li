from setuptools import setup, Extension, find_packages
from Cython.Build import cythonize
import Cython.Compiler.Options
Cython.Compiler.Options.docstrings = False
import os
import sys

# 排除的目录和文件
EXCLUDE_DIRS = {'web', '.git', '.github', '__pycache__', 'build', 'dist', '.vscode', '.idea', 'tools', '.serena', '.test', '.doc', 'openspec', 'sam_hq'}
EXCLUDE_FILES = {
    'setup.py', '__init__.py', 'loader_bootstrap.py',
    'blendmodes.py', 'imagefunc.py',
    # Cython Limited API 不支持 generator/async generator，
    # 含 @contextmanager/@asynccontextmanager + yield 的文件必须保留源码
    'workflow_parallel.py', 'video_task_manager.py',
    # 调试/复现脚本，不应编译发布
    'reproduce_issue_ms.py', 'reproduce_toml_issue.py', 'poll_manual.py',
}

if __name__ == "__main__":
    extensions = []

    for root, dirs, files in os.walk("."):
        # 过滤目录：排除显式列表 + 隐藏目录(.*) + cf_worker* 目录
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith('.') and not d.startswith('cf_worker')]
        
        for file in files:
            if not file.endswith(".py"):
                continue
                
            if file in EXCLUDE_FILES:
                continue
                
            # 构建完整文件路径
            file_path = os.path.join(root, file)
            
            # 构建模块名称
            # 移除开头的 ./ (如果有)
            rel_path = os.path.relpath(file_path, ".")
            # 去掉 .py 后缀
            module_name = os.path.splitext(rel_path)[0]
            # 将路径分隔符转换为点号
            module_name = module_name.replace(os.sep, ".")

            # 跳过含非法标识符字符的模块名（如连字符目录 ui-ux-pro-max）
            if not all(part.isidentifier() for part in module_name.split(".")):
                print(f"Skipping invalid module name: {module_name}")
                continue
            
            print(f"Adding extension: {module_name} from {file_path}")
            
            ext = Extension(
                name=module_name,
                sources=[file_path],
                # 启用 Limited API (Stable ABI)
                # 0x030A0000 对应 Python 3.10
                define_macros=[("Py_LIMITED_API", "0x030A0000")],
                py_limited_api=True,
            )
            extensions.append(ext)

    setup(
        name="comfyui-banana-li",
        packages=find_packages(exclude=["web"]),
        ext_modules=cythonize(
            extensions,
            nthreads=min(os.cpu_count(), 4),
            compiler_directives={
                "language_level": "3",
                "always_allow_keywords": True,
                "emit_code_comments": False, # 不在 C 代码中保留 Python 源码注释 (安全)
            },
            annotate=False, # 发布时关闭注解生成
        ),
    )
