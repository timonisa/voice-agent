#!/usr/bin/env python3
"""
Голосовой интерфейс для ZCode — Windows И macOS. Работает из трея.

Логика:
  • В фоне слушает микрофон лёгким распознавателем (vosk) и ждёт фразу
    «рабочая голова робота» (плюс старое слово «зета»).
  • Услышав — пищит и пишет команду до паузы в речи (~2.2 с тишины).
  • Команду распознаёт whisper (локально) и дальше по ситуации:
      — приложение ZCode доступно → текст вставляется в поле ввода текущего
        диалога и отправляется (единый диалог: текстом и голосом);
      — ZCode не запущен        → общий голосовой агент (headless), ответ
                                  озвучивается.
  • Голосовые команды демона (в чат не идут):
      «стоп / замолчи / хватит»  — мгновенно глушит любую озвучку;
      «новое окно [, текст]»     — новый диалог (New task, Ctrl/Cmd+N).
  • Хоткей: Windows Ctrl+Alt+Пробел, macOS Cmd+Option+Пробел (зажать —
    говорить — отпустить).

Запуск: ярлык «Голосовой ZCode» / start-voice.cmd / start-voice.command
Лог: %USERPROFILE%/voice-zcode/voice.log (или ~/voice-zcode/voice.log)
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

BASE = Path.home() / "voice-zcode"
WORKSPACE = BASE / "agent-workspace"
SETTINGS_FILE = BASE / "settings.json"
MODEL_DIR = BASE / "models"
VOSK_MODEL_DIR = BASE / "vosk-model-small-ru-0.22"
LOG_FILE = BASE / "voice.log"

if IS_WIN:
    ZCODE_BIN_CANDIDATES = [r"C:\Program Files\ZCode\ZCode.exe"]
    ZCODE_CJS_CANDIDATES = [r"C:\Program Files\ZCode\resources\glm\zcode.cjs"]
    DEFAULT_HOTKEY = ["ctrl", "alt", "space"]
else:
    ZCODE_BIN_CANDIDATES = [
        "/Applications/ZCode.app/Contents/MacOS/ZCode",
        "/Applications/ZCode.app/Contents/Frameworks/ZCode Helper (GPU).app/Contents/MacOS/ZCode Helper (GPU)",  # на случай смены имени
    ]
    ZCODE_CJS_CANDIDATES = [
        "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",
        "/Applications/ZCode.app/Contents/Resources/resources/glm/zcode.cjs",
    ]
    DEFAULT_HOTKEY = ["cmd", "option", "space"]


def _first_existing(paths: list[str]) -> str:
    for p in paths:
        if Path(p).exists():
            return p
    return ""


ZCODE_BIN = _first_existing(ZCODE_BIN_CANDIDATES)
ZCODE_CJS = _first_existing(ZCODE_CJS_CANDIDATES)

DESKTOP_CFG = Path.home() / ".zcode" / "v2" / "config.json"
CLI_CFG = Path.home() / ".zcode" / "cli" / "config.json"

DEFAULT_SETTINGS = {
    "wake_word": "рабочая голова робота",
    "wake_enabled": True,          # постоянное прослушивание ключевой фразы
    "wake_verify": True,           # перепроверка сомнительных фраз whisper-ом
    "wake_whisper": "small",
    "hotkey": DEFAULT_HOTKEY,
    "hotkey_enabled": True,        # дублёр: зажал — говоришь, отпустил — ушла
    "mic_name": None,
    "whisper_size": "medium",
    "tts_voice": "ru-RU-SvetlanaNeural",
    "agent_model": None,
    "max_speak_chars": 1200,
    "agent_timeout_sec": 300,
    "sample_rate": 16000,
    "paste_submit": True,          # в ZCode: вставить и нажать Enter
    "interrupt_before_send": True, # Esc перед вставкой — чтобы команда
                                   # доходила сразу, даже если агент печает
    "endpoint_silence_ms": 1500,   # конец команды: столько тишины после речи
    "endpoint_max_sec": 30,        # …или жесткий лимит на длину команды
    "cmd_grace_ms": 350,           # после старта записи столько НЕ считаем речью
    "min_cmd_sec": 1.0,            # короче — считаем пустышкой
    "rms_threshold": 0.060,        # стартовый порог «это речь» (дальше адаптивный)
    "stop_command_enabled": True,
    "new_dialog_command_enabled": True,
}

# ---------------------------------------------------------------- утилиты

def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_settings() -> dict:
    merged = dict(DEFAULT_SETTINGS)
    merged["hotkey"] = list(DEFAULT_HOTKEY)
    if SETTINGS_FILE.exists():
        try:
            merged.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
        except Exception as e:
            log(f"! settings.json не читается ({e}), беру значения по умолчанию")
    if IS_MAC:  # привычные виндовые имена клавиш переводим в маковые
        merged["hotkey"] = [{"ctrl": "ctrl", "alt": "option", "windows": "cmd"}
                            .get(k, k) for k in merged["hotkey"]]
    return merged


def save_default_settings() -> None:
    if not SETTINGS_FILE.exists():
        SETTINGS_FILE.write_text(
            json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2), "utf-8")


def refresh_agent_config(settings: dict) -> None:
    """Пересобирает ~/.zcode/cli/config.json из конфига десктоп-приложения."""
    src = json.loads(DESKTOP_CFG.read_text("utf-8"))
    providers = src.get("provider", {})
    entry = providers.get("builtin:zai-coding-plan")
    if entry is None:
        for v in providers.values():
            if v.get("enabled"):
                entry = v
                break
    if entry is None or not entry.get("options", {}).get("apiKey"):
        raise RuntimeError(
            "В конфиге десктопа нет включённого провайдера с API-ключом. "
            "Залогиньтесь в приложении ZCode и перезапустите демона.")
    opts = entry["options"]
    model = settings.get("agent_model")
    if not model:
        models = list(entry.get("models", {}).keys())
        model = models[0] if models else "GLM-5.3"
    cfg = {
        "model": f"builtin:zai-coding-plan/{model}",
        "provider": {
            "builtin:zai-coding-plan": {
                "kind": "anthropic",
                "options": {"apiKey": opts["apiKey"],
                            "baseURL": opts.get("baseURL", "https://api.z.ai/api/anthropic")},
            }
        },
    }
    new = json.dumps(cfg, ensure_ascii=False, indent=2)
    old = CLI_CFG.read_text("utf-8") if CLI_CFG.exists() else ""
    if new != old:
        CLI_CFG.write_text(new, "utf-8")
        log(f"Конфиг агента обновлён (модель {model}).")

# ---------------------------------------------------------------- звук

def make_beeps(sr: int) -> dict:
    def tone(freq: float, ms: int) -> np.ndarray:
        t = np.linspace(0, ms / 1000, int(sr * ms / 1000), False)
        return (np.sin(freq * 2 * np.pi * t) * 0.25 * 32767).astype(np.int16)

    return {"wake": tone(1046, 80), "stop": tone(1568, 80),
            "error": tone(196, 250)}


def play_np(data: np.ndarray, sr: int) -> None:
    try:
        sd.play(data, sr)
    except Exception:
        pass

# ---------------------------------------------------------------- whisper

class Transcriber:
    def __init__(self, size: str):
        from faster_whisper import WhisperModel
        log(f"Загружаю модель whisper-{size}…")
        t0 = time.time()
        self.model = WhisperModel(size, device="cpu", compute_type="int8",
                                  download_root=str(MODEL_DIR))
        log(f"Модель готова за {time.time() - t0:.1f} с.")

    def transcribe(self, audio: np.ndarray, sr: int) -> str:
        pcm = audio.astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            pcm, language="ru", beam_size=1, vad_filter=True,
            condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()

# ---------------------------------------------------------------- агент

def ask_agent(text: str, settings: dict, fresh: bool = False) -> str:
    if not (ZCODE_BIN and ZCODE_CJS):
        return ("Не нашёл приложение ZCode на этой машине, "
                "общий диалог недоступен.")
    env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
    base_cmd = [ZCODE_BIN, ZCODE_CJS, "--prompt", text, "--mode", "yolo",
                "--cwd", str(WORKSPACE)]
    variants = [base_cmd] if fresh else [base_cmd + ["--continue"], base_cmd]
    for i, cmd in enumerate(variants):
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env,
                timeout=settings["agent_timeout_sec"], cwd=str(WORKSPACE))
        except subprocess.TimeoutExpired:
            return "Агент не ответил вовремя, попробуй ещё раз."
        answer = (r.stdout or "").strip()
        if answer:
            return answer
        if i + 1 < len(variants):
            continue
        err = (r.stderr or "").strip().splitlines()
        return "Ошибка агента: " + (err[-1] if err else f"код {r.returncode}")
    return ""

# ---------------------------------------------------------------- озвучка

class Speaker:
    """edge-tts (нейроголос) с фолбэком: Windows → SAPI Irina, macOS → say."""

    def __init__(self, voice: str, stop_event: threading.Event,
                 echo_guard: dict | None = None):
        self.voice = voice
        self.stop_event = stop_event
        self.echo_guard = echo_guard or {"until": 0.0}
        self.speaking = threading.Event()
        self._lock = threading.Lock()

    def speak(self, text: str) -> None:
        limit = 200
        pieces = [text[i:i + limit] for i in range(0, len(text), limit)] or [""]
        with self._lock:
            self.speaking.set()
            try:
                for piece in pieces:
                    if self.stop_event.is_set():
                        return
                    if not self._try_edge(piece):
                        self._speak_fallback(piece)
            finally:
                self.echo_guard["until"] = time.time() + 1.2
                self.speaking.clear()

    def _try_edge(self, text: str) -> bool:
        for attempt in range(2):  # у edge-tts бывают случайные 403
            try:
                return self._edge_attempt(text)
            except Exception as e:
                if attempt == 1:
                    log(f"edge-tts недоступен ({type(e).__name__}: {e}), озвучиваю системным голосом")
        return False

    def _edge_attempt(self, text: str) -> bool:
        import edge_tts
        import miniaudio

        async def gen(path: str) -> None:
            await edge_tts.Communicate(text, self.voice).save(path)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        asyncio.run(gen(path))
        if self.stop_event.is_set():
            os.unlink(path)
            return True

        decoded = miniaudio.decode_file(path)
        os.unlink(path)
        if self.stop_event.is_set():
            return True
        pcm = np.frombuffer(decoded.samples, dtype=np.int16)
        pcm = pcm.reshape(-1, decoded.nchannels)
        sd.play(pcm, decoded.sample_rate)
        while True:
            time.sleep(0.05)
            if self.stop_event.is_set():
                sd.stop()
                return True
            stream = sd.get_stream()
            if stream is None or not stream.active:
                return True

    def _speak_fallback(self, text: str) -> None:
        try:
            if IS_MAC:
                subprocess.run(["say", "-r", "185", text], check=False)
            else:
                import pyttsx3
                eng = pyttsx3.init()
                for v in eng.getProperty("voices"):
                    if "ru" in (v.id + v.name).lower():
                        eng.setProperty("voice", v.id)
                        break
                eng.say(text)
                eng.runAndWait()
        except Exception as e:
            log(f"! Озвучка не удалась: {e}")

class VoiceCues:
    """Короткие голосовые реплики вместо бипов (edge-tts, кэш в памяти)."""

    PHRASES = {"wake": "Да?", "accept": "Поняла",
               "error": "Упси! Не удалось!"}

    def __init__(self):
        self.cues: dict = {}

    def generate(self, voice: str) -> bool:
        import asyncio
        import edge_tts
        import miniaudio

        cache = BASE / "cues"
        try:
            cache.mkdir(exist_ok=True)
        except OSError:
            pass

        async def gen_missing():
            async def one(key, text):
                # кэш на диске: сеть нужна только при первой генерации
                path = cache / f"{key}.mp3"
                if not path.exists():
                    tmp = path.with_suffix(".tmp.mp3")
                    await edge_tts.Communicate(text, voice).save(str(tmp))
                    tmp.replace(path)
                d = miniaudio.decode_file(str(path))
                # .copy(): frombuffer держит ссылку на память декодера,
                # которая освобождается после выхода из one() — сегфолт
                pcm = np.frombuffer(d.samples, dtype=np.int16).copy()
                if d.nchannels > 1:   # иначе стерео играет как моно-каша и
                    pcm = pcm.reshape(-1, d.nchannels)   # голос «падает»
                self.cues[key] = (pcm, d.sample_rate)
            await asyncio.gather(*(one(k, s) for k, s in self.PHRASES.items()
                                   if k not in self.cues))

        for attempt in range(3):   # edge-tts иногда отдаёт 403
            try:
                asyncio.run(gen_missing())
                return True
            except Exception as e:
                if attempt == 2:
                    log(f"! Реплики не сгенерированы ({type(e).__name__}: {e}), "
                        "остаются бипы")
                else:
                    time.sleep(2)
        return False

    def play(self, key: str, wait: bool = True) -> bool:
        cue = self.cues.get(key)
        if cue is None:
            return False
        pcm, sr = cue
        sd.play(pcm, sr)
        if wait:
            sd.wait()
        return True


# ------------------------------------------------- окно/ввод: платформы

if IS_WIN:
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    def _exe_of_hwnd(hwnd: int) -> str:
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wt.DWORD(512)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return Path(buf.value).name.lower()
        finally:
            kernel32.CloseHandle(h)
        return ""

    def find_zcode_hwnd() -> int:
        fg = user32.GetForegroundWindow()
        if fg and _exe_of_hwnd(fg) == "zcode.exe":
            return fg
        candidates: list[tuple[int, int, str]] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def enum_cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) <= 0:
                return True
            if user32.GetWindowLongW(hwnd, -20) & 0x00000080:
                return True
            if _exe_of_hwnd(hwnd) == "zcode.exe":
                r = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                area = max(0, (r.right - r.left)) * max(0, (r.bottom - r.top))
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                candidates.append((area, hwnd, buf.value))
            return True

        user32.EnumWindows(enum_cb, 0)
        if not candidates:
            return 0
        candidates.sort(reverse=True)
        log(f"окна ZCode: {[(c[2][:30], c[0]) for c in candidates[:3]]}")
        return candidates[0][1]

    def focus_window(hwnd: int) -> bool:
        """Выводим окно на передний план (обход запрета красть фокус)."""
        if user32.GetForegroundWindow() == hwnd:
            return True
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.35)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.25)
        if user32.GetForegroundWindow() == hwnd:
            return True
        user32.keybd_event(0x12, 0, 0, 0)      # Alt-«ключ» к захвату фокуса
        user32.keybd_event(0x12, 0, 2, 0)
        time.sleep(0.08)
        user32.SetForegroundWindow(hwnd)
        for _ in range(10):
            time.sleep(0.1)
            if user32.GetForegroundWindow() == hwnd:
                return True
        return False

    def keybd(vk: int, up: bool = False) -> None:
        user32.keybd_event(vk, 0, 2 if up else 0, None)

    def short_path(p: Path) -> str:
        """8.3-путь без кириллицы — vosk не открывает модели из путей с не-ASCII."""
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.GetShortPathNameW(str(p), buf, 1024):
            return buf.value
        return str(p)

    def focus_text_input(hwnd: int) -> bool:
        """Фокус на поле ввода внутри окна (UIA, фолбэк — клик в нижнюю зону)."""
        try:
            import uiautomation as auto
            auto.uiautomation.SetGlobalSearchTimeout(3)
            for attempt in range(2):
                win = auto.ControlFromHandle(hwnd)
                if win is None:
                    break
                for ctl in (win.DocumentControl(searchDepth=10),
                            win.EditControl(searchDepth=10)):
                    if ctl is not None:
                        try:
                            ctl.SetFocus()
                            time.sleep(0.15)
                            focused = auto.GetFocusedControl()
                            if focused is None or focused.ControlType in (
                                    auto.ControlType.EditControl,
                                    auto.ControlType.DocumentControl):
                                log(f"✓ фокус на поле ввода (UIA, {ctl.ControlTypeName})")
                                return True
                        except Exception:
                            pass
                if attempt == 0:
                    time.sleep(0.8)
        except ImportError:
            pass
        except Exception as e:
            log(f"! UIA не сработал: {e}")
        try:
            r = wt.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            if w > 40 and h > 120:
                pt = wt.POINT(w // 2, h - 55)
                user32.ClientToScreen(hwnd, ctypes.byref(pt))
                user32.SetCursorPos(pt.x, pt.y)
                user32.mouse_event(2, 0, 0, 0, 0)
                user32.mouse_event(4, 0, 0, 0, 0)
                time.sleep(0.2)
                log("✓ фокус на поле ввода (клик в нижнюю зону)")
                return True
        except Exception as e:
            log(f"! клик-фолбэк не сработал: {e}")
        return False

    def paste_and_submit(text: str, submit: bool, hwnd: int = 0,
                         interrupt: bool = False) -> bool:
        import win32clipboard
        if hwnd and not focus_window(hwnd):
            return False
        if hwnd:
            focus_text_input(hwnd)
        if interrupt:
            # прерываем текущую генерацию, иначе Enter при занятом агенте
            # не отправляет команду
            keybd(0x1B)                        # Esc
            time.sleep(0.05)
            keybd(0x1B, True)
            time.sleep(0.7)
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        time.sleep(0.2)
        keybd(0x11); time.sleep(0.03); keybd(0x56)   # Ctrl+V
        time.sleep(0.12)
        keybd(0x56, True); time.sleep(0.03); keybd(0x11, True)
        if submit:
            time.sleep(0.25)
            keybd(0x0D); time.sleep(0.05); keybd(0x0D, True)   # Enter
        return True

    def send_new_dialog_shortcut() -> None:
        keybd(0x11); time.sleep(0.03); keybd(0x4E)   # Ctrl+N
        time.sleep(0.1)
        keybd(0x4E, True); time.sleep(0.03); keybd(0x11, True)

    def kill_speech_processes() -> None:
        """Гасим озвучку speak.py/say.cmd (для команды «стоп»)."""
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { "
             "($_.Name -like 'python*' -or $_.Name -eq 'powershell.exe') -and "
             "($_.CommandLine -match 'speak\\.py' -or $_.CommandLine -match 'System\\.Speech') "
             "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
            capture_output=True, timeout=15)

else:
    # ------------------------- macOS

    def _osascript(script: str) -> bool:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=15)
        return r.returncode == 0

    def find_zcode_hwnd() -> int:
        """На маке «дескриптор» — 1, если приложение установлено и запущено."""
        if not ZCODE_BIN:
            return 0
        r = subprocess.run(["osascript", "-e",
                            'tell application "System Events" to '
                            '(exists (processes where name is "ZCode"))'],
                           capture_output=True, text=True, timeout=10)
        return 1 if "true" in (r.stdout or "") else 0

    def focus_window(hwnd: int) -> bool:
        ok = _osascript('tell application "ZCode" to activate')
        time.sleep(0.5)
        return ok

    def keybd(vk: int, up: bool = False) -> None:  # совместимость сигнатуры
        pass

    def short_path(p: Path) -> str:
        return str(p)

    def focus_text_input(hwnd: int) -> bool:
        # Chromium не отдаёт дерево без скринридера; на маке полагаемся на то,
        # что ZCode держит фокус на поле ввода, либо кликаем нижнюю зону
        # через cliclick, если он установлен (brew install cliclick)
        if shutil.which("cliclick"):
            r = subprocess.run(["osascript", "-e",
                                'tell application "System Events" to tell process "ZCode" '
                                'to get {position, size} of window 1'],
                               capture_output=True, text=True, timeout=10)
            m = re.findall(r"-?\d+", r.stdout or "")
            if len(m) >= 4:
                x, y, w, h = map(int, m[:4])
                subprocess.run(["cliclick", f"c:{x + w // 2},{y + h - 55}"],
                               capture_output=True, timeout=10)
                time.sleep(0.25)
                return True
        return True  # не мешаем вставке: просто продолжаем

    def paste_and_submit(text: str, submit: bool, hwnd: int = 0,
                         interrupt: bool = False) -> bool:
        if hwnd and not focus_window(hwnd):
            return False
        # клик по полю ввода: после активации окна из фона фокус может
        # не встать в поле сам — тогда вставка уходит в никуда
        focus_text_input(hwnd)
        if interrupt:
            # key code 53 = Esc: прерываем текущую генерацию, иначе Enter
            # при занятом агенте не отправляет команду
            _osascript('tell application "System Events" to key code 53')
            time.sleep(0.7)
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        time.sleep(0.25)
        # key code 9 = V: keystroke "v" при русской раскладке не находит
        # латинскую v и даёт keycode 0 (клавиша A) — выходит «выделить всё»
        _osascript('tell application "System Events" to '
                   "key code 9 using command down")
        if submit:
            time.sleep(0.3)
            _osascript('tell application "System Events" to key code 36')
        return True

    def send_new_dialog_shortcut() -> None:
        _osascript('tell application "System Events" to '
                   "key code 45 using command down")

    def kill_speech_processes() -> None:
        subprocess.run(["pkill", "-f", "speak.py"], capture_output=True)
        subprocess.run(["pkill", "-f", "say -r"], capture_output=True)

# ---------------------------------------------------------------- приложение

class VoiceApp:
    """Один бесконечный поток чтения микрофона с конечным автоматом:
    WAKE (ждём «голова робота») → CMD (пишем команду до тишины) → обработка."""

    def __init__(self, settings: dict):
        self.s = settings
        self.sr = settings["sample_rate"]
        self.beeps = make_beeps(self.sr)
        self.stop_speaking = threading.Event()
        self.echo_guard = {"until": 0.0}  # до этого времени wake не срабатывает
        self._speech_rms_gate = settings.get("rms_threshold", 0.01)
        self._cmd_gate = self._speech_rms_gate   # порог записи команды
        self.transcriber = Transcriber(settings["whisper_size"])
        self.speaker = Speaker(settings["tts_voice"], self.stop_speaking,
                               self.echo_guard)
        self.cues = VoiceCues()
        log("Генерирую голосовые реплики…")
        if not self.cues.generate(settings["tts_voice"]):
            self.cues = None
        self.audio_q: "queue.Queue[bytes]" = queue.Queue()
        self.reset_dialog = False   # следующий headless-запрос без --continue
        self.busy = False
        self.manual_trigger = threading.Event()
        self.hold_active = threading.Event()    # «кнопка зажата»
        self.hold_release = threading.Event()   # «кнопку отпустили — конец записи»
        self.utt_buf: list[bytes] = []    # текущая фраза для whisper-перепроверки
        self.utt_last = 0.0
        self._wv_model = None             # whisper-small (ленивая загрузка)
        self._WAKE_RE = self._make_wake_re(settings.get("wake_word", "голова робота"))
        # скелеты основ слов фразы: ловят проглоченные гласные
        # («глава» = «голова») и лишние согласные в окончаниях
        vow = r"[аеёиоуыэюяьъй]"
        stems = []
        for tok in re.split(r"\s+", settings.get("wake_word", "").strip().lower()):
            if not tok:
                continue
            stem = (tok[:-3] if len(tok) > 5 else
                    tok[:-2] if len(tok) > 4 else
                    tok[:-1] if len(tok) > 2 else tok)
            stems.append(re.sub(vow, "", stem))
        self._WAKE_STEM_SKELS = [s for s in stems if s]
        self.wake_enabled = threading.Event()
        if settings["wake_enabled"]:
            self.wake_enabled.set()
        self._wake_recognizer = self._make_wake_recognizer()

    # ---- vosk wake-детектор + whisper-перепроверка
    def _make_wake_recognizer(self):
        try:
            from vosk import KaldiRecognizer, Model
            if not VOSK_MODEL_DIR.exists():
                log(f"! Модель vosk не найдена ({VOSK_MODEL_DIR}), wake-слово выключено")
                return None
            model = Model(short_path(VOSK_MODEL_DIR))
            return KaldiRecognizer(model, self.sr)
        except Exception as e:
            log(f"! vosk недоступен ({e}), wake-слово выключено")
            return None

    # надёжные варианты написания (быстрый путь, без перепроверки):
    # старое «зета» оставляем + строим регулярку из текущей фразы
    @staticmethod
    def _make_wake_re(phrase: str) -> "re.Pattern":
        parts = [r"з[еиэ][тткг][ао]"]  # зета/зита/зэта/зека…
        tokens = [t for t in re.split(r"\s+", phrase.strip().lower()) if t]
        stems = []
        # короткие основы («рабо», «голо», «робо») — чтобы ловить косое
        # произношение («работая голову роботов») и заскоки распознавателя
        for t in tokens:
            if len(t) > 5:
                stems.append(t[:-3])
            elif len(t) > 4:
                stems.append(t[:-2])
            elif len(t) > 2:
                stems.append(t[:-1])
            else:
                stems.append(t)
        if len(stems) >= 2:
            parts.append(r"\b" + r"\W{1,3}".join(s + r"\w{0,4}" for s in stems) + r"\b")
        elif stems:
            parts.append(r"\b" + stems[0] + r"\w{0,4}\b")
        return re.compile("|".join(parts), re.IGNORECASE)

    def _wake_hit(self, text: str) -> bool:
        if self._WAKE_RE.search(text):
            return True
        skels = [re.sub(r"[аеёиоуыэюяьъй]", "", w)
                 for w in text.lower().split()]
        # основы фразы должны встретиться в скелетах слов ПО ПОРЯДКУ
        i = 0
        for tgt in self._WAKE_STEM_SKELS:
            while i < len(skels) and tgt not in skels[i]:
                i += 1
            if i == len(skels):
                return False
            i += 1
        return bool(self._WAKE_STEM_SKELS)

    def _wake_verify_model(self):
        if self._wv_model is None:
            from faster_whisper import WhisperModel
            self._wv_model = WhisperModel(self.s.get("wake_whisper", "small"),
                                          device="cpu", compute_type="int8",
                                          download_root=str(MODEL_DIR))
        return self._wv_model

    def _verify_wake(self, utt_buf: list[bytes]) -> bool:
        """Перепроверяем фразу whisper-ом: vosk часто пишет «голова робота»
        криво («глава роботов» и т.п.), а whisper понимает правильно."""
        try:
            audio = np.frombuffer(b"".join(utt_buf), dtype=np.int16)
            if len(audio) < self.sr * 0.4:
                return False
            model = self._wake_verify_model()
            segments, _ = model.transcribe(audio.astype(np.float32) / 32768.0,
                                           language="ru", beam_size=1,
                                           condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segments).strip()
            if text:
                log(f"· перепроверка: «{text}»")
            hit = self._wake_hit(text)
            if hit:
                log("✓ whisper подтвердил ключевую фразу")
            return hit
        except Exception as e:
            log(f"! Перепроверка wake не удалась: {e}")
            return False

    def _check_wake(self, chunk: bytes, rms: float, now: float) -> bool:
        rec = self._wake_recognizer
        if rec is None:
            return False
        # копим текущую фразу целиком (от первой речи до тишины)
        if rms >= self._speech_rms_gate:
            self.utt_buf.append(chunk)
            self.utt_last = now
        elif self.utt_buf and now - self.utt_last < 0.6:
            self.utt_buf.append(chunk)   # хвост после речи
        if rec.AcceptWaveform(chunk):
            text = json.loads(rec.Result()).get("text", "")
            rec.Reset()
            buf, self.utt_buf = self.utt_buf, []
            if not text:
                return False
            log(f"· услышано: «{text}»")
            if self._wake_hit(text):
                return True
            if len(text.split()) <= 7 and self.s.get("wake_verify", True):
                return self._verify_wake(buf)
            return False
        return False

    def _reset_wake(self) -> None:
        if self._wake_recognizer is not None:
            try:
                self._wake_recognizer.Reset()
            except Exception:
                pass

    # ---- реплики вместо бипов: playback + сброс «хвостов» микрофона
    def _drain_audio(self) -> None:
        try:
            while True:
                self.audio_q.get_nowait()
        except queue.Empty:
            pass

    def _cue(self, key: str, beep_key: str,
             wait: bool = True, drain: bool = True) -> None:
        if not (self.cues and self.cues.play(key, wait=wait)):
            play_np(self.beeps[beep_key], self.sr)
        if drain:
            # сливаем очередь только для реплик ПОСЛЕ записи команды;
            # для «Да?» сливать нельзя — там уже идёт речь пользователя
            self._drain_audio()
        self.echo_guard["until"] = time.time() + 1.2

    # ---- основной цикл аудио
    def run_audio_loop(self) -> None:
        self._open_input()
        state = "WAKE"
        cmd_audio: list[bytes] = []
        speech_started = False
        last_speech = 0.0
        cmd_started = 0.0
        over_cnt = 0                 # подряд идущие чанки над порогом речи
        noise_ema = self.s["rms_threshold"]   # адаптивный уровень шума
        cmd_dbg_max = 0.0            # максимум rms между записями в лог (CMD)
        cmd_dbg_t = 0.0
        rms_hist: list = []          # громкость последних ~3 с (для cmd_gate)
        echo_guard = self.echo_guard          # общий с Speaker: не будим себя

        log("Слушаю микрофон…")
        while True:
            try:
                chunk = self.audio_q.get(timeout=0.2)
            except queue.Empty:
                chunk = None

            # пока агент говорит в динамики — не слушаем (иначе сам себя разбудит)
            if self.speaker.speaking.is_set():
                state, cmd_audio, speech_started = "WAKE", [], False
                continue
            if self.manual_trigger.is_set() and state == "WAKE":
                self.manual_trigger.clear()
                self.hold_active.set()          # это может быть hold-to-talk
                self._cue("wake", "wake", wait=False, drain=False)
                wpeak = max(rms_hist[-180:] or [0.03])
                self._cmd_gate = max(0.35 * wpeak, 0.012)
                state, cmd_audio = "CMD", []
                speech_started, last_speech = False, time.time()
                cmd_started = time.time()
                over_cnt = 0
                continue
            if state == "CMD" and self.hold_release.is_set():
                # отпустили хоткей — заканчиваем запись немедленно
                self.hold_release.clear()
                self.hold_active.clear()
                if len(cmd_audio) >= 3:
                    self._dispatch(cmd_audio)
                    self._reset_wake()
                state, cmd_audio, speech_started = "WAKE", [], False
                continue
            if state == "CMD":
                self.hold_release.clear()   # старые флаги не должны стрелять сразу
            if chunk is None:
                if state == "CMD" and speech_started and \
                        (time.time() - last_speech) * 1000 >= self.s["endpoint_silence_ms"]:
                    self._dispatch(cmd_audio)
                    self._reset_wake()
                    self.hold_active.clear()
                    state, cmd_audio, speech_started = "WAKE", [], False
                continue

            rms = float(np.sqrt(np.mean((np.frombuffer(chunk, dtype=np.int16)
                                         .astype(np.float32) / 32768.0) ** 2)))
            now = time.time()

            if state == "WAKE":
                noise_ema = 0.98 * noise_ema + 0.02 * rms   # фон обновляем всегда
                # rms_threshold — настоящий нижний предел: иначе в тихой
                # комнате порог сползает до 0.01 и запись тянут шорохи
                self._speech_rms_gate = max(noise_ema * 2.5,
                                            self.s["rms_threshold"])
                rms_hist.append(rms)
                if len(rms_hist) > 180:   # ~3 с истории громкости
                    rms_hist.pop(0)
                if self.wake_enabled.is_set() and now >= echo_guard["until"] \
                        and self._check_wake(chunk, rms, now):
                    log(f"◆ «{self.s['wake_word']}» услышано")
                    wpeak = max(rms_hist[-180:] or [rms])
                    self._cmd_gate = max(0.35 * wpeak, 0.012)
                    self._reset_wake()
                    self.utt_buf = []
                    self._cue("wake", "wake", wait=False, drain=False)
                    state, cmd_audio = "CMD", []
                    speech_started, last_speech = False, now
                    cmd_started = now
                    over_cnt = 0
                continue

            # state == CMD: пишем команду.
            # Первые cmd_grace_ms не считаем речью и не пишем — там «пик»
            # и звук клавиш; речью считаем только 2 чанка подряд над порогом.
            if now - cmd_started >= self.s.get("cmd_grace_ms", 350) / 1000:
                cmd_audio.append(chunk)
                cmd_dbg_max = max(cmd_dbg_max, rms)
                if now - cmd_dbg_t >= 3.0:
                    cmd_dbg_t = now
                    log(f"· запись: rms={cmd_dbg_max:.4f} "
                        f"порог={self._cmd_gate:.4f}")
                    cmd_dbg_max = 0.0
                if rms >= self._cmd_gate:
                    over_cnt += 1
                    last_speech = now
                    if over_cnt >= 2:
                        speech_started = True
                else:
                    over_cnt = 0
            if not speech_started and now - cmd_started >= 4.0:
                log("после ключевой фразы тишина — отмена")
                self.hold_active.clear()
                state, cmd_audio, speech_started = "WAKE", [], False
            elif speech_started and (now - last_speech) * 1000 >= self.s["endpoint_silence_ms"]:
                self._dispatch(cmd_audio)
                self._reset_wake()
                self.hold_active.clear()
                state, cmd_audio, speech_started = "WAKE", [], False
            elif now - cmd_started >= self.s["endpoint_max_sec"]:
                self._dispatch(cmd_audio)
                self._reset_wake()
                self.hold_active.clear()
                state, cmd_audio, speech_started = "WAKE", [], False

    def _open_input(self) -> None:
        def callback(indata, frames, t, status):
            self.audio_q.put(bytes(indata))
        kwargs = {"samplerate": self.sr, "channels": 1, "dtype": "int16",
                  "callback": callback}
        if self.s.get("mic_name"):
            for i, d in enumerate(sd.list_devices()):
                if d["max_input_channels"] > 0 and \
                        self.s["mic_name"].lower() in d["name"].lower():
                    kwargs["device"] = i
                    break
            else:
                log(f"! Микрофон '{self.s['mic_name']}' не найден, беру системный")
        # держим ссылку: иначе GC собирает поток, PortAudio зовёт
        # освобождённый callback и процесс падает с SIGSEGV
        self.stream = sd.InputStream(**kwargs)
        self.stream.start()

    # ---- обработка команды
    # «голова робота стоп» и родственные: мгновенно глушим любую озвучку,
    # в чат команда не уходит
    _STOP_RE = re.compile(
        r"^\s*(?:стоп|замолчи|хватит|тише|останов[иы](?:\s+озвучку)?|"
        r"прекрат[иь](?:\s+озвучку)?)\s*[!.]?\s*$", re.IGNORECASE)

    # «новое окно»: новый диалог (New task) в приложении ZCode, Ctrl/Cmd+N
    _NEW_RE = re.compile(
        r"^\s*(?:новое\s+окно|новый\s+диалог|новая\s+сессия|новая\s+задача)"
        r"\b[\s,.!?–-]*", re.IGNORECASE)

    def _stop_all_playback(self) -> None:
        self.stop_speaking.set()
        try:
            sd.stop()
        except Exception:
            pass
        try:
            kill_speech_processes()
        except Exception as e:
            log(f"! остановка озвучки: {e}")
        self.stop_speaking.clear()

    def _dispatch(self, cmd_audio: list[bytes]) -> None:
        if not cmd_audio:
            return
        audio = np.frombuffer(b"".join(cmd_audio), dtype=np.int16)
        if len(audio) < self.sr * self.s.get("min_cmd_sec", 1.0):
            log("слишком коротко (пик/щелчок?) — пропускаю")
            return
        self.busy = True
        try:
            t0 = time.time()
            text = self.transcriber.transcribe(audio, self.sr)
            # срезаем слова-активаторы в начале, если попали в запись команды
            text = re.sub(
                r"^\s*(?:(?:зета|рабо\w*\s+голов\w*\s+робот\w*|"
                r"голов[ауы]\s+робот\w*|да|ага|угу)\b[\s,.!?–-]*)+",
                "", text, flags=re.IGNORECASE).strip()
            log(f"▸ «{text}» ({len(audio)/self.sr:.1f} с, распознавание {time.time()-t0:.1f} с)")
            if self.s.get("stop_command_enabled", True) and self._STOP_RE.match(text):
                log("⏹ «стоп»: глушу озвучку")
                self._stop_all_playback()
                return
            if self.s.get("new_dialog_command_enabled", True):
                m = self._NEW_RE.match(text)
                if m:
                    rest = text[m.end():].strip()
                    log("✦ «новое окно»: открываю новый диалог")
                    hwnd = find_zcode_hwnd()
                    if hwnd and focus_window(hwnd):
                        focus_text_input(hwnd)
                        send_new_dialog_shortcut()
                        time.sleep(1.6)     # даём новому диалогу открыться
                        if rest:
                            focus_text_input(hwnd)
                            ok = paste_and_submit(
                                rest, self.s.get("paste_submit", True), hwnd,
                                interrupt=self.s.get("interrupt_before_send", True))
                            log("✓ новый диалог, текст вставлен" if ok
                                else "! вставка в новый диалог не удалась")
                        else:
                            log("✓ новый диалог готов")
                        self._cue("accept", "stop")
                    else:
                        self.speaker.speak("Приложение зед код не запущено.")
                    return
            if not text:
                self.speaker.speak("Не расслышал.")
                return
            self._cue("accept", "stop")   # команда принята
            hwnd = find_zcode_hwnd()
            if hwnd:
                log("→ ZCode доступен: вставляю туда (единый диалог)")
                ok = paste_and_submit(
                    text, self.s.get("paste_submit", True), hwnd,
                    interrupt=self.s.get("interrupt_before_send", True))
                if not ok:
                    log("! не смог активировать окно ZCode")
                    self.speaker.speak("Не смог открыть окно зед код.")
                elif IS_WIN and user32.GetForegroundWindow() != hwnd:
                    log("! фокус ушёл с окна ZCode при вставке")
                else:
                    log("✓ вставлено")
            else:
                log("→ общий диалог (headless)")
                fresh, self.reset_dialog = self.reset_dialog, False
                log("… агент думает")
                answer = ask_agent(text, self.s, fresh=fresh)
                log(f"◂ {answer[:300]}{'…' if len(answer) > 300 else ''}")
                self.stop_speaking.clear()
                if len(answer) > self.s["max_speak_chars"]:
                    answer = answer[:self.s["max_speak_chars"]] + " … Подробности скину в файл."
                self.speaker.speak(answer)
        except Exception as e:
            log(f"! Ошибка обработки: {e}")
            self._cue("error", "error")
        finally:
            self.busy = False

    # ---- ручные триггеры (хоткей/трей)
    def trigger_command(self) -> None:
        if not self.busy:
            self.manual_trigger.set()

    def release_command(self) -> None:
        if self.hold_active.is_set():
            self.hold_release.set()

# ---------------------------------------------------------------- tray

def make_tray_icon() -> "object":
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(24, 34, 234, 255))
    d.ellipse((22, 14, 42, 38), fill=(255, 255, 255, 255))
    d.rounded_rectangle((27, 36, 37, 52), radius=5, fill=(255, 255, 255, 255))
    d.ellipse((18, 36, 46, 52), outline=(255, 255, 255, 255), width=3)
    return img


def main() -> None:
    save_default_settings()
    settings = load_settings()
    BASE.mkdir(exist_ok=True)
    WORKSPACE.mkdir(exist_ok=True)
    try:
        LOG_FILE.write_text("", encoding="utf-8")  # новый лог при старте
    except Exception:
        pass

    log(f"Платформа: {sys.platform} | ZCode: {ZCODE_BIN or 'НЕ НАЙДЕН'}")
    refresh_agent_config(settings)
    app = VoiceApp(settings)

    threading.Thread(target=app.run_audio_loop, daemon=True).start()

    # хоткей: зажал — говоришь, отпустил — отправилось
    if settings["hotkey_enabled"]:
        try:
            if IS_MAC:
                # библиотека keyboard на macOS не работает, нужен pynput
                from pynput import keyboard as pk

                # названия клавиш приводим к именам pynput (option → alt)
                target = {{"option": "alt", "windows": "cmd"}.get(k, k)
                          for k in settings["hotkey"]}

                def canon(key) -> str:
                    n = getattr(key, "name", "") or ""
                    for suf in ("_left", "_right", "_l", "_r"):
                        if n.endswith(suf):
                            n = n[:-len(suf)]
                    return {"option": "alt"}.get(n, n)

                held: set[str] = set()
                holding = False

                def on_press(key):
                    nonlocal holding
                    n = canon(key)
                    if n and n in target and n not in held:
                        held.add(n)
                        if target <= held and not holding:
                            holding = True
                            app.trigger_command()

                def on_release(key):
                    nonlocal holding
                    n = canon(key)
                    if n and n in held:
                        held.discard(n)
                    if holding and n in target:
                        holding = False
                        app.release_command()

                pk.Listener(on_press=on_press, on_release=on_release).start()
            else:
                import keyboard

                hotkey = set(settings["hotkey"])
                held: set[str] = set()
                holding = False

                def canon(name: str) -> str:
                    for prefix in ("left ", "right "):
                        if name.startswith(prefix):
                            return name[len(prefix):]
                    return name

                def on_event(event):
                    nonlocal holding
                    name = canon((event.name or "").lower())
                    if name not in hotkey:
                        return
                    if event.event_type == "down":
                        if name not in held:
                            held.add(name)
                            if hotkey <= held and not holding:
                                holding = True
                                app.trigger_command()
                    else:
                        held.discard(name)
                        if holding:
                            holding = False
                            app.release_command()

                keyboard.hook(on_event, suppress=False)
        except Exception as e:
            log(f"! Хоткей недоступен: {e}")

    # треи
    import pystray
    from pystray import Menu, MenuItem

    def on_listen(icon, item):
        app.trigger_command()

    def on_wake_toggle(icon, item):
        if app.wake_enabled.is_set():
            app.wake_enabled.clear()
            log("Wake-слово выключено")
        else:
            app.wake_enabled.set()
            log("Wake-слово включено")

    def on_new_dialog(icon, item):
        app.reset_dialog = True
        app.speaker.speak("Начинаю новый диалог.")
        log("Следующий запрос начнёт новый диалог")

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    menu = Menu(
        MenuItem("Слушать сейчас (говорите до паузы)", on_listen),
        MenuItem(f"Слушать «{settings['wake_word']}»", on_wake_toggle,
                 checked=lambda item: app.wake_enabled.is_set()),
        MenuItem("Новый диалог", on_new_dialog),
        Menu.SEPARATOR,
        MenuItem("Выход", on_quit),
    )
    icon = pystray.Icon("zcode-voice", make_tray_icon(),
                        f"Голосовой ZCode — «{settings['wake_word']}» или хоткей",
                        menu)
    try:
        mic = sd.query_devices(kind="input")
        log(f"Микрофон: {mic['name']}")
    except Exception:
        pass
    hk = "+".join(settings["hotkey"])
    log(f"Готов. Жду «{settings['wake_word']}», либо зажми {hk} и говори.")
    icon.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("ФАТАЛЬНО: " + traceback.format_exc())
        raise
