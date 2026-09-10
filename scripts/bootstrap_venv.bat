@echo off
setlocal
rem ============================================================
rem SGME 项目级 Python 环境重建脚本
rem 铁律：项目依赖必须项目级，基准解释器只允许取自 AI 资源仓库
rem       仓库位置 D:\AI\python ；禁止使用 PATH 上的 python 建 venv
rem 用法：双击本脚本，或在项目根执行 scripts\bootstrap_venv.bat
rem ============================================================
cd /d "%~dp0.."

set "BASE_DIR=D:\AI\python"
set "BASE="
for /d %%D in ("%BASE_DIR%\cpython-3.12*-windows-x86_64-none") do (
  if exist "%%~fD\python.exe" set "BASE=%%~fD\python.exe"
)

if not defined BASE (
  echo [错误] 未在 %BASE_DIR% 找到 3.12 便携解释器。
  echo        按仓库规则，缺少的版本要下载到仓库，而不是用系统 Python：
  echo            uv python install 3.12 --install-dir "%BASE_DIR%"
  echo        装好后重跑本脚本。
  exit /b 1
)

echo [1/4] 基准解释器：%BASE%
"%BASE%" -V
if errorlevel 1 exit /b 1

if exist ".venv" (
  echo [错误] .venv 已存在，本脚本不覆盖。
  echo        重建请先把它改名归档（原件不删），例如：
  echo            ren .venv .venv.bak-日期
  exit /b 1
)

echo [2/4] 建 venv ...
"%BASE%" -m venv .venv
if errorlevel 1 exit /b 1

echo [3/4] 装依赖（requirements.txt 已钉版本）...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

echo [4/4] 可编辑安装项目自身 ...
".venv\Scripts\python.exe" -m pip install --no-deps -e .
if errorlevel 1 exit /b 1

echo.
echo 验收：pyvenv.cfg 的 home 必须指向 %BASE_DIR%
findstr /c:"home" /c:"version" .venv\pyvenv.cfg
echo 完成。
endlocal
