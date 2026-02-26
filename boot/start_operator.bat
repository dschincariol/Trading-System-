:: FILE: boot/start_operator.bat

@echo off
setlocal
cd /d "%~dp0.."

REM Ensure node deps are present (express)
if not exist "node_modules\express\package.json" (
  echo [operator] node_modules missing; running npm install...
  npm install
  if errorlevel 1 (
    echo [operator] ERROR: npm install failed
    pause
    exit /b 1
  )
)

start "" http://127.0.0.1:4001/
node boot\operator_server.js
pause