@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0backend"

if not exist "app.py" (
    echo [ERROR] app.py not found!
    pause
    exit /b 1
)

echo ========================================
echo   ZhiDu XiaoZhan - Starting
echo ========================================
echo.

:: ---------------------------------------------------------------- 1. Python
echo [1/4] Checking Python...
set "PYTHON_CMD="

py --version >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py"

if "!PYTHON_CMD!"=="" (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if "!PYTHON_CMD!"=="" (
    echo [ERROR] Python not found!
    echo Please install Python from https://www.python.org/
    pause
    exit /b 1
)
echo [OK] Python found - using !PYTHON_CMD!
echo.

:: ---------------------------------------------------------------- 2. Deps
echo [2/4] Checking dependencies...
!PYTHON_CMD! -c "import flask, fitz, bcrypt, sentence_transformers, faiss, openai, dotenv" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing dependencies, this may take a few minutes...
    !PYTHON_CMD! -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies!
        pause
        exit /b 1
    )
    echo [OK] Dependencies installed
) else (
    echo [OK] Dependencies ready
)
echo.

:: ---------------------------------------------------------------- 3. Env + GPU
echo [3/4] Checking configuration...
if not exist ".env" (
    echo [WARN] .env not found - AI features will be unavailable.
    echo        Copy .env.example to .env and fill in your API key.
) else (
    echo [OK] .env found
)

set "DEVICE=CPU"
for /f "delims=" %%i in ('!PYTHON_CMD! -c "import torch;print('GPU' if torch.cuda.is_available() else 'CPU')" 2^>nul') do set "DEVICE=%%i"
if "!DEVICE!"=="GPU" (
    echo [OK] NVIDIA GPU detected - indexing will be fast
) else (
    echo [WARN] No CUDA GPU detected - building the search index on CPU is slow
    echo        ^(about 12-35 min for a 100-chunk paper^). See README for the CUDA torch install.
)
echo.

:: ---------------------------------------------------------------- 4. Run
echo [4/4] Starting backend...
echo ========================================
echo   URL: http://127.0.0.1:5000
echo   The browser will open automatically.
echo   Keep this window open. Press Ctrl+C to stop.
echo ========================================
echo.

set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if exist "%EDGE%" (
    echo   Browser: Microsoft Edge
    start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 6; Start-Process '%EDGE%' 'http://127.0.0.1:5000'"
) else (
    echo   Browser: system default
    start "" /b cmd /c "timeout /t 6 >nul & start http://127.0.0.1:5000"
)

set PYTHONIOENCODING=utf-8
!PYTHON_CMD! app.py

echo.
echo ========================================
echo   Service stopped
echo ========================================
pause
