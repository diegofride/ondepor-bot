@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Probando login y ubicacion del turno (NO reserva nada)...
echo.
python reservar_bot.py probar all
echo.
pause
