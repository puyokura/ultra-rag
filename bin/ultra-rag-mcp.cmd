@echo off
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%;%ROOT%\secret-core;%ROOT%\key-use;%ROOT%\password-input;%ROOT%\cli;%PYTHONPATH%"
python "%ROOT%\scripts\run_mcp.py" %*
exit /b %ERRORLEVEL%
