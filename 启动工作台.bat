@echo off
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo [ERROR] Virtual environment not found: .venv\Scripts\pythonw.exe
    pause
    exit /b 1
)

if /i "%~1"=="--browser" goto :console_mode
if /i "%~1"=="--debug" goto :console_mode

start "" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0src\desktop_app.py" %*
exit /b 0

:console_mode
"%~dp0.venv\Scripts\python.exe" "%~dp0src\desktop_app.py" %*
pause
