@echo off
chcp 65001 >nul
title MOMOKA STARTER

set VENV_DIR=.venv
set "PYTHON_CMD=py -3.10"

echo ================================
echo MOMOKA STARTER
echo ================================
echo.

REM Python 3.10 availability check
echo [INFO] Checking for Python 3.10 interpreter...
%PYTHON_CMD% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10 could not be found. Please install it and ensure 'py -3.10' works.
    pause
    exit /b 1
)
for /f "tokens=2 delims= " %%A in ('%PYTHON_CMD% --version 2^>nul') do set "PY310_VERSION=%%A"
if not defined PY310_VERSION set "PY310_VERSION=Unknown"
echo [INFO] Detected Python %PY310_VERSION%

REM 仮想環境の存在チェック
if not exist "%VENV_DIR%" (
    echo [INFO] Creating virtual environment in '%VENV_DIR%' folder...
    %PYTHON_CMD% -m venv %VENV_DIR%

    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        echo [ERROR] Please check if Python is installed correctly.
        pause
        exit /b 1
    )
    echo [SUCCESS] Virtual environment created successfully.
    echo.
) else (
    echo [INFO] Virtual environment already exists.
    echo.
)

REM 仮想環境のアクティベート
echo [INFO] Activating virtual environment...
call "%VENV_DIR%\Scripts\activate.bat"
if %errorlevel% neq 0 (
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b 1
)
echo [SUCCESS] Virtual environment activated.
echo.

REM Verify active Python version is 3.10.x
set "ACTIVE_PY_VERSION="
for /f "tokens=2 delims= " %%A in ('python --version 2^>nul') do set "ACTIVE_PY_VERSION=%%A"
if not defined ACTIVE_PY_VERSION (
    echo [ERROR] Unable to determine Python version inside virtual environment.
    pause
    exit /b 1
)
echo [INFO] Virtual environment Python version: %ACTIVE_PY_VERSION%
echo %ACTIVE_PY_VERSION% | find "3.10." >nul
if errorlevel 1 (
    echo [ERROR] Virtual environment is not using Python 3.10.x. Please recreate the venv.
    pause
    exit /b 1
)

REM パッケージのインストール/更新
echo [INFO] Installing/Updating required packages...
python -m pip install --upgrade pip
python -m pip install -U -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install packages.
    echo [ERROR] Please check requirements.txt file.
    pause
    exit /b 1
)
echo [SUCCESS] All packages installed successfully.
echo.

REM MOMOKAの起動
echo ================================
echo Starting MOMOKA...
echo ================================
echo.
python main.py

REM 終了時の処理
echo.
echo ================================
echo MOMOKA has stopped.
echo ================================
pause