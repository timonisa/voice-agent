@echo off
rem === Установка голосового интерфейса ZCode (Windows) ===
rem Запускать на машине с установленным приложением ZCode (пользователь залогинен).
chcp 65001 >nul
setlocal
set SRC=%~dp0voice-zcode
set DST=%USERPROFILE%\voice-zcode

echo [1/5] Копирую проект в %DST%
robocopy "%SRC%" "%DST%" /E /NFL /NDL /NJH /NJS /XF crash.log out.log voice.log >nul
if errorlevel 8 goto :err

echo [2/5] Копирую навык агенту
xcopy /Y /I "%~dp0skills\zcode-voice" "%USERPROFILE%\.zcode\skills\zcode-voice" >nul || goto :err

echo [3/5] Проверяю Python 3.12
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist "%PY%" (
    echo       Ставлю Python 3.12 через winget...
    winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements || goto :err
)

echo [4/5] Создаю окружение и ставлю зависимости (5-10 минут)
cd /d "%DST%"
"%PY%" -m venv .venv || goto :err
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto :err
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :err

echo [5/5] Создаю ярлыки (рабочий стол + автозапуск)
powershell -NoProfile -Command ^
 "$ws = New-Object -ComObject WScript.Shell;" ^
 "$desktop = [Environment]::GetFolderPath('Desktop'); $startup = [Environment]::GetFolderPath('Startup');" ^
 "$l = $ws.CreateShortcut(\"$desktop\Голосовой ZCode.lnk\"); $l.TargetPath = \"$env:USERPROFILE\voice-zcode\.venv\Scripts\pythonw.exe\"; $l.Arguments = 'voice.py'; $l.WorkingDirectory = \"$env:USERPROFILE\voice-zcode\"; $l.IconLocation = 'C:\Program Files\ZCode\ZCode.exe,0'; $l.Save();" ^
 "$l2 = $ws.CreateShortcut(\"$startup\Голосовой ZCode.lnk\"); $l2.TargetPath = \"$env:USERPROFILE\voice-zcode\.venv\Scripts\pythonw.exe\"; $l2.Arguments = 'voice.py'; $l2.WorkingDirectory = \"$env:USERPROFILE\voice-zcode\"; $l2.IconLocation = 'C:\Program Files\ZCode\ZCode.exe,0'; $l2.Save()" || goto :err

echo.
echo ГОТОВО! Первый запуск скачает модель whisper-medium (~1.5 ГБ).
echo Запусти «Голосовой ZCode» с рабочего стола и скажи: голова робота, привет.
pause
exit /b 0

:err
echo.
echo ОШИБКА на этом шаге — посмотри сообщение выше и повтори вручную по инструкции.
pause
exit /b 1
