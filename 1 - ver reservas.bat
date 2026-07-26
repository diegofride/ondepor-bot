@echo off
chcp 65001 >nul
cd /d "%~dp0"
python reservar_bot.py listar
echo.
pause
