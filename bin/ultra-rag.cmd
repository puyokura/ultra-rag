@echo off
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%;%ROOT%\secret-core;%ROOT%\key-use;%ROOT%\password-input;%ROOT%\cli;%PYTHONPATH%"
python -m ultra_rag_cli.main %*
exit /b %ERRORLEVEL%
