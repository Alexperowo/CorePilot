@echo off
chcp 65001 >nul
title CorePilot Mobile - Сборка (Ultimate Fix)
setlocal

REM Жёстко фиксируем рабочую папку
cd /d "%~dp0"

echo [1/4] Определяем чистый путь к Python (обход Windows Store)...
for /f "delims=" %%i in ('python -c "import sys, os; print(os.path.dirname(sys.executable))"') do set "REAL_PYTHON=%%i"
echo     Найден Python: %REAL_PYTHON%
REM Ставим настоящий питон в самый приоритет
set "PATH=%REAL_PYTHON%;%REAL_PYTHON%\Scripts;%PATH%"

echo [2/4] Подготавливаем короткую папку TEMP (защита от длинных путей)...
mkdir C:\flet_temp >nul 2>&1
set "TEMP=C:\flet_temp"
set "TMP=C:\flet_temp"

echo [3/4] Инжектируем Flutter и Git...
set "PATH=%PATH%;C:\Users\User\flutter\3.38.7\bin"
set "PATH=%PATH%;C:\Program Files\Git\cmd;C:\Program Files\Git\bin"

echo.
echo [4/4] Запускаем финальную сборку Flet...
echo.
call flet build apk --verbose

echo.
echo Сборка завершена или прервана.
pause
