@echo off
REM ---------------------------------------------------------------
REM  NOTE: this file is intentionally ASCII-only.
REM  cmd.exe parses .bat files using the OEM codepage (GBK on a
REM  Chinese Windows), so UTF-8 Chinese text gets mangled and can
REM  corrupt the ^ line-continuation syntax. Keep it ASCII.
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

echo [1/3] Generating assets\app.ico
python tools\make_app_ico.py
if errorlevel 1 goto :error

echo.
echo [2/3] Packaging with PyInstaller (onefile + noconsole)
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name deepseek-tray ^
  --paths . ^
  --icon assets\app.ico ^
  --hidden-import pystray._win32 ^
  --exclude-module pystray._xorg ^
  --exclude-module pystray._gtk ^
  --exclude-module pystray._appindicator ^
  --exclude-module pystray._darwin ^
  --exclude-module yaml ^
  --exclude-module numpy ^
  --exclude-module pandas ^
  --exclude-module matplotlib ^
  --exclude-module scipy ^
  --exclude-module cv2 ^
  --exclude-module onnxruntime ^
  --exclude-module rapidocr ^
  --exclude-module playwright ^
  --exclude-module mss ^
  --exclude-module pyzbar ^
  --exclude-module shapely ^
  --exclude-module lxml ^
  --exclude-module PyPDF2 ^
  --exclude-module docx ^
  --exclude-module PyQt5 ^
  --exclude-module PyQt6 ^
  --exclude-module PySide2 ^
  --exclude-module PySide6 ^
  --exclude-module setuptools ^
  --exclude-module pip ^
  --noupx ^
  run.py
if errorlevel 1 goto :error

echo.
echo [3/3] Done
for %%F in (dist\deepseek-tray.exe) do echo   %%F  (%%~zF bytes)
echo.
echo Just double-click it. First launch unpacks to %%TEMP%% and may take 1-3 s.
goto :eof

:error
echo.
echo BUILD FAILED
exit /b 1
