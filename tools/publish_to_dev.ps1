# publish_to_dev.ps1
# Automates the Windows build and publish process locally.
# Usage: .\tools\publish_to_dev.ps1 -TargetDir "path\to\comfyui-banana-li"

param (
    [Parameter(Mandatory = $true)]
    [string]$TargetDir,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"

# --- Configuration ---
$SourceDir = Get-Location
$StagingDir = Join-Path $env:TEMP "banana_pro_build_stage"
$TimeStamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

Write-Host ">>> Starting Local Publish Process at $TimeStamp" -ForegroundColor Cyan
Write-Host "Source: $SourceDir"
Write-Host "Target: $TargetDir"
Write-Host "Stage : $StagingDir"
Write-Host "Python: $PythonExe"

# --- 1. Prepare Staging Area ---
Write-Host "`n>>> [1/5] Preparing Staging Area..." -ForegroundColor Cyan
if (Test-Path $StagingDir) {
    Remove-Item $StagingDir -Recurse -Force
}
New-Item -ItemType Directory -Path $StagingDir | Out-Null

# Robocopy to staging (Mirror, exclude git and heavy folders to speed up)
# /MIR :: Mirror a directory tree.
# /XD :: Exclude Directories matching given names/paths.
Write-Host "Copying source to staging..."
$RoboArgs = @($SourceDir, $StagingDir, "/MIR", "/XD", ".git", ".github", ".vscode", "__pycache__", "build", "dist", "web", "openspec", ".agent", ".serena", ".test", ".doc", ".venv")
& robocopy @RoboArgs
if ($LASTEXITCODE -ge 8) { throw "Robocopy failed with exit code $LASTEXITCODE" }

# --- 2. Build Extensions ---
Write-Host "`n>>> [2/5] Building Cython Extensions..." -ForegroundColor Cyan
Push-Location $StagingDir
try {
    # Ensure dependencies
    & $PythonExe -m pip install setuptools Cython --upgrade
    
    # Patch setup.py to disable parallel build (fixes multiprocessing issues on some Windows envs)
    $SetupContent = Get-Content "setup.py" -Raw
    $SetupContent = $SetupContent -replace "nthreads=min\(os.cpu_count\(\), 4\)", "nthreads=0"
    Set-Content "setup.py" -Value $SetupContent

    # Build
    & $PythonExe setup.py build_ext --inplace
    if ($LASTEXITCODE -ne 0) { throw "Build failed!" }
}
finally {
    Pop-Location
}

# --- 3. Clean Source Code ---
Write-Host "`n>>> [3/5] Cleaning Source Code in Staging..." -ForegroundColor Cyan
# Using the exact python logic from the workflow to ensure consistency
$CleanScript = @"
import os
import shutil

DIRS_TO_REMOVE = {'build', '.agent', '.github', '.git', '.vscode', '.idea', '.serena', '.test', '.doc', 'openspec', 'sam_hq', '__pycache__', 'dist'}
FILES_TO_KEEP = {'setup.py', '__init__.py', 'loader_bootstrap.py', 'blendmodes.py', 'imagefunc.py', 'requirements.txt', 'workflow_parallel.py', 'video_task_manager.py', 'clean_script.py'}

print('Starting cleanup...')
for root, dirs, files in os.walk('.', topdown=True):
    for d in list(dirs):
        if d in DIRS_TO_REMOVE:
            path = os.path.join(root, d)
            print(f'Removing directory: {path}')
            shutil.rmtree(path)
            dirs.remove(d)
    
    for file in files:
        file_path = os.path.join(root, file)
        if file.endswith('.md'):
            if file.lower() != 'readme.md':
                print(f'Deleting MD: {file}')
                os.remove(file_path)
            continue
        if file.endswith(('.py', '.c', '.cpp', '.h')):
            if file in FILES_TO_KEEP: continue
            print(f'Deleting source: {file}')
            os.remove(file_path)

# Verify binaries exist
pyd_files = []
for root, dirs, files in os.walk('.'):
    for f in files:
        if f.endswith('.pyd'):
            pyd_files.append(f)

if not pyd_files:
    print('ERROR: No .pyd binaries found after build!')
    exit(1)
else:
    print(f'Found {len(pyd_files)} binaries.')
"@

$CleanScriptPath = Join-Path $StagingDir "clean_script.py"
Set-Content -Path $CleanScriptPath -Value $CleanScript
Push-Location $StagingDir
try {
    & $PythonExe clean_script.py
    if ($LASTEXITCODE -ne 0) { throw "Cleanup script failed!" }
    Remove-Item clean_script.py -Force
    
    # Final cleanup (workflow steps)
    if (Test-Path "tools") { Remove-Item "tools" -Recurse -Force }
    if (Test-Path "setup.py") { Remove-Item "setup.py" -Force }
    # Remove .so files on windows if any leaked
    Get-ChildItem -Path . -Include "*.so" -Recurse | Remove-Item -Force
    # 移除含敏感数据的运行时/调试文件
    Remove-Item -Force -ErrorAction SilentlyContinue history.toml.lock
    Get-ChildItem -Path . -Filter "reproduce_*.py" | Remove-Item -Force
    Get-ChildItem -Path . -Filter "reproduce_*.pyd" | Remove-Item -Force
    Remove-Item -Force -ErrorAction SilentlyContinue poll_manual.py, poll_manual.pyd
}
finally {
    Pop-Location
}

# --- 4. Deploy to Target ---
Write-Host "`n>>> [4/5] Deploying to Target Directory..." -ForegroundColor Cyan
if (-not (Test-Path $TargetDir)) {
    throw "Target directory does not exist: $TargetDir"
}

# Clean Target (Keep .git)
Get-ChildItem -Path $TargetDir -Exclude ".git" | Remove-Item -Recurse -Force

# Copy from Staging to Target
Copy-Item -Path "$StagingDir\*" -Destination $TargetDir -Recurse -Force

# --- 5. Git Push ---
Write-Host "`n>>> [5/5] Pushing changes..." -ForegroundColor Cyan
Push-Location $TargetDir
try {
    # Check if we are on dev branch
    $currentBranch = git symbolic-ref --short HEAD
    Write-Host "Current branch: $currentBranch"
    
    if ($currentBranch -ne "dev") {
        Write-Warning "Target repo is not on 'dev' branch. Switching..."
        git checkout dev
        if ($LASTEXITCODE -ne 0) {
            git checkout -b dev
        }
    }

    git add .
    
    # Check for changes
    $status = git status --porcelain
    if (-not $status) {
        Write-Host "No changes to commit." -ForegroundColor Green
    }
    else {
        git commit -m "Local build release $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        git push origin dev
        Write-Host "Successfully pushed to dev!" -ForegroundColor Green
    }
}
finally {
    Pop-Location
}

Write-Host "`n>>> Done! Build published to $TargetDir" -ForegroundColor Green
