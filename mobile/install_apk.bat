@echo off
chcp 65001 >nul
title CorePilot Mobile - установка APK на телефон
setlocal EnableDelayedExpansion

REM ============================================================================
REM  install_apk.bat — ставит собранный APK на подключённый телефон через adb.
REM  Сначала соберите APK через build_apk.bat.
REM
REM  Требуется adb (Android Platform Tools) в PATH и телефон с включённой
REM  "Отладкой по USB" (или подключённый по Wi-Fi: adb connect IP:5555).
REM ============================================================================

cd /d "%~dp0"

where adb >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] adb не найден в PATH.
    echo Установите Android Platform Tools и добавьте их в PATH.
    pause
    exit /b 1
)

REM --- Поиск APK (Flet 0.80+ кладёт в flutter\build\app\outputs\flutter-apk\) ---
set "APK="
for /f "delims=" %%f in ('dir /b /s "build\flutter\build\app\outputs\flutter-apk\*.apk" 2^>nul') do set "APK=%%f"
if not defined APK (
    for /f "delims=" %%f in ('dir /b /s "build\apk\*.apk" 2^>nul') do set "APK=%%f"
)
if not defined APK (
    echo [ОШИБКА] APK не найден. Ожидаемый путь:
    echo   build\flutter\build\app\outputs\flutter-apk\app-release.apk
    echo Сначала соберите приложение: build_apk.bat
    pause
    exit /b 1
)
echo Найден APK: !APK!
echo.

REM --- Проверка устройства ---
set "DEVICE="
for /f "skip=1 tokens=1,2" %%a in ('adb devices') do (
    if "%%b"=="device" set "DEVICE=%%a"
)
if not defined DEVICE (
    echo [ОШИБКА] Телефон не найден. Включите "Отладку по USB" и разрешите доступ,
    echo либо подключитесь по Wi-Fi:  adb connect IP:5555
    pause
    exit /b 1
)
echo Устройство: !DEVICE!
echo.

echo Установка (флаг -r — обновление поверх, данные сохраняются)...
adb install -r "!APK!"
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Установка не удалась. Проверьте, что на телефоне разрешена
    echo установка из этого источника.
    pause
    exit /b 1
)

echo.
echo ================================================================
echo   ГОТОВО. CorePilot Mobile установлен.
echo   Не забудьте залить модели:  update_mobile.bat
echo ================================================================
echo.
pause
exit /b 0
