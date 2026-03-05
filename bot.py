#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    from aiy.board import Board, Led
except Exception:
    Board = None
    Led = None

try:
    from aiy.leds import Color, Leds
except Exception:
    Color = None
    Leds = None


if load_dotenv:
    load_dotenv()


_AUDIO_PLAYBACK_DISABLED = False
_AUDIO_WARNING_SHOWN = False


@dataclass
class Config:
    api_key: str
    base_url: str
    stt_model: str
    chat_model: str
    tts_model: str
    tts_voice: str
    record_seconds: int
    sample_rate: int
    record_format: str
    record_channels: int
    input_device: str
    output_device: str
    system_prompt: str
    max_turns: int


def env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def build_config() -> Config:
    return Config(
        api_key=env("OPENAI_API_KEY", ""),
        base_url=env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        stt_model=env("STT_MODEL", "gpt-4o-mini-transcribe"),
        chat_model=env("CHAT_MODEL", "gpt-4.1-mini"),
        tts_model=env("TTS_MODEL", "gpt-4o-mini-tts"),
        tts_voice=env("TTS_VOICE", "alloy"),
        record_seconds=int(env("RECORD_SECONDS", "5")),
        sample_rate=int(env("SAMPLE_RATE", "16000")),
        record_format=env("RECORD_FORMAT", "S16_LE"),
        record_channels=int(env("RECORD_CHANNELS", "1")),
        input_device=env("INPUT_DEVICE", ""),
        output_device=env("OUTPUT_DEVICE", ""),
        system_prompt=env(
            "SYSTEM_PROMPT",
            "You are AIYVoice, a concise, helpful home voice assistant.",
        ),
        max_turns=int(env("MAX_TURNS", "6")),
    )


def run_cmd(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return proc


def has_capture_device() -> bool:
    proc = run_cmd(["arecord", "-l"], check=False)
    return "card" in proc.stdout.lower()


def has_playback_device() -> bool:
    proc = run_cmd(["aplay", "-l"], check=False)
    return "card" in proc.stdout.lower()


def has_aiy_library() -> bool:
    return Board is not None and Led is not None


def _led_state(name: str):
    if Led is None:
        return None
    return getattr(Led, name, None)


def set_led(board, state, leds=None) -> None:
    if state is None:
        return

    # Match AIYVision behavior: white button LED on/off only in AIY mode.
    if leds is not None and Leds is not None and Color is not None:
        try:
            if state == _led_state("OFF"):
                leds.update(Leds.rgb_off())
            else:
                leds.update(Leds.rgb_on(Color.WHITE))
            return
        except Exception:
            pass

    if board is None:
        return
    try:
        board.led.state = state
    except Exception:
        pass


def add_history(history: List[Dict[str, str]], user_text: str, reply: str, max_turns: int) -> None:
    history.extend(
        [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply},
        ]
    )
    max_items = max_turns * 2
    if len(history) > max_items:
        del history[:-max_items]


def record_wav(path: Path, cfg: Config) -> None:
    cmd = [
        "arecord",
        "-q",
        "-f",
        str(cfg.record_format),
        "-c",
        str(cfg.record_channels),
        "-r",
        str(cfg.sample_rate),
        "-d",
        str(cfg.record_seconds),
    ]
    if cfg.input_device:
        cmd.extend(["-D", cfg.input_device])
    cmd.append(str(path))
    run_cmd(cmd)


def play_wav(path: Path, cfg: Config) -> None:
    global _AUDIO_PLAYBACK_DISABLED, _AUDIO_WARNING_SHOWN

    if _AUDIO_PLAYBACK_DISABLED:
        return

    if env("DISABLE_AUDIO_PLAYBACK", "0") == "1":
        _AUDIO_PLAYBACK_DISABLED = True
        if not _AUDIO_WARNING_SHOWN:
            print("Audio playback disabled via DISABLE_AUDIO_PLAYBACK=1")
            _AUDIO_WARNING_SHOWN = True
        return

    cmd = ["aplay", "-q"]
    if cfg.output_device:
        cmd.extend(["-D", cfg.output_device])
    cmd.append(str(path))

    proc = run_cmd(cmd, check=False)
    if proc.returncode == 0:
        return

    err = (proc.stderr or proc.stdout or "").strip()

    # Some Pi setups report a playback card but cannot open it (e.g. inactive HDMI).
    # Switch to silent null sink for this run so chat flow keeps working.
    if not cfg.output_device:
        null_proc = run_cmd(["aplay", "-D", "null", "-q", str(path)], check=False)
        if null_proc.returncode == 0:
            _AUDIO_PLAYBACK_DISABLED = True
            if not _AUDIO_WARNING_SHOWN:
                print(
                    "Warning: no usable audio output sink; running in silent mode. "
                    "Set OUTPUT_DEVICE once speaker output is available."
                )
                _AUDIO_WARNING_SHOWN = True
            return

    _AUDIO_PLAYBACK_DISABLED = True
    if not _AUDIO_WARNING_SHOWN:
        print(f"Warning: audio playback disabled for this session ({err})")
        _AUDIO_WARNING_SHOWN = True


def wav_signal_levels(path: Path):
    import struct
    import wave

    with wave.open(str(path), "rb") as w:
        n_frames = w.getnframes()
        sample_width = w.getsampwidth()
        data = w.readframes(n_frames)

    if not data:
        return 0.0, 0

    if sample_width == 2:
        vals = struct.unpack(f"<{len(data) // 2}h", data)
    elif sample_width == 4:
        vals = struct.unpack(f"<{len(data) // 4}i", data)
    else:
        return 0.0, 0

    peak = max(abs(v) for v in vals) if vals else 0
    rms = (sum(v * v for v in vals) / len(vals)) ** 0.5 if vals else 0.0
    return rms, peak


def transcribe_audio(path: Path, cfg: Config) -> str:
    cmd = [
        "curl",
        "-sS",
        "-X",
        "POST",
        f"{cfg.base_url}/audio/transcriptions",
        "-H",
        f"Authorization: Bearer {cfg.api_key}",
        "-F",
        f"file=@{path}",
        "-F",
        f"model={cfg.stt_model}",
        "-F",
        "response_format=json",
    ]
    proc = run_cmd(cmd)
    data = json.loads(proc.stdout)
    if "error" in data:
        raise RuntimeError(f"STT error: {data['error']}")
    text = data.get("text", "").strip()
    return text


def post_json(url: str, payload: Dict, api_key: str, timeout: int = 120) -> Dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {details}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error calling {url}: {e}") from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON from {url}: {raw[:500]}") from e


def chat_reply(user_text: str, history: List[Dict[str, str]], cfg: Config) -> str:
    messages = [{"role": "system", "content": cfg.system_prompt}] + history + [
        {"role": "user", "content": user_text}
    ]
    payload = {
        "model": cfg.chat_model,
        "messages": messages,
        "temperature": 0.6,
    }
    data = post_json(f"{cfg.base_url}/chat/completions", payload, cfg.api_key)
    try:
        text = data["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError(f"Unexpected chat response: {data}") from e
    return text.strip()


def synthesize_speech(text: str, out_path: Path, cfg: Config) -> None:
    payload = {
        "model": cfg.tts_model,
        "voice": cfg.tts_voice,
        "input": text,
        "response_format": "wav",
    }
    req = urllib.request.Request(
        f"{cfg.base_url}/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            audio = resp.read()
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"TTS HTTP {e.code}: {details}") from e
    out_path.write_bytes(audio)


def run_voice_turn(cfg: Config, history: List[Dict[str, str]], board=None, leds=None) -> None:
    with tempfile.TemporaryDirectory(prefix="aiyvoice_") as tmp:
        in_wav = Path(tmp) / "input.wav"
        out_wav = Path(tmp) / "reply.wav"

        set_led(board, _led_state("BLINK_3"), leds=leds)
        print(f"Recording for {cfg.record_seconds}s...")
        record_wav(in_wav, cfg)

        rms, peak = wav_signal_levels(in_wav)
        if peak == 0:
            raise RuntimeError(
                "No microphone signal detected (recording is all zeros). "
                "Check VoiceBonnet mic path or INPUT_DEVICE/RECORD_* settings."
            )

        set_led(board, _led_state("PULSE_QUICK"), leds=leds)
        print("Transcribing...")
        user_text = transcribe_audio(in_wav, cfg)
        if not user_text:
            print("I did not catch that. Please try again.")
            set_led(board, _led_state("ON"), leds=leds)
            return
        print(f"You: {user_text}")

        reply = chat_reply(user_text, history, cfg)
        print(textwrap.fill(f"AIYVoice: {reply}", width=92))

        set_led(board, _led_state("BEACON_DARK"), leds=leds)
        synthesize_speech(reply, out_wav, cfg)
        play_wav(out_wav, cfg)
        set_led(board, _led_state("ON"), leds=leds)

        add_history(history, user_text, reply, cfg.max_turns)


def print_self_test(cfg: Config) -> int:
    print("== AIYVoice Bot Self-Test ==")
    print(f"Python: {sys.version.split()[0]}")
    print(f"API key present: {'yes' if bool(cfg.api_key) else 'no'}")
    print(f"Base URL: {cfg.base_url}")
    print(f"Capture device: {'yes' if has_capture_device() else 'no'}")
    print(f"Playback device: {'yes' if has_playback_device() else 'no'}")
    print(f"AIY library: {'yes' if has_aiy_library() else 'no'}")

    if has_aiy_library():
        try:
            board = Board()
            try:
                set_led(board, _led_state("OFF"))
                print("AIY board init: yes")
            finally:
                board.close()
        except Exception as e:
            print(f"AIY board init: no ({e})")

    return 0


def run_voice_loop(cfg: Config, once: bool) -> int:
    history: List[Dict[str, str]] = []
    print("Voice mode ready. Press Enter to record, or type q to quit.")

    while True:
        try:
            action = input("\\n[Enter/q] > ").strip().lower()
        except EOFError:
            print("\\nInput closed. Exiting.")
            return 0

        if action in {"q", "quit", "exit"}:
            return 0

        try:
            run_voice_turn(cfg, history)
        except KeyboardInterrupt:
            print("\\nStopped.")
            return 0
        except Exception as e:
            print(f"Error: {e}")

        if once:
            return 0


def run_aiy_loop(cfg: Config, once: bool) -> int:
    if not has_aiy_library():
        print("AIY library not installed. Falling back to voice mode.")
        return run_voice_loop(cfg, once)

    if not has_capture_device():
        print("No capture device detected. Falling back to text mode.")
        return run_text_loop(cfg, once)

    if not has_playback_device():
        print("No playback device detected. Cannot run AIY mode.")
        return 1

    history: List[Dict[str, str]] = []
    try:
        with Board() as board:
            rgb_leds = None
            if Leds is not None and Color is not None:
                try:
                    rgb_leds = Leds()
                except Exception:
                    rgb_leds = None
            set_led(board, _led_state("OFF"), leds=rgb_leds)
            print("AIY mode ready. Press the board button to talk. Ctrl+C to quit.")

            while True:
                set_led(board, _led_state("PULSE_SLOW"), leds=rgb_leds)
                board.button.wait_for_press()
                board.button.wait_for_release()

                try:
                    run_voice_turn(cfg, history, board=board, leds=rgb_leds)
                except Exception as e:
                    set_led(board, _led_state("BLINK"), leds=rgb_leds)
                    print(f"Error: {e}")
                finally:
                    set_led(board, _led_state("OFF"), leds=rgb_leds)

                if once:
                    return 0
    except KeyboardInterrupt:
        print("\\nStopped.")
        return 0
    except Exception as e:
        print(f"AIY board error: {e}. Falling back to voice/text mode.")
        if has_capture_device() and has_playback_device():
            return run_voice_loop(cfg, once)
        return run_text_loop(cfg, once)


def run_text_loop(cfg: Config, once: bool) -> int:
    history: List[Dict[str, str]] = []
    print("Text mode ready. Type q to quit.")

    while True:
        try:
            user_text = input("\\nYou > ").strip()
        except KeyboardInterrupt:
            print("\\nStopped.")
            return 0
        except EOFError:
            print("\\nInput closed. Exiting.")
            return 0

        if user_text.lower() in {"q", "quit", "exit"}:
            return 0
        if not user_text:
            continue

        try:
            reply = chat_reply(user_text, history, cfg)
            print(textwrap.fill(f"AIYVoice: {reply}", width=92))

            if has_playback_device():
                with tempfile.TemporaryDirectory(prefix="aiyvoice_") as tmp:
                    out_wav = Path(tmp) / "reply.wav"
                    synthesize_speech(reply, out_wav, cfg)
                    play_wav(out_wav, cfg)

            add_history(history, user_text, reply, cfg.max_turns)
        except Exception as e:
            print(f"Error: {e}")

        if once:
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AIYVoice bot")
    parser.add_argument("--mode", choices=["auto", "aiy", "voice", "text"], default="auto")
    parser.add_argument("--once", action="store_true", help="Run one turn and exit")
    parser.add_argument("--self-test", action="store_true", help="Print local checks and exit")
    args = parser.parse_args()

    cfg = build_config()

    if args.self_test:
        return print_self_test(cfg)

    if not cfg.api_key:
        print("OPENAI_API_KEY is missing. Put it in ~/.env or ./.env")
        return 1

    mode = args.mode
    if mode == "auto":
        if has_aiy_library():
            mode = "aiy"
        elif has_capture_device():
            mode = "voice"
        else:
            mode = "text"

    if mode == "aiy":
        return run_aiy_loop(cfg, args.once)

    if mode == "voice" and not has_capture_device():
        print("No capture device detected. Falling back to text mode.")
        mode = "text"

    if mode == "voice" and not has_playback_device():
        print("No playback device detected. Cannot run voice mode.")
        return 1

    if mode == "text":
        return run_text_loop(cfg, args.once)
    return run_voice_loop(cfg, args.once)


if __name__ == "__main__":
    raise SystemExit(main())
