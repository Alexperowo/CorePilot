@echo off
chcp 65001 >nul
title CorePilot - AI Factory
setlocal

REM ============================================================================
REM  start.bat — запуск CorePilot (стационарная версия, графический интерфейс).
REM  Перед первым запуском выполните setup.bat (создаёт venv и ставит зависимости).
REM ============================================================================

cd /d "%~dp0"

REM --- Проверка, что установка выполнена ---
if not exist "venv\Scripts\python.exe" (
    echo [ОШИБКА] Виртуальная среда не найдена.
    echo Сначала запустите setup.bat для установки зависимостей.
    echo.
    pause
    exit /b 1
)

echo Запуск CorePilot (графический интерфейс)...
"venv\Scripts\python.exe" ui_dpg.py
if errorlevel 1 (
    echo.
    echo Приложение завершилось с ошибкой. Подробности — выше в этом окне.
    pause
    exit /b 1
)

exit /b 0
