#!/usr/bin/env python3
"""Озвучить текст: edge-tts (нейроголос Dmitry) с фолбэком на системный Irina.

Использование:
    python speak.py "текст"          — текст аргументом
    echo текст | python speak.py -   — текст со stdin (UTF-8)
    python speak.py file.txt         — текст из файла (UTF-8)
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

VOICE = "ru-RU-SvetlanaNeural"
CHUNK = 200  # edge-tts не любит длинные запросы — режем


def read_text() -> str:
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "-":
            try:
                sys.stdin.reconfigure(encoding="utf-8")
            except Exception:
                pass
            return sys.stdin.read()
        p = Path(arg)
        if p.exists():
            return p.read_text(encoding="utf-8")
        return " ".join(sys.argv[1:])
    return ""


def edge_speak(text: str) -> bool:
    import edge_tts
    import miniaudio
    import numpy as np
    import sounddevice as sd

    async def gen(path: str) -> None:
        await edge_tts.Communicate(text, VOICE).save(path)

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        asyncio.run(gen(path))
        decoded = miniaudio.decode_file(path)
        os.unlink(path)
        pcm = np.frombuffer(decoded.samples, dtype=np.int16)
        pcm = pcm.reshape(-1, decoded.nchannels)
        sd.play(pcm, decoded.sample_rate)
        sd.wait()
        return True
    except Exception:
        return False


def sapi_speak(text: str) -> None:
    try:
        if sys.platform == "darwin":
            import subprocess
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
        print(f"! озвучка не удалась: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    text = read_text().strip()
    if not text:
        print("нечего озвучивать", file=sys.stderr)
        sys.exit(1)
    for i in range(0, len(text), CHUNK):
        piece = text[i:i + CHUNK]
        if not edge_speak(piece):
            sapi_speak(text[i:])  # sapi сразу всё, без кусков
            return


if __name__ == "__main__":
    main()
