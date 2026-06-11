@echo off
chcp 65001 >nul
title CorePilot Mobile - сборка APK
setlocal enabledelayedexpansion

REM ============================================================================
REM  build_apk.bat — надёжная сборка Android APK из проекта (Flet).
REM
REM  Исправлено по итогам полевой сборки:
REM   - авто-принятие лицензий Android SDK (без ручного flutter doctor)
REM   - надёжный поиск Python/Git, динамическое добавление Flutter в PATH сессии
REM     (без хардкода путей конкретного пользователя)
REM   - точная версия flet в зависимостях (символ < ломал парсер CMD)
REM
REM  Нужно заранее: Python 3.12 (с галочкой "Add to PATH"). Flutter/JDK/Android SDK
REM  Flet скачает сам при первой сборке (~1-2 ГБ, 10-20 минут — это нормально).
REM ============================================================================

cd /d "%~dp0"

echo ================================================================
echo   CorePilot Mobile — сборка APK
echo ================================================================
echo.

REM --- Python: ищем надёжно (py launcher или python из PATH) ---
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY ( where py >nul 2>&1 && set "PY=py" )
if not defined PY (
    echo [ОШИБКА] Python не найден в PATH.
    echo Установите Python 3.12 с https://www.python.org/downloads/
    echo и отметьте галочку "Add Python to PATH".
    pause & exit /b 1
)
echo [OK] Python: !PY!

REM --- Git: нужен Flet для скачивания Flutter. Ищем и добавляем в PATH ---
where git >nul 2>&1
if errorlevel 1 (
    for %%G in (
        "%ProgramFiles%\Git\cmd"
        "%ProgramFiles(x86)%\Git\cmd"
        "%LocalAppData%\Programs\Git\cmd"
    ) do (
        if exist "%%~G\git.exe" (
            set "PATH=%%~G;!PATH!"
            echo [OK] Git найден: %%~G
        )
    )
) else (
    echo [OK] Git: в PATH
)

REM --- Flet CLI ---
echo [1/3] Проверка/установка Flet...
!PY! -c "import flet" >nul 2>&1
if errorlevel 1 (
    echo     Flet не найден — устанавливаю...
    !PY! -m pip install --upgrade pip >nul 2>&1
    !PY! -m pip install "flet==0.80.5"
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось установить Flet. Проверьте интернет.
        pause & exit /b 1
    )
) else (
    echo     Flet уже установлен.
)

REM --- Flutter: если установлен — добавим в PATH сессии динамически ---
echo [2/3] Поиск Flutter...
where flutter >nul 2>&1
if errorlevel 1 (
    REM Фиксированные пути
    for %%F in (
        "%LocalAppData%\flet\flutter\bin"
        "%UserProfile%\flutter\bin"
        "%LocalAppData%\Pub\Cache\bin"
        "C:\flutter\bin"
    ) do (
        if exist "%%~F\flutter.bat" (
            set "PATH=%%~F;!PATH!"
            echo [OK] Flutter найден: %%~F
        )
    )
    REM Версионированные пути (flutter\X.Y.Z\bin — Flutter installer 2+)
    for /d %%V in ("%UserProfile%\flutter\*") do (
        if exist "%%V\bin\flutter.bat" (
            set "PATH=%%V\bin;!PATH!"
            echo [OK] Flutter найден: %%V\bin
        )
    )
) else (
    echo [OK] Flutter: в PATH
)

REM --- Проверка движка llama-server (Edge AI) ---
set "ENGINE_OK=0"
if exist "%~dp0engine\arm64-v8a\libllama-server.so" (
    REM Движок есть — проверим, что прописан в pyproject.toml
    findstr /c:"[tool.flet.android.libs]" "%~dp0pyproject.toml" >nul 2>&1
    if errorlevel 1 (
        echo [!] Движок найден, но НЕ прописан в pyproject.toml. Прописываю...
        !PY! "%~dp0_patch_pyproject.py"
        if errorlevel 1 (
            echo [ПРЕДУПРЕЖДЕНИЕ] Не удалось обновить pyproject.toml. Edge AI в APK не будет.
        ) else (
            set "ENGINE_OK=1"
        )
    ) else (
        echo [OK] Движок llama-server: встроен
        set "ENGINE_OK=1"
    )
) else (
    echo [!] Движок llama-server НЕ найден в engine\arm64-v8a\.
    echo     Edge AI на телефоне работать не будет (облачный чат — будет).
    echo     Для установки движка запустите:  setup_engine.bat
    echo.
)

REM --- Авто-принятие лицензий Android SDK (чтобы юзер не делал руками) ---
REM Лицензии спрашиваются при первой сборке; прожимаем 'y' заранее, если sdkmanager есть.
for %%S in (
    "%LocalAppData%\Android\Sdk\cmdline-tools\latest\bin\sdkmanager.bat"
    "%LocalAppData%\flet\android-sdk\cmdline-tools\latest\bin\sdkmanager.bat"
    "%Android_Home%\cmdline-tools\latest\bin\sdkmanager.bat"
) do (
    if exist "%%~S" (
        echo [OK] Принимаю лицензии Android SDK...
        (for /l %%i in (1,1,20) do @echo y) | "%%~S" --licenses >nul 2>&1
    )
)

echo.
echo [3/3] Сборка APK...
echo     При ПЕРВОМ запуске Flet скачает Flutter SDK + JDK + Android SDK (1-2 ГБ, 10-20 мин).
echo     Не закрывайте окно.
echo.
flet build apk --verbose
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Сборка не удалась. Прокрутите лог выше — там причина.
    echo Частые причины: нет интернета, мало места, не приняты лицензии SDK.
    echo Если ошибка про лицензии — запустите:  flutter doctor --android-licenses
    pause & exit /b 1
)

echo.
echo ================================================================
echo   ГОТОВО. APK собран:
echo     %CD%\build\flutter\build\app\outputs\flutter-apk\app-release.apk
echo ----------------------------------------------------------------
echo   Установить на телефон:  install_apk.bat
echo ================================================================
echo.
pause
exit /b 0
