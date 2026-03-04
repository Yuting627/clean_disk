@echo off
chcp 65001 >nul
title C 盘清理 - 服务
cd /d "%~dp0"

echo 正在启动 C 盘清理服务...
start "C盘清理-uvicorn" cmd /k "uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000"
echo 等待服务就绪...
timeout /t 3 /nobreak >nul
echo 正在打开浏览器...
start "" "http://127.0.0.1:8000/"
echo.
echo 已在浏览器打开 http://127.0.0.1:8000/
echo 关闭上方「C盘清理-uvicorn」窗口即可停止服务。
pause
