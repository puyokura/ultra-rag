@echo off
setlocal
set "HERE=%~dp0"
if not defined ULTRA_RAG_HOME set "ULTRA_RAG_HOME=%LOCALAPPDATA%\ultra-rag"
call "%HERE%ultra-rag-sync.cmd" >nul 2>&1
dsh-tui %*
set "CODE=%ERRORLEVEL%"
call "%HERE%ultra-rag-sync.cmd" >nul 2>&1
exit /b %CODE%
