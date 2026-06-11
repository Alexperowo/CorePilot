@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title CorePilot Mobile - Setup Engine (llama.cpp Android)

echo ============================================================
echo   llama.cpp Android ARM64 - автоматическая установка движка
echo ============================================================
echo.
echo   Скрипт скачает последний llama.cpp для Android (arm64),
echo   поместит файлы в engine\arm64-v8a\ и автоматически
echo   пропишет их в pyproject.toml для сборки APK.
echo.

cd /d "%~dp0"

:: ── 1-4. Скачать и распаковать движок (PowerShell-скрипт)
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0_download_engine.ps1"
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Не удалось скачать движок.
    pause
    exit /b 1
)

:: ── 5. Обновить pyproject.toml автоматически
echo.
echo [5/5] Обновляем pyproject.toml...

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY ( where py >nul 2>&1 && set "PY=py" )
if not defined PY (
    echo [ОШИБКА] Python не найден — не удалось обновить pyproject.toml.
    goto :manual_hint
)

!PY! "%~dp0_patch_pyproject.py"
if errorlevel 1 (
    echo [ОШИБКА] Не удалось обновить pyproject.toml.
    goto :manual_hint
)

echo.
echo ============================================================
echo   ГОТОВО! Движок llama.cpp установлен и прописан в проекте.
echo.
echo   Следующий шаг: build_apk.bat  (сборка APK с движком)
echo ============================================================
echo.
pause
endlocal
exit /b 0

:manual_hint
echo.
echo   Файлы скопированы в engine\arm64-v8a\, но pyproject.toml
echo   не обновлён. Запустите вручную:
echo     python _patch_pyproject.py
echo.
pause
endlocal
exit /b 1
