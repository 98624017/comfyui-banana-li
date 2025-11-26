@echo off
:: 设置编码为 UTF-8 以支持中文显示
chcp 65001 >nul
title 香蕉节点代码分支切换工具

:MENU
cls
echo =======================================================
echo               香蕉节点代码管理工具
echo =======================================================
echo.
echo    [1] 切换到 主分支 (main/master) 并强制更新
echo    [2] 切换到 开发分支 (dev) 并强制更新
echo    [3] 退出
echo.
echo    注意：此操作会丢弃所有本地修改！
echo.
echo =======================================================
set /p choice="请输入您的选择 (1-3): "

if "%choice%"=="1" goto SWITCH_MAIN
if "%choice%"=="2" goto SWITCH_DEV
if "%choice%"=="3" goto EXIT_SCRIPT

echo.
echo [错误] 输入无效，请重新输入...
timeout /t 2 >nul
goto MENU

:SWITCH_MAIN
echo.
echo -------------------------------------------------------
echo [警告] 此操作将丢弃所有本地修改，强制更新到最新版本！
echo.
echo [执行] 正在尝试切换到 main 分支...
git checkout main
if errorlevel 1 (
    echo [提示] main 分支切换失败，尝试切换 master 分支...
    git checkout master
)
if errorlevel 1 (
    echo.
    echo [失败] 无法切换到主分支。
    echo.
    pause
    goto MENU
)
echo.
echo [执行] 正在强制拉取最新代码...
git fetch --all
git reset --hard origin/main
if errorlevel 1 (
    :: 如果 main 不存在，尝试重置到 master
    git reset --hard origin/master
)
echo.
echo [完成] 主分支强制更新完毕。
pause
goto MENU

:SWITCH_DEV
echo.
echo -------------------------------------------------------
echo [警告] 此操作将丢弃所有本地修改，强制更新到最新版本！
echo.
echo [执行] 正在尝试切换到 dev 分支...
git checkout dev
if errorlevel 1 (
    echo.
    echo [失败] 无法切换到 dev 分支。
    echo.
    pause
    goto MENU
)
echo.
echo [执行] 正在强制拉取最新代码...
git fetch --all
git reset --hard origin/dev
echo.
echo [完成] 开发分支强制更新完毕。
pause
goto MENU

:EXIT_SCRIPT
exit
