@echo off
setlocal
chcp 65001 >nul

echo ==========================================
echo Stable Diffusion WebUI Forge - API Starter
echo ==========================================

echo [Forge] Preparing environment...
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if not defined PYTHON set "PYTHON=python"
if not defined FORGE_VENV_DIR set "FORGE_VENV_DIR=%SCRIPT_DIR%forge-venv"

if not exist "%FORGE_VENV_DIR%\Scripts\python.exe" (
    echo [Forge] Creating virtual environment at "%FORGE_VENV_DIR%" ...
    %PYTHON% -m venv "%FORGE_VENV_DIR%"
    if %ERRORLEVEL% neq 0 (
        echo [Forge][ERROR] Failed to create virtual environment.
        exit /b 1
    )
)

call "%FORGE_VENV_DIR%\Scripts\activate.bat"
if %ERRORLEVEL% neq 0 (
    echo [Forge][ERROR] Failed to activate virtual environment.
    exit /b 1
)

set "PYTHON=%FORGE_VENV_DIR%\Scripts\python.exe"

if not "%FORGE_SKIP_PIP%"=="1" (
    echo [Forge] Ensuring pip is up to date...
    %PYTHON% -m pip install --upgrade pip
    if %ERRORLEVEL% neq 0 (
        echo [Forge][ERROR] Failed to upgrade pip.
        exit /b 1
    )

    echo [Forge] Installing required python packages (set FORGE_SKIP_PIP=1 to skip)...
    %PYTHON% -m pip install -r requirements_versions.txt --prefer-binary
    if %ERRORLEVEL% neq 0 (
        echo [Forge][ERROR] Failed to install required packages.
        exit /b 1
    )
)

if defined FORGE_API_PORT (
    set "PORT_ARG=--port %FORGE_API_PORT%"
) else (
    set "PORT_ARG=--port 7861"
)

if defined FORGE_COMMANDLINE_ARGS (
    set "COMMANDLINE_ARGS=%FORGE_COMMANDLINE_ARGS%"
) else (
    set "COMMANDLINE_ARGS=--nowebui --api --api-server-stop --listen %PORT_ARG% --disable-all-extensions --skip-version-check --no-gradio-queue --no-hashing --disable-console-progressbars"
)

echo [Forge] Launching Stable Diffusion WebUI Forge (API only)...
"%PYTHON%" launch.py %COMMANDLINE_ARGS%

endlocal
