@echo off
rem Озвучивает текст аргументов системным голосом (Irina, офлайн).
rem Использование: say.cmd Привет, как дела
setlocal
set "TEXT=%*"
set "TEXT=%TEXT:'=''%"
powershell.exe -NoProfile -Command "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('%TEXT%')"
