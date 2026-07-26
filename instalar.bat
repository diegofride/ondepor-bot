@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Instalando lo que necesita el bot (requests y ntplib)...
python -m pip install --upgrade requests ntplib
echo.
echo Listo. Ya podes usar los demas accesos (empeza por "1 - ver reservas.bat").
pause
