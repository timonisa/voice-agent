#!/bin/bash
# === Установка голосового интерфейса ZCode (macOS) ===
# Запускать на Маке с установленным приложением ZCode (пользователь залогинен):
#   bash install.sh
set -e
SRC="$(cd "$(dirname "$0")" && pwd)/voice-zcode"
DST="$HOME/voice-zcode"

echo "[1/6] Копирую проект в $DST"
mkdir -p "$DST"
rsync -a --exclude 'crash.log' --exclude 'out.log' --exclude 'voice.log' "$SRC/" "$DST/" 2>/dev/null || cp -R "$SRC/." "$DST/"

echo "[2/6] Копирую навык агенту"
mkdir -p "$HOME/.zcode/skills"
cp -R "$(dirname "$0")/skills/zcode-voice" "$HOME/.zcode/skills/"

echo "[3/6] Проверяю python3 (нужен 3.10+)"
if ! command -v python3 >/dev/null; then
    echo "      Python не найден. Поставь через https://www.python.org/downloads/ или:"
    echo "      xcode-select --install && brew install python"
    exit 1
fi
python3 --version

echo "[4/6] Создаю окружение и ставлю зависимости (5-10 минут)"
cd "$DST"
python3 -m venv .venv
.venv/bin/python3 -m pip install --quiet --upgrade pip
.venv/bin/python3 -m pip install --quiet -r requirements.txt

echo "[5/6] Права запуска"
chmod +x start-voice.command say.sh 2>/dev/null || true

echo "[6/6] Автозапуск (Launch Agent)"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$HOME/Library/LaunchAgents/com.zcode.voice.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.zcode.voice</string>
  <key>ProgramArguments</key><array>
    <string>$DST/.venv/bin/python3</string>
    <string>$DST/voice.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/zcode-voice.out</string>
  <key>StandardErrorPath</key><string>/tmp/zcode-voice.err</string>
</dict></plist>
PLIST
launchctl unload "$HOME/Library/LaunchAgents/com.zcode.voice.plist" 2>/dev/null || true
launchctl load "$HOME/Library/LaunchAgents/com.zcode.voice.plist"

echo ""
echo "ГОТОВО! Первый запуск скачает модель whisper-medium (~1.5 ГБ)."
echo "ОБЯЗАТЕЛЬНО дай разрешения (Системные настройки → Конфиденциальность):"
echo "  • Микрофон — для Терминала/python"
echo "  • Универсальный доступ + Мониторинг ввода — для хоткея и вставки"
echo "  • Далее: Системные настройки → Клавиатура → Диктовка (если нужна)"
echo "Проверка: скажи «голова робота, привет» — в окне ZCode появится сообщение."
echo "Лог: ~/voice-zcode/voice.log"
