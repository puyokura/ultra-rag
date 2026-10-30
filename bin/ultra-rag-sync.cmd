@echo off
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%;%ROOT%\secret-core;%ROOT%\key-use;%ROOT%\password-input;%ROOT%\cli;%PYTHONPATH%"
if not defined ULTRA_RAG_HOME set "ULTRA_RAG_HOME=%LOCALAPPDATA%\ultra-rag"
python -m ultra_rag_cli.main --home "%ULTRA_RAG_HOME%" rag sync once %*
exit /b %ERRORLEVEL%
