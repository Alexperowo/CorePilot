@echo off
chcp 65001 >nul
title CorePilot - Chainlit (запасной веб-интерфейс)
setlocal

REM ============================================================================
REM  start_chainlit.bat — ЗАПАСНОЙ веб-интерфейс (Chainlit) для стационарной версии.
REM  Основной интерфейс — графический (start.bat). Этот нужен, только если вы
REM  предпочитаете браузерный UI.
REM  Требует раскомментированного chainlit в requirements.txt и повторного setup.bat.
REM ============================================================================

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [ОШИБКА] Виртуальная среда не найдена. Сначала запустите setup.bat.
    pause
    exit /b 1
)

"venv\Scripts\python.exe" -c "import chainlit" 2>nul || (
    echo [ОШИБКА] Chainlit не установлен.
    echo Раскомментируйте строку chainlit в requirements.txt,
    echo затем запустите setup.bat повторно.
    pause
    exit /b 1
)

echo Запуск веб-интерфейса (откроется браузер: http://localhost:8000)...
"venv\Scripts\python.exe" -m chainlit run app.py -w --port 8000
pause
exit /b 0
