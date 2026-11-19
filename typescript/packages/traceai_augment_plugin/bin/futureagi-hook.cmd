@echo off
rem Packaged for future Windows qualification; setup currently rejects Windows.
setlocal DisableDelayedExpansion
set /p FI_NODE=<"%~dp0node-path"
"%FI_NODE%" "%~dp0..\runtime\hook.cjs" "%~dp0registration.json" >nul 2>&1
exit /b 0
