@echo off
rem Desliga o whisper-flow-local e libera a VRAM.
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0toggle.ps1" stop
