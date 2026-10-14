@echo off
rem Запуск голосового интерфейса ZCode (push-to-talk Ctrl+Alt+Пробел)
cd /d "%USERPROFILE%\voice-zcode"
".venv\Scripts\python.exe" voice.py
pause
