@echo off
chcp 65001 >nul
title Sistema de Presença - Programa Canela

cd /d "%~dp0"

echo.
echo   Iniciando o sistema de presença...
echo   O navegador abrirá automaticamente.
echo   NÃO feche esta janela enquanto estiver usando.
echo.

call "%~dp0atualizar.bat"
echo.

if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe web.py
) else (
    python web.py
)

pause
