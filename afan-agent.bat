@echo off
set "CLI=%~dp0scripts\afan_agent_cli.py"
if exist "%USERPROFILE%\miniconda3\python.exe" (
  "%USERPROFILE%\miniconda3\python.exe" "%CLI%" %*
  exit /b %ERRORLEVEL%
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%CLI%" %*
  exit /b %ERRORLEVEL%
)
python "%CLI%" %*
