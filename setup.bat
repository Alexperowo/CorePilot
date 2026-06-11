@echo off
chcp 65001 >nul
title CorePilot - Установка (стационарная версия)
setlocal

REM ============================================================================
REM  setup.bat — первоначальная установка CorePilot для стационарной версии.
REM  Создаёт виртуальную среду venv и ставит зависимости из requirements.txt.
REM  Запускается ОДИН раз. Дальше используйте start.bat.
REM
REM  Требуется заранее установленный Python 3.12 (с галочкой "Add to PATH").
REM ============================================================================

cd /d "%~dp0"

echo ================================================================
echo   CorePilot — установка стационарной версии
echo ================================================================
echo.

REM ---- Выбор бэкенда llama.cpp (как в портативной версии) ---------------------
REM llama.cpp server ставится ПО УМОЛЧАНИЮ, чтобы локальные модели работали
REM "из коробки" без сторонних программ. LM Studio/Ollama — опционально, для
REM продвинутых; их можно выбрать в настройках приложения.
set "LLAMA_DIR=%~dp0llama"
set "TMP_DIR=%~dp0_tmp_setup"
set "LLAMA_API=https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
echo  Выберите бэкенд для llama.cpp server (движок локальных моделей):
echo    [1] Vulkan       - универсальный GPU (AMD Radeon, Intel Arc, NVIDIA). РЕКОМЕНДУЕТСЯ.
echo    [2] CUDA 12      - NVIDIA с CUDA 12 (новые драйверы).
echo    [3] CUDA 13      - NVIDIA с CUDA 13 (новейшие драйверы).
echo    [4] CPU          - без GPU (медленно, но работает везде).
echo    [0] Пропустить   - не ставить (если используете LM Studio/Ollama).
echo.
set /p BACKEND_CHOICE="  Ваш выбор [1/2/3/4/0] (по умолчанию 1): "
if "%BACKEND_CHOICE%"=="" set "BACKEND_CHOICE=1"
set "LLAMA_MATCH="
if "%BACKEND_CHOICE%"=="1" set "LLAMA_MATCH=win-vulkan-x64"
if "%BACKEND_CHOICE%"=="2" set "LLAMA_MATCH=win-cuda-12"
if "%BACKEND_CHOICE%"=="3" set "LLAMA_MATCH=win-cuda-13"
if "%BACKEND_CHOICE%"=="4" set "LLAMA_MATCH=win-cpu-x64"
echo.

REM --- Проверка наличия Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден в PATH.
    echo Установите Python 3.12 с https://www.python.org/downloads/
    echo и обязательно отметьте галочку "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo Найден Python %PYVER%
echo.

REM --- Создание виртуальной среды (если ещё нет) ---
if exist "venv\Scripts\python.exe" (
    echo Виртуальная среда venv уже существует — пропускаю создание.
) else (
    echo [1/3] Создание виртуальной среды venv...
    python -m venv venv
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось создать venv.
        pause
        exit /b 1
    )
)

REM --- Установка зависимостей ---
echo [2/3] Установка зависимостей из requirements.txt...
call "venv\Scripts\activate.bat"
python -m pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Не удалось установить зависимости.
    echo Проверьте интернет-соединение и содержимое requirements.txt.
    pause
    exit /b 1
)

REM --- Загрузка llama.cpp server (последний релиз, выбранный бэкенд) ---
echo [3/3] Установка llama.cpp server...
if "%LLAMA_MATCH%"=="" (
    echo       Пропущено по вашему выбору. Локальные модели — через LM Studio/Ollama.
) else if exist "%LLAMA_DIR%\llama-server.exe" (
    echo       llama-server уже присутствует в .\llama\ — пропуск.
) else (
    if not exist "%LLAMA_DIR%" mkdir "%LLAMA_DIR%"
    if not exist "%TMP_DIR%" mkdir "%TMP_DIR%"
    echo       Поиск релиза llama.cpp (%LLAMA_MATCH%)...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_download_llama_win.ps1" -Match "%LLAMA_MATCH%" -OutFile "%TMP_DIR%\llama.zip"
    if errorlevel 1 (
        echo  [ВНИМАНИЕ] Не удалось скачать llama.cpp для '%LLAMA_MATCH%'.
        echo            Приложение запустится, но локальные модели через llama.cpp
        echo            будут недоступны. Можно скачать вручную и распаковать в:
        echo            %LLAMA_DIR%
        echo            https://github.com/ggml-org/llama.cpp/releases/latest
    ) else (
        powershell -NoProfile -Command "Expand-Archive -Path '%TMP_DIR%\llama.zip' -DestinationPath '%LLAMA_DIR%' -Force"
        if not exist "%LLAMA_DIR%\llama-server.exe" (
            for /f "delims=" %%S in ('dir /b /s "%LLAMA_DIR%\llama-server.exe" 2^>nul') do (
                robocopy "%%~dpS." "%LLAMA_DIR%" /E /MOVE /NJH /NJS /NDL /NP >nul
            )
        )
        echo       llama-server установлен в .\llama\ (бэкенд: %LLAMA_MATCH%).
    )
    if exist "%TMP_DIR%" rmdir /s /q "%TMP_DIR%"
)

echo.
echo ================================================================
echo   ГОТОВО. Зависимости установлены.
echo ----------------------------------------------------------------
echo   Запуск приложения:  start.bat
echo   (модели и Демон настраиваются прямо в окне приложения)
echo ================================================================
echo.
pause
exit /b 0

