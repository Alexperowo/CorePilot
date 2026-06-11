@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title CorePilot - Download llama.cpp Android Engine

echo ============================================================
echo   llama.cpp Android ARM64 - Download Engine
echo ============================================================
echo.

:: ── Целевая папка в проекте
set "TARGET_DIR=%~dp0engine\arm64-v8a"
set "TEMP_DIR=%TEMP%\llama_android_%RANDOM%"
mkdir "%TEMP_DIR%" 2>nul

:: ── 1. Получить URL последнего android-aarch64 архива
echo [1/6] Запрашиваем последний релиз llama.cpp...

:: ИСПРАВЛЕНИЕ: добавлены заголовки User-Agent и Accept (без них GitHub API возвращает 403)
::              добавлен $ProgressPreference='SilentlyContinue' для корректной работы
powershell -ExecutionPolicy Bypass -NoProfile -Command "$ProgressPreference='SilentlyContinue'; $h=@{'User-Agent'='llama-dl/1.0';'Accept'='application/vnd.github+json'}; $r=Invoke-RestMethod -Uri 'https://api.github.com/repos/ggerganov/llama.cpp/releases/latest' -Headers $h; $a=$r.assets | Where-Object { $_.name -like '*android-aarch64.zip' } | Select-Object -First 1; if(-not $a){exit 1}; $a.browser_download_url | Out-File -Encoding ascii '%TEMP_DIR%\url.txt'"

if errorlevel 1 (
    echo.
    echo [ОШИБКА] Не удалось получить список релизов с GitHub.
    echo          Проверьте подключение к интернету.
    goto :fail
)

set /p ASSET_URL=< "%TEMP_DIR%\url.txt"

if not defined ASSET_URL (
    echo [ОШИБКА] Не удалось найти android-aarch64.zip в последнем релизе.
    goto :fail
)

for %%F in ("%ASSET_URL%") do set "ZIP_NAME=%%~nxF"
echo     Найден : %ZIP_NAME%
echo     URL    : %ASSET_URL%

:: ── 2. Скачать архив
echo.
echo [2/6] Скачиваем архив (~50-200 МБ, подождите)...
set "ZIP_PATH=%TEMP_DIR%\%ZIP_NAME%"

:: ИСПРАВЛЕНИЕ: добавлены $ProgressPreference и User-Agent (без SilentlyContinue скорость падает в 10x)
powershell -ExecutionPolicy Bypass -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%ASSET_URL%' -OutFile '%ZIP_PATH%' -UseBasicParsing -Headers @{'User-Agent'='curl/7.68.0'}"

if not exist "%ZIP_PATH%" (
    echo.
    echo [ОШИБКА] Файл не скачан. Проверьте подключение к интернету.
    goto :fail
)
echo     Сохранён: %ZIP_PATH%

:: ── 3. Распаковать архив
echo.
echo [3/6] Распаковываем архив...
set "UNZIP_DIR=%TEMP_DIR%\extracted"
powershell -ExecutionPolicy Bypass -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Expand-Archive -Path '%ZIP_PATH%' -DestinationPath '%UNZIP_DIR%' -Force"

if not exist "%UNZIP_DIR%" (
    echo.
    echo [ОШИБКА] Не удалось распаковать архив.
    goto :fail
)

:: ── 4. Создать целевую папку
echo.
echo [4/6] Создаём целевую папку: %TARGET_DIR%
if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"

:: ── 5. Копировать файлы
echo.
echo [5/6] Копируем файлы...

:: ИСПРАВЛЕНИЕ: заменён for /r (не работает с именами без wildcard) на dir /s /b через for /f
:: Сначала ищем llama-server без расширения (Android ELF binary)
set "SERVER_SRC="
for /f "delims=" %%F in ('dir /s /b "%UNZIP_DIR%\llama-server" 2^>nul') do (
    if not defined SERVER_SRC set "SERVER_SRC=%%F"
)
:: Если не нашли — ищем с любым расширением, кроме .so
if not defined SERVER_SRC (
    for /f "delims=" %%F in ('dir /s /b "%UNZIP_DIR%\llama-server.*" 2^>nul') do (
        if not defined SERVER_SRC (
            set "_ext=%%~xF"
            if /i not "!_ext!"==".so" set "SERVER_SRC=%%F"
        )
    )
)

if defined SERVER_SRC (
    copy /y "!SERVER_SRC!" "%TARGET_DIR%\libllama-server.so" >nul
    echo     [OK] llama-server  -^>  libllama-server.so
) else (
    echo     [WARN] Бинарник llama-server не найден в архиве.
)

:: Копируем все .so файлы
set "SO_COUNT=0"
for /r "%UNZIP_DIR%" %%F in (*.so) do (
    copy /y "%%F" "%TARGET_DIR%\%%~nxF" >nul
    echo     [OK] %%~nxF
    set /a SO_COUNT+=1
)
echo     Скопировано .so: !SO_COUNT!

:: ── 6. Удалить временные файлы
echo.
echo [6/6] Удаляем временные файлы...
rd /s /q "%TEMP_DIR%" 2>nul
echo     Готово.

:: ── Итог: блок для pyproject.toml
echo.
echo ============================================================
echo   Вставьте этот блок в конец pyproject.toml:
echo ============================================================
echo.
echo [tool.flet.android.libs]

if defined SERVER_SRC (
    echo "arm64-v8a/libllama-server.so" = "engine/arm64-v8a/libllama-server.so"
)

for %%F in ("%TARGET_DIR%\*.so") do (
    set "_name=%%~nxF"
    if /i not "!_name!"=="libllama-server.so" (
        echo "arm64-v8a/!_name!" = "engine/arm64-v8a/!_name!"
    )
)

echo.
echo ============================================================
echo   Файлы находятся в: %TARGET_DIR%
echo ============================================================
echo.
pause
endlocal
exit /b 0

:fail
echo.
if exist "%TEMP_DIR%" rd /s /q "%TEMP_DIR%" 2>nul
pause
endlocal
exit /b 1
