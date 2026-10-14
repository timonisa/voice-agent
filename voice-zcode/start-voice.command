#!/bin/bash
# Запуск голосового интерфейса ZCode на macOS (двойной клик в Finder)
cd "$(dirname "$0")"
".venv/bin/python3" voice.py
read -r -p "Нажми Enter чтобы закрыть…" _
