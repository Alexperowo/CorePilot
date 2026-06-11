@echo off
chcp 65001 >nul
title CorePilot Mobile - предпросмотр на ПК
setlocal

REM ============================================================================
REM  run_dev.bat — запускает приложение на ПК (без сборки APK), чтобы быстро
REM  посмотреть интерфейс и проверить настройки/связь с ПК.
REM
REM  Голос и локальный llama-сервер на ПК работают в режиме фолбэка (это нормально —
REM  они задействуют системные API Android только на телефоне).
REM ============================================================================

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден в PATH. Установите Python 3.12.
    pause
    exit /b 1
)

python -c "import flet" >nul 2>&1
if errorlevel 1 (
    echo Flet не найден — устанавливаю...
    pip install "flet>=0.27.0"
    if errorlevel 1 ( echo [ОШИБКА] Не удалось установить Flet. & pause & exit /b 1 )
)

echo Запуск предпросмотра CorePilot Mobile...
flet run
exit /b 0
