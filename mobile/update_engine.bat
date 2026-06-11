@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title CorePilot Mobile - Обновление движка llama.cpp

echo ============================================================
echo   Обновление движка llama.cpp (Android ARM64)
echo ============================================================
echo.
echo   Скачает новую версию, обновит pyproject.toml.
echo   После обновления пересоберите APK: build_apk.bat
echo.

cd /d "%~dp0"

:: Проверить, что движок уже установлен
if not exist "%~dp0engine\arm64-v8a\libllama-server.so" (
    echo [!] Движок ещё не установлен. Запускаю setup_engine.bat...
    call "%~dp0setup_engine.bat"
    exit /b %errorlevel%
)

:: Показать текущую версию (по дате файла)
echo Текущий движок:
for %%F in ("%~dp0engine\arm64-v8a\libllama-server.so") do (
    echo     libllama-server.so  %%~tF  (%%~zF байт)
)
echo.

:: Создать резервную копию
set "BACKUP_DIR=%~dp0engine\backup_%DATE:~6,4%%DATE:~3,2%%DATE:~0,2%"
echo Создаю резервную копию в %BACKUP_DIR%...
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"
xcopy /y /q "%~dp0engine\arm64-v8a\*" "%BACKUP_DIR%\" >nul 2>&1
echo     [OK] Резервная копия создана.
echo.

:: Запустить установку (setup_engine.bat сам очистит старые файлы)
call "%~dp0setup_engine.bat"
if errorlevel 1 (
    echo.
    echo [!] Обновление не удалось. Восстанавливаю из резервной копии...
    if exist "%BACKUP_DIR%" (
        xcopy /y /q "%BACKUP_DIR%\*" "%~dp0engine\arm64-v8a\" >nul 2>&1
        echo     [OK] Старая версия восстановлена.
    )
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Движок обновлён. Старая версия: %BACKUP_DIR%
echo.
echo   Следующий шаг: build_apk.bat  (пересобрать APK)
echo   Затем:         install_apk.bat (переустановить на телефон)
echo.
echo   Если всё работает — папку backup можно удалить.
echo ============================================================
echo.
pause
endlocal
exit /b 0
