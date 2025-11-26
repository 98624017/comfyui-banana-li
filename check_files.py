import os

EXCLUDE_DIRS = {'web', '.git', '.github', '__pycache__', 'build', 'dist', '.vscode', '.idea', 'tools', '.serena', '.test', '.doc', 'openspec'}
EXCLUDE_FILES = {'setup.py', '__init__.py', 'check_files.py'}

print("Starting file walk...")
count = 0
for root, dirs, files in os.walk("."):
    # Filter directories in-place
    dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
    
    for file in files:
        if not file.endswith(".py"):
            continue
            
        if file in EXCLUDE_FILES:
            continue
            
        file_path = os.path.join(root, file)
        rel_path = os.path.relpath(file_path, ".")
        module_name = os.path.splitext(rel_path)[0].replace(os.sep, ".")
        
        print(f"Found: {module_name}")
        count += 1
print(f"Walk complete. Found {count} files.")
