:: FILE: boot/start_operator.bat

@echo off
setlocal
cd /d "%~dp0.."
start "" http://127.0.0.1:4001/
node boot\operator_server.js
pause