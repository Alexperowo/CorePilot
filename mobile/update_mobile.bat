@echo off
chcp 65001 >nul
title CorePilot Mobile - обновление моделей
setlocal EnableDelayedExpansion

REM ============================================================================
REM  update_mobile.bat — заливает модели .gguf на телефон по USB или Wi-Fi (adb).
REM
REM  ВАЖНО про движок: на нерутованном Android бинарник llama-server НЕЛЬЗЯ
REM  запускать из публичной папки (Download смонтирован noexec, домашняя папка
REM  приложения — W^X для targetAPI>=29). Поэтому движок ВСТРОЕН В APK (как
REM  нативная библиотека) и обновляется ТОЛЬКО переустановкой APK (build_apk.bat
REM  + install_apk.bat). Этим батником заливаются только МОДЕЛИ.
REM
REM  Структура на телефоне (публичная папка, видимая приложению):
REM    /storage/emulated/0/Download/CorePilot/models/   <- файлы .gguf
REM
REM  Структура на ПК (рядом с этим батником):
REM    models\   <- положите сюда .gguf модели
REM
REM  Требуется установленный adb (Android Platform Tools) в PATH.
REM ============================================================================

cd /d "%~dp0"

set "REMOTE=/storage/emulated/0/Download/CorePilot"
set "LOCAL_MODELS=models"

echo ================================================================
echo   CorePilot Mobile — заливка моделей на телефон
echo ================================================================
echo.

REM --- Проверка adb ---
where adb >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] adb не найден в PATH.
    echo Установите Android Platform Tools:
    echo   https://developer.android.com/tools/releases/platform-tools
    echo и добавьте папку с adb.exe в переменную PATH.
    echo.
    pause
    exit /b 1
)

REM --- Проверка подключённого устройства ---
echo [1/3] Поиск устройства...
set "DEVICE_FOUND="
for /f "skip=1 tokens=1,2" %%a in ('adb devices') do (
    if "%%b"=="device" set "DEVICE_FOUND=%%a"
)
if not defined DEVICE_FOUND (
    echo [ОШИБКА] Устройство не найдено.
    echo  - USB: включите "Отладку по USB" в параметрах разработчика и разрешите доступ.
    echo  - Wi-Fi: сначала подключитесь командой  adb connect IP:5555
    echo.
    pause
    exit /b 1
)
echo     Устройство: !DEVICE_FOUND!
echo.

REM --- Создание папок на телефоне ---
echo [2/3] Подготовка папок на телефоне...
adb shell mkdir -p "%REMOTE%/models" "%REMOTE%/logs" >nul 2>&1

REM --- Заливка моделей ---
echo [3/3] Заливка моделей (models\*.gguf)...
if exist "%LOCAL_MODELS%\*.gguf" (
    for %%f in ("%LOCAL_MODELS%\*.gguf") do (
        echo     -^> %%~nxf  (это может занять время^)
        adb push "%%f" "%REMOTE%/models/" >nul
        if errorlevel 1 echo        [!] не удалось залить %%~nxf
    )
) else (
    echo     [пропуск] в models\ нет .gguf — положите сюда модели.
)
echo.

echo ================================================================
echo   ГОТОВО. Модели обновлены на телефоне.
echo   Откройте CorePilot Mobile -> вкладка Edge AI -> выберите модель.
echo
echo   Напоминание: движок (llama-server) обновляется через переустановку
echo   APK (build_apk.bat + install_apk.bat), а не этим батником.
echo ================================================================
echo.
pause
exit /b 0
