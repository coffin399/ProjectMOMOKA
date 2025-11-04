@echo off
chcp 65001 >nul
title MOMOKA STARTER

set VENV_DIR=.venv

echo ================================
echo MOMOKA STARTER
echo ================================
echo.

REM 仮想環境の存在チェック
if not exist "%VENV_DIR%" (
    echo [INFO] Creating virtual environment in '%VENV_DIR%' folder...
    python -m venv %VENV_DIR%
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