@echo off
rem Liga o whisper-flow-local (servidor + ditado) e abre o painel.
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0toggle.ps1" start
start "" http://127.0.0.1:9978
