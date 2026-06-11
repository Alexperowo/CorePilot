@echo off
chcp 65001 >nul
title CorePilot - Обновление llama.cpp server
setlocal EnableDelayedExpansion

REM ============================================================================
REM  update_llama.bat — обновляет движок локальных моделей (llama.cpp server)
REM  до последней версии. Работает и для стационарной (.\llama\), и для
REM  портативной (.\CorePilot_Portable\llama\) установки — папка ищется сама.
REM
REM  Безопасно: старый бинарник сохраняется в llama\backup_ГГГГММДД до успешной
REM  замены. Если что-то пойдёт не так — старую версию можно вернуть.
REM ============================================================================

cd /d "%~dp0"

echo ================================================================
echo   CorePilot — обновление llama.cpp server
echo ================================================================
echo.

REM ---- Поиск папки llama (стационар или портатив) ----------------------------
set "LLAMA_DIR="
if exist "%~dp0llama\llama-server.exe" set "LLAMA_DIR=%~dp0llama"
if not defined LLAMA_DIR if exist "%~dp0CorePilot_Portable\llama\llama-server.exe" set "LLAMA_DIR=%~dp0CorePilot_Portable\llama"

if not defined LLAMA_DIR (
    echo  [ВНИМАНИЕ] llama-server.exe не найден ни в .\llama\, ни в
    echo             .\CorePilot_Portable\llama\.
    echo             Сначала установите его через setup.bat или Portable.bat.
    echo.
    pause
    exit /b 1
)
echo  Текущая установка: %LLAMA_DIR%
echo.

REM ---- Выбор бэкенда (тот же набор, что при установке) -----------------------
echo  Выберите бэкенд для обновления (как при установке):
echo    [1] Vulkan       - универсальный GPU (AMD/Intel/NVIDIA). РЕКОМЕНДУЕТСЯ.
echo    [2] CUDA 12      - NVIDIA с CUDA 12.
echo    [3] CUDA 13      - NVIDIA с CUDA 13.
echo    [4] CPU          - без GPU.
echo.
set /p BACKEND_CHOICE="  Ваш выбор [1/2/3/4] (по умолчанию 1): "
if "%BACKEND_CHOICE%"=="" set "BACKEND_CHOICE=1"
set "LLAMA_MATCH="
if "%BACKEND_CHOICE%"=="1" set "LLAMA_MATCH=win-vulkan-x64"
if "%BACKEND_CHOICE%"=="2" set "LLAMA_MATCH=win-cuda-12"
if "%BACKEND_CHOICE%"=="3" set "LLAMA_MATCH=win-cuda-13"
if "%BACKEND_CHOICE%"=="4" set "LLAMA_MATCH=win-cpu-x64"
if "%LLAMA_MATCH%"=="" (
    echo  [ОШИБКА] Неверный выбор. Запустите снова.
    pause
    exit /b 1
)
echo  Выбран бэкенд: %LLAMA_MATCH%
echo.

set "LLAMA_API=https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
set "TMP_DIR=%~dp0_tmp_update"
if not exist "%TMP_DIR%" mkdir "%TMP_DIR%"

REM ---- Узнаём последнюю версию и сравниваем (по возможности) ------------------
echo  Проверка последней версии на GitHub...
for /f "delims=" %%T in ('powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { $h=@{'User-Agent'='CorePilot-Update';'Accept'='application/vnd.github+json'}; $r=Invoke-RestMethod -Uri 'https://api.github.com/repos/ggml-org/llama.cpp/releases/latest' -Headers $h -TimeoutSec 15; Write-Output $r.tag_name } catch { $ua='Mozilla/5.0'; $wr=Invoke-WebRequest -Uri 'https://github.com/ggml-org/llama.cpp/releases/latest' -UseBasicParsing -UserAgent $ua; Write-Output $wr.BaseResponse.ResponseUri.AbsoluteUri.Split('/')[-1] }" 2^>nul') do set "LATEST_TAG=%%T"

if defined LATEST_TAG (
    echo  Последний релиз llama.cpp: %LATEST_TAG%
) else (
    echo  [ВНИМАНИЕ] Не удалось узнать версию (нет интернета?). Прерываю.
    rmdir /s /q "%TMP_DIR%" 2>nul
    pause
    exit /b 1
)
echo.

REM ---- Скачиваем нужный ассет ------------------------------------------------
echo  Загрузка llama.cpp (%LLAMA_MATCH%)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_download_llama_win.ps1" -Match "%LLAMA_MATCH%" -OutFile "%TMP_DIR%\llama.zip"
if errorlevel 1 (
    echo  [ОШИБКА] Не удалось скачать llama.cpp для '%LLAMA_MATCH%'.
    echo           Проверьте интернет или скачайте вручную:
    echo           https://github.com/ggml-org/llama.cpp/releases/latest
    rmdir /s /q "%TMP_DIR%" 2>nul
    pause
    exit /b 1
)

REM ---- Резервная копия старого бинарника -------------------------------------
set "BACKUP_DIR=%LLAMA_DIR%\backup_%date:~-4%%date:~3,2%%date:~0,2%"
echo  Резервная копия старой версии в: %BACKUP_DIR%
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"
copy /Y "%LLAMA_DIR%\*.exe" "%BACKUP_DIR%\" >nul 2>&1
copy /Y "%LLAMA_DIR%\*.dll" "%BACKUP_DIR%\" >nul 2>&1

REM ---- Распаковка поверх -----------------------------------------------------
echo  Установка новой версии...
powershell -NoProfile -Command "Expand-Archive -Path '%TMP_DIR%\llama.zip' -DestinationPath '%LLAMA_DIR%' -Force"
REM Релизы иногда кладут бинарники во вложенную папку — поднимаем наверх.
if not exist "%LLAMA_DIR%\llama-server.exe" (
    for /f "delims=" %%S in ('dir /b /s "%LLAMA_DIR%\llama-server.exe" 2^>nul') do (
        robocopy "%%~dpS." "%LLAMA_DIR%" /E /MOVE /NJH /NJS /NDL /NP >nul
    )
)

rmdir /s /q "%TMP_DIR%" 2>nul

if exist "%LLAMA_DIR%\llama-server.exe" (
    echo.
    echo ================================================================
    echo   ГОТОВО. llama.cpp server обновлён до %LATEST_TAG%.
    echo   Папка: %LLAMA_DIR%
    echo   Старая версия сохранена: %BACKUP_DIR%
    echo   (если новая версия работает — папку backup можно удалить)
    echo ================================================================
) else (
    echo  [ОШИБКА] После распаковки llama-server.exe не найден.
    echo           Восстановите из резервной копии: %BACKUP_DIR%
)
echo.
pause
exit /b 0
