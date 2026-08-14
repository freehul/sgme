@echo off
rem ===== SGME Windows Service Installer (run as admin) =====
rem 路径自适应：项目根 = 脚本上一级目录（scripts\..），不依赖写死位置
set SVC=SGME
set ROOT=%~dp0..
set PYEXE=%ROOT%\.venv\Scripts\python.exe

rem ---- locate nssm: PATH 优先，其次 WinGet 包目录 ----
set NSSM=
where nssm >nul 2>&1 && set NSSM=nssm
if not defined NSSM (
    for /f "delims=" %%i in ('where /r "%LOCALAPPDATA%\Microsoft\WinGet\Packages" nssm.exe 2^>nul') do (
        set NSSM=%%i
        goto :nssm_found
    )
)
:nssm_found
if not defined NSSM (
    echo [FAIL] 未找到 nssm.exe。请先安装：winget install NSSM.NSSM
    exit /b 1
)
echo [OK] nssm: %NSSM%
if not exist "%PYEXE%" (
    echo [FAIL] 未找到 Python 解释器: %PYEXE%
    echo        请先在 %ROOT% 创建 venv 并安装依赖。
    exit /b 1
)

rem ---- remove old service if exists ----
"%NSSM%" stop %SVC% >nul 2>&1
"%NSSM%" remove %SVC% confirm >nul 2>&1

rem ---- install service ----
"%NSSM%" install %SVC% "%PYEXE%" -m sgme
if errorlevel 1 goto :fail

rem ---- configure ----
"%NSSM%" set %SVC% AppDirectory "%ROOT%"
"%NSSM%" set %SVC% AppStdout "%ROOT%\tmp\sgme-service.log"
"%NSSM%" set %SVC% AppStderr "%ROOT%\tmp\sgme-service.err.log"
"%NSSM%" set %SVC% AppRotateFiles 1
"%NSSM%" set %SVC% AppRotateBytes 10485760
"%NSSM%" set %SVC% DisplayName "SGME Memory Engine"
"%NSSM%" set %SVC% Description "SGME daemon (9910) - Hermes memory provider"
"%NSSM%" set %SVC% Start SERVICE_AUTO_START
"%NSSM%" set %SVC% AppExit Default Restart
"%NSSM%" set %SVC% AppRestartDelay 5000

rem ---- inject secrets (安全加固 2026-08-11：key 不进源码/配置文件，仅环境变量注入) ----
rem 注：SGME 启动时 load_env_file() 会 setdefault 加载 config/.env——服务环境
rem 变量优先于 .env（2026-08-07 事故教训：AppEnvironmentExtra 覆盖式 set 曾冲掉
rem DEEPSEEK_API_KEY；此处仅注入 SGME 自身 key，其余密钥一律走 config/.env）
if defined SGME_ADMIN_KEY (
    "%NSSM%" set %SVC% AppEnvironmentExtra SGME_ADMIN_KEY=%SGME_ADMIN_KEY% SGME_AGENT_KEY=%SGME_AGENT_KEY%
) else (
    echo [WARN] 未设置 SGME_ADMIN_KEY/SGME_AGENT_KEY 环境变量，服务将使用默认 dev key（仅限本机开发）
)

rem ---- start ----
sc failure %SVC% reset= 86400 actions= restart/5000/restart/10000/restart/30000
"%NSSM%" start %SVC%
if errorlevel 1 goto :fail

echo [OK] SGME 服务已安装并启动。
exit /b 0

:fail
echo [FAIL] 安装失败，请检查上方错误信息。
exit /b 1
