@echo off
rem Atualizacao automatica do sistema a partir do GitHub.
rem Chamado pelos outros .bat antes de iniciar o servidor. Nunca trava nem
rem apaga nada: se a maquina nao tiver git, nao tiver internet, ou tiver
rem alguma mudanca local que colida com a atualizacao, so avisa e segue
rem com a versao ja instalada (git pull --ff-only nunca sobrescreve
rem mudanca local por conta propria).

if not exist "%~dp0.git" exit /b 0

where git >nul 2>nul
if errorlevel 1 (
    echo   Atualizacao automatica desativada: git nao encontrado nesta maquina.
    exit /b 0
)

set "GIT_TERMINAL_PROMPT=0"
pushd "%~dp0"

for /f "delims=" %%i in ('git rev-parse HEAD 2^>nul') do set "NRUOMR_ANTES=%%i"

echo   Verificando atualizacoes...
git fetch --quiet origin
if errorlevel 1 (
    echo   Sem conexao com o repositorio agora - continuando com a versao atual.
    popd
    exit /b 0
)

git pull --ff-only --quiet
if errorlevel 1 (
    echo   Nao foi possivel atualizar automaticamente ^(ha mudancas locais nesta maquina^).
    echo   Continuando com a versao ja instalada.
    popd
    exit /b 0
)

for /f "delims=" %%i in ('git rev-parse HEAD 2^>nul') do set "NRUOMR_DEPOIS=%%i"

if "%NRUOMR_ANTES%"=="%NRUOMR_DEPOIS%" (
    echo   Sistema ja estava atualizado.
) else (
    echo   Sistema atualizado para a versao mais recente.
    git diff --quiet %NRUOMR_ANTES% %NRUOMR_DEPOIS% -- requirements.txt
    if errorlevel 1 (
        echo   Dependencias mudaram, instalando...
        if exist "venv\Scripts\python.exe" (
            venv\Scripts\python.exe -m pip install -q -r requirements.txt
        ) else (
            python -m pip install -q -r requirements.txt
        )
    )
)

popd
exit /b 0
