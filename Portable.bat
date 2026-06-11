@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title CorePilot - Portable Builder

REM ============================================================================
REM  CorePilot (AI Factory) - Portable Builder
REM ----------------------------------------------------------------------------
REM  Собирает автономную портативную версию в подпапку .\CorePilot_Portable\:
REM    - embeddable Python (без установки в систему)
REM    - все зависимости из requirements.txt (в локальную папку)
REM    - llama.cpp server (универсальный: вы выбираете Vulkan или CUDA)
REM    - launcher-скрипты для запуска приложения и сервера моделей
REM
REM  АНТИ-ХАРДКОД: номер релиза llama.cpp и версия Python не зашиты.
REM  Скрипт определяет последний релиз через GitHub API и подбирает нужный
REM  ассет по бэкенду. Версию Python можно переопределить переменной PY_VERSION.
REM
REM  Требования: Windows 10/11 x64, PowerShell (есть в системе), интернет.
REM ============================================================================

REM ---- Настройки (можно переопределить через переменные окружения) -----------
if "%PY_VERSION%"=="" set "PY_VERSION=3.12.10"
set "PORTABLE_DIR=%~dp0CorePilot_Portable"
set "APP_DIR=%PORTABLE_DIR%\app"
set "PY_DIR=%PORTABLE_DIR%\python"
set "LLAMA_DIR=%PORTABLE_DIR%\llama"
set "MODELS_DIR=%PORTABLE_DIR%\models"
set "TMP_DIR=%PORTABLE_DIR%\_tmp"
set "GET_PIP_URL=https://bootstrap.pypa.io/get-pip.py"
set "PY_EMBED_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-embed-amd64.zip"
set "LLAMA_API=https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"

echo.
echo ================================================================
echo   CorePilot - Portable Builder
echo   Python %PY_VERSION% ^| llama.cpp server (Vulkan/CUDA)
echo ================================================================
echo.

REM ---- Выбор бэкенда llama.cpp -----------------------------------------------
REM llama.cpp server ставится ПО УМОЛЧАНИЮ — локальные модели работают сразу,
REM без сторонних программ. LM Studio/Ollama — опционально (выбор в настройках UI).
echo  Выберите бэкенд для llama.cpp server (движок локальных моделей):
echo    [1] Vulkan       - универсальный GPU (AMD Radeon, Intel Arc, NVIDIA). РЕКОМЕНДУЕТСЯ.
echo    [2] CUDA 12      - NVIDIA с CUDA 12 (новые драйверы).
echo    [3] CUDA 13      - NVIDIA с CUDA 13 (новейшие драйверы).
echo    [4] CPU          - без GPU (медленно, но работает везде).
echo    [0] Пропустить   - не ставить (если используете LM Studio/Ollama).
echo.
set /p BACKEND_CHOICE="  Ваш выбор [1/2/3/4/0] (по умолчанию 1): "
if "%BACKEND_CHOICE%"=="" set "BACKEND_CHOICE=1"

if "%BACKEND_CHOICE%"=="0" (
    set "LLAMA_MATCH=SKIP"
    echo  llama.cpp пропущен — будете использовать LM Studio/Ollama.
    echo.
    goto :after_backend
)
if "%BACKEND_CHOICE%"=="1" set "LLAMA_MATCH=win-vulkan-x64"
if "%BACKEND_CHOICE%"=="2" set "LLAMA_MATCH=win-cuda-12"
if "%BACKEND_CHOICE%"=="3" set "LLAMA_MATCH=win-cuda-13"
if "%BACKEND_CHOICE%"=="4" set "LLAMA_MATCH=win-cpu-x64"
if "%LLAMA_MATCH%"=="" (
    echo  [ОШИБКА] Неверный выбор. Запустите снова.
    goto :fail
)
echo  Выбран бэкенд: %LLAMA_MATCH%
echo.
:after_backend

REM ---- Подготовка структуры папок --------------------------------------------
echo [1/6] Создание структуры папок...
for %%D in ("%PORTABLE_DIR%" "%APP_DIR%" "%PY_DIR%" "%LLAMA_DIR%" "%MODELS_DIR%" "%TMP_DIR%") do (
    if not exist "%%~D" mkdir "%%~D"
)

REM ---- Копирование исходников приложения -------------------------------------
echo [2/6] Копирование файлов проекта...
REM Копируем все .py и конфиги, исключая саму портативную папку и служебное.
robocopy "%~dp0." "%APP_DIR%" *.py /XD "%PORTABLE_DIR%" "__pycache__" ".git" ".chainlit" /NJH /NJS /NDL /NP >nul
copy /Y "%~dp0requirements.txt" "%APP_DIR%\requirements.txt" >nul 2>&1
if exist "%~dp0.chainlit" robocopy "%~dp0.chainlit" "%APP_DIR%\.chainlit" /E /NJH /NJS /NDL /NP >nul
if exist "%~dp0chainlit.md" copy /Y "%~dp0chainlit.md" "%APP_DIR%\chainlit.md" >nul 2>&1

REM ---- Загрузка embeddable Python --------------------------------------------
echo [3/6] Загрузка portable Python %PY_VERSION%...
if exist "%PY_DIR%\python.exe" (
    echo       Python уже присутствует, пропуск.
) else (
    powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PY_EMBED_URL%' -OutFile '%TMP_DIR%\python-embed.zip' } catch { Write-Host $_.Exception.Message; exit 1 }"
    if errorlevel 1 ( echo  [ОШИБКА] Не удалось скачать Python. Проверьте PY_VERSION и интернет. & goto :fail )
    powershell -NoProfile -Command "Expand-Archive -Path '%TMP_DIR%\python-embed.zip' -DestinationPath '%PY_DIR%' -Force"

    REM Включаем site-packages в embeddable Python (раскомментируем import site)
    for %%F in ("%PY_DIR%\python*._pth") do (
        powershell -NoProfile -Command "(Get-Content '%%~F') -replace '#\s*import site','import site' | Set-Content '%%~F'"
    )
)

REM ---- Установка pip и зависимостей ------------------------------------------
echo [4/6] Установка зависимостей (это может занять несколько минут)...
set "PY_EXE=%PY_DIR%\python.exe"
"%PY_EXE%" -c "import pip" 2>nul
if errorlevel 1 (
    powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%GET_PIP_URL%' -OutFile '%TMP_DIR%\get-pip.py' } catch { exit 1 }"
    if errorlevel 1 ( echo  [ОШИБКА] Не удалось скачать get-pip.py. & goto :fail )
    "%PY_EXE%" "%TMP_DIR%\get-pip.py" --no-warn-script-location
)
"%PY_EXE%" -m pip install --upgrade pip --no-warn-script-location
"%PY_EXE%" -m pip install -r "%APP_DIR%\requirements.txt" --no-warn-script-location
if errorlevel 1 ( echo  [ОШИБКА] Установка зависимостей не удалась. См. сообщения выше. & goto :fail )

REM ---- Загрузка llama.cpp server (последний релиз, нужный бэкенд) -------------
echo [5/6] Поиск и загрузка llama.cpp server (%LLAMA_MATCH%)...
if "%LLAMA_MATCH%"=="SKIP" (
    echo       Пропущено по вашему выбору. Локальные модели — через LM Studio/Ollama.
) else if exist "%LLAMA_DIR%\llama-server.exe" (
    echo       llama-server уже присутствует, пропуск.
) else (
    REM Определяем URL ассета через GitHub API + HTML-фолбэк (без хардкода версии).
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_download_llama_win.ps1" -Match "%LLAMA_MATCH%" -OutFile "%TMP_DIR%\llama.zip"
    if errorlevel 1 (
        echo  [ОШИБКА] Не удалось найти/скачать llama.cpp для '%LLAMA_MATCH%'.
        echo           Скачайте вручную со страницы релизов и распакуйте в:
        echo           %LLAMA_DIR%
        echo           https://github.com/ggml-org/llama.cpp/releases/latest
    ) else (
        powershell -NoProfile -Command "Expand-Archive -Path '%TMP_DIR%\llama.zip' -DestinationPath '%LLAMA_DIR%' -Force"
        REM Релизы иногда кладут бинарники во вложенную папку — поднимаем наверх.
        if not exist "%LLAMA_DIR%\llama-server.exe" (
            for /f "delims=" %%S in ('dir /b /s "%LLAMA_DIR%\llama-server.exe" 2^>nul') do (
                robocopy "%%~dpS." "%LLAMA_DIR%" /E /MOVE /NJH /NJS /NDL /NP >nul
            )
        )
    )
)

REM ---- Генерация launcher-скриптов -------------------------------------------
echo [6/6] Создание launcher-скриптов...

REM start_llama.bat — интерактивный CLI-менеджер моделей (запасной; обычно сервер
REM запускается прямо из интерфейса на вкладке "Llama-сервер").
(
echo @echo off
echo setlocal
echo chcp 65001 ^>nul
echo title CorePilot - llama.cpp manager
echo set "HERE=%%~dp0"
echo set "LLAMA_MODELS_DIR=%%HERE%%models"
echo set "LLAMA_BIN_DIR=%%HERE%%llama"
echo "%%HERE%%python\python.exe" "%%HERE%%app\llama_manager.py"
echo pause
) > "%PORTABLE_DIR%\start_llama.bat"

REM START_HERE.bat — основной запуск: графический интерфейс DearPyGui (без браузера).
REM Сервером моделей и демоном управляют прямо из окна приложения.
(
echo @echo off
echo setlocal
echo chcp 65001 ^>nul
echo title CorePilot - AI Factory
echo set "HERE=%%~dp0"
echo cd /d "%%HERE%%app"
echo set "LLAMA_MODELS_DIR=%%HERE%%models"
echo set "LLAMA_BIN_DIR=%%HERE%%llama"
echo echo Запуск CorePilot (графический интерфейс)...
echo "%%HERE%%python\python.exe" "%%HERE%%app\ui_dpg.py"
echo if errorlevel 1 pause
) > "%PORTABLE_DIR%\START_HERE.bat"

REM start_chainlit.bat — ЗАПАСНОЙ веб-интерфейс (Chainlit) в браузере.
REM Требует раскомментированного chainlit в requirements.txt и его установки.
(
echo @echo off
echo setlocal
echo chcp 65001 ^>nul
echo title CorePilot - Chainlit (запасной веб-интерфейс)
echo set "HERE=%%~dp0"
echo cd /d "%%HERE%%app"
echo "%%HERE%%python\python.exe" -c "import chainlit" 2^>nul ^|^| ^(echo Chainlit не установлен. Раскомментируйте его в requirements.txt и переустановите. ^& pause ^& exit /b 1^)
echo echo Запуск веб-интерфейса (откроется браузер: http://localhost:8000)...
echo "%%HERE%%python\python.exe" -m chainlit run app.py -w --port 8000
echo pause
) > "%PORTABLE_DIR%\start_chainlit.bat"

REM Очистка временных файлов
if exist "%TMP_DIR%" rmdir /s /q "%TMP_DIR%"

echo.
echo ================================================================
echo   ГОТОВО. Портативная версия собрана в:
echo   %PORTABLE_DIR%
echo ----------------------------------------------------------------
echo   ДАЛЬШЕ:
echo    1. Положите GGUF-модель в папку  models\
echo    2. Запустите  START_HERE.bat  (графический интерфейс)
echo.
echo   Интерфейс: DearPyGui (без браузера). Сервером моделей и демоном
echo   управляйте на вкладках "Llama-сервер" и "Управление".
echo   Бинарник сервера: llama\llama-server.exe (бэкенд: %LLAMA_MATCH%)
echo   Запасной веб-интерфейс: start_chainlit.bat (нужен chainlit).
echo ================================================================
echo.
pause
exit /b 0

:fail
echo.
echo  Сборка прервана. Исправьте проблему и запустите Portable.bat снова.
echo.
pause
exit /b 1
