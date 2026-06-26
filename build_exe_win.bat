@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Claude Code Feishu Sync  -  Build EXE
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.8+ first.
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/3] Installing PyInstaller and tray deps...
python -m pip install pyinstaller pystray pillow --quiet
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo [2/3] Building, please wait 1-3 minutes...
pyinstaller --onefile --windowed --noconfirm --clean --name "飞书同步助手" --hidden-import pystray._win32 feishu_sync_app.py
if errorlevel 1 (
    echo [ERROR] Build failed. See log above.
    pause
    exit /b 1
)

echo [3/3] Done.  Output: dist\飞书同步助手.exe
echo Double-click to run. No Python needed.
echo If Windows SmartScreen warns: More info -^> Run anyway.
echo.
pause
