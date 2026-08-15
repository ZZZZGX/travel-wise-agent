@echo off
REM ============================================================
REM  TravelWise smoke test launcher (ASCII only on purpose:
REM  cmd parses .bat byte-by-byte, so non-ASCII comments here
REM  can break line parsing).
REM
REM  Usage from cmd:
REM     smoke.bat            stages 0-2, no paid API calls
REM     smoke.bat 5          full chain, asks before each paid stage
REM     smoke.bat 5 yes      full chain, no prompts
REM
REM  Double-clicking also works; the window stays open at the end.
REM  Set a specific interpreter first if "python" is not on PATH:
REM     set PY=E:\path\to\python.exe
REM ============================================================
chcp 65001 >nul
setlocal

REM Always run from the folder this .bat lives in, so relative
REM paths work no matter where it was launched from.
cd /d "%~dp0"

if "%PY%"=="" set PY=python

set STAGE=%1
if "%STAGE%"=="" set STAGE=2

set AUTOYES=
if /I "%2"=="yes" set AUTOYES=--yes
if /I "%2"=="y"   set AUTOYES=--yes

"%PY%" --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [ERROR] Python not found: %PY%
  echo   Set the full path first, for example:
  echo     set PY=E:\1comfyui\ComfyUI-aki-v3\ComfyUI-aki-v3\python\python.exe
  echo   Then run this again.
  goto :done
)

echo.
echo   Python : %PY%
echo   Folder : %CD%
echo   Stage  : %STAGE%
echo.

"%PY%" scripts\smoke_full_flow.py --stage %STAGE% %AUTOYES%

echo.
if errorlevel 1 (
  echo   Some checks FAILED -- see the [FAIL] lines above.
) else (
  echo   All checks passed.
)

:done
REM Keep the window open when launched by double-click.
echo %cmdcmdline% | find /i "%~0" >nul
if not errorlevel 1 pause
endlocal
