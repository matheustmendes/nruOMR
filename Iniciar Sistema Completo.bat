@echo off
chcp 65001 >nul
title Sistema de Presença - Iniciando

cd /d "%~dp0"

echo.
echo   ===========================================
echo     SISTEMA DE PRESENCA - Iniciando tudo...
echo   ===========================================
echo.
echo   Aguarde um instante. Duas abas vao abrir
echo   automaticamente no seu navegador.
echo.

call "%~dp0atualizar.bat"
echo.

if exist "venv\Scripts\python.exe" (
    set "PYTHON=venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

start "Sistema Web" /min "%PYTHON%" web.py
start "Dashboard" /min "%PYTHON%" dashboard.py

echo.
echo   Sistema iniciado com sucesso!
echo.
echo     - Pagina principal: http://localhost:5000
echo     - Dashboard:        http://localhost:5001
echo.
echo   Duas janelas pretas foram abertas minimizadas
echo   (veja na barra de tarefas). Elas precisam
echo   continuar abertas enquanto voce usa o sistema.
echo.
echo   Para ENCERRAR o sistema, feche essas duas
echo   janelas pretas.
echo.
echo   Esta janela aqui pode ser fechada a qualquer
echo   momento, ela ja fez seu trabalho.
echo.
pause
