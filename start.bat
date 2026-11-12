@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not defined AGENT_WEB_PORT set "AGENT_WEB_PORT=8000"
set "LAUNCH_URL=http://127.0.0.1:%AGENT_WEB_PORT%/launch"
set "APP_URL=http://127.0.0.1:%AGENT_WEB_PORT%/"

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo ============================================================
echo   LangGraph 研究助手 . 一键启动
echo   启动页：%LAUNCH_URL%
echo   主界面：%APP_URL%
echo   关闭本窗口即停止服务（Ctrl+C 也可以停止）
echo ============================================================

rem 等后端起来再自动打开浏览器（4 秒后打开启动页）
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process '%LAUNCH_URL%'"

"%PY%" web.py

if errorlevel 1 (
  echo.
  echo [研究助手] 启动失败。常见原因：
  echo   - 依赖未安装：pip install -r requirements.txt
  echo   - 端口被占用：set AGENT_WEB_PORT=8001 后重新运行
  pause
)
endlocal
