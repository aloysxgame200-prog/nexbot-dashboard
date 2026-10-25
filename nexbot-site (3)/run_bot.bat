@echo off
REM Lance Nexbot et le relance automatiquement apres *restart (code de sortie 3).
REM Double-cliquez sur ce fichier, ou lancez-le depuis un terminal.
cd /d "%~dp0"
chcp 65001 >nul
:boucle
python -m nexbot.bot
if %errorlevel%==3 (
    echo Redemarrage de Nexbot...
    timeout /t 2 /nobreak >nul
    goto boucle
)
echo Nexbot est arrete (code %errorlevel%).
pause
