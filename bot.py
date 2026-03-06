#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import textwrap
import urllib.error
import urllib.request
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

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
    playback_volume: int
    telegram_bot_token: str
    chief_fafa_chat_id: str
    chief_fafa_prefix: str
    yuen_yuen_chat_id: str
    yuen_yuen_prefix: str


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
            "你係 AIYVoice，請一律以廣東話（繁體中文）回應，語氣自然、簡潔，像在跟屋企人對話咁回覆。",
        ),
        max_turns=int(env("MAX_TURNS", "6")),
        playback_volume=int(env("PLAYBACK_VOLUME", "80")),
        telegram_bot_token=env("TELEGRAM_BOT_TOKEN", ""),
        chief_fafa_chat_id=env("TELEGRAM_CHIEF_FAFA_CHAT_ID", ""),
        chief_fafa_prefix=env("TELEGRAM_CHIEF_FAFA_PREFIX", ""),
        yuen_yuen_chat_id=env("TELEGRAM_YUEN_YUEN_CHAT_ID", ""),
        yuen_yuen_prefix=env("TELEGRAM_YUEN_YUEN_PREFIX", ""),
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


def list_capture_devices() -> List[str]:
    proc = run_cmd(["arecord", "-l"], check=False)
    if proc.returncode != 0:
        return []

    lines = proc.stdout.splitlines()
    in_capture_section = False
    devices: List[str] = []
    current_card = None

    for line in lines:
        if line.startswith("**** List of CAPTURE Hardware Devices"):
            in_capture_section = True
            continue
        if line.startswith("**** List of PLAYBACK Hardware Devices"):
            break
        if not in_capture_section:
            continue

        combined = re.match(r"^card\s+(\d+):.*?device\s+(\d+):", line)
        if combined:
            candidate = f"plughw:{combined.group(1)},{combined.group(2)}"
            if candidate not in devices:
                devices.append(candidate)
            current_card = None
            continue

        card_match = re.match(r"^card\s+(\d+):", line)
        if card_match:
            current_card = card_match.group(1)
            continue

        device_match = re.match(r"^\s*device\s+(\d+):", line)
        if device_match and current_card is not None:
            candidate = f"plughw:{current_card},{device_match.group(1)}"
            if candidate not in devices:
                devices.append(candidate)
            current_card = None

    return devices


def has_capture_device() -> bool:
    return len(list_capture_devices()) > 0


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

    def _set_rgb(rgb):
        if leds is None or Leds is None or Color is None:
            return False
        try:
            if rgb is None:
                leds.update(Leds.rgb_off())
            else:
                leds.update(Leds.rgb_on(rgb))
            return True
        except Exception:
            return False

    # Use button RGB colors for processing states while preserving legacy board LED states.
    if leds is not None and Leds is not None and Color is not None:
        if state == _led_state("OFF"):
            if _set_rgb(None):
                return
        if state == _led_state("DECAY"):
            if _set_rgb((8, 8, 8)):
                return
        if state in (_led_state("PULSE_SLOW"), _led_state("ON")):
            if _set_rgb(Color.BLUE):
                return
        if state in (_led_state("BLINK_3"), _led_state("BLINK")):
            if _set_rgb(Color.RED):
                return
        if state == _led_state("PULSE_QUICK"):
            if _set_rgb(Color.PURPLE):
                return
        if state == _led_state("BEACON_DARK"):
            if _set_rgb(Color.GREEN):
                return

        if isinstance(state, str):
            led_label = state.upper()
            if led_label == "LISTENING":
                if _set_rgb(Color.YELLOW):
                    return
            if led_label == "TRANSCRIBING":
                if _set_rgb(Color.PURPLE):
                    return
            if led_label == "THINKING":
                if _set_rgb(Color.CYAN):
                    return
            if led_label == "SPEAKING":
                if _set_rgb(Color.GREEN):
                    return
            if led_label == "DIM":
                if _set_rgb((12, 12, 12)):
                    return
            if led_label == "ERROR":
                if _set_rgb((255, 0, 0)):
                    return
            if led_label == "READY":
                if _set_rgb(Color.BLUE):
                    return

        if _set_rgb(Color.WHITE):
            return

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


def _iter_record_rates(preferred: int) -> List[int]:
    rates = [preferred]
    for rate in (48000, 44100, 32000, 24000, 22050, 16000):
        if rate not in rates:
            rates.append(rate)
    return rates


def record_wav(path: Path, cfg: Config) -> None:
    devices: List[str] = []
    if cfg.input_device:
        devices.append(cfg.input_device)
    else:
        devices.extend(list_capture_devices())

    if not devices:
        devices.extend(["plughw:1,0", "plughw:0,0", "default", "hw:1,0", "hw:0,0"])

    seen = set()
    unique_devices = []
    for device in devices:
        if device and device not in seen:
            seen.add(device)
            unique_devices.append(device)

    last_error = "No capture attempt made."

    for device in unique_devices:
        for rate in _iter_record_rates(cfg.sample_rate):
            cmd = [
                "arecord",
                "-q",
                "-f",
                str(cfg.record_format),
                "-c",
                str(cfg.record_channels),
                "-r",
                str(rate),
                "-d",
                str(cfg.record_seconds),
                "-D",
                device,
                str(path),
            ]
            proc = run_cmd(cmd, check=False)
            if proc.returncode == 0 and path.exists() and path.stat().st_size > 100:
                return

            if path.exists():
                try:
                    path.unlink()
                except Exception:
                    pass

            stderr = (proc.stderr or proc.stdout or "").strip().replace("\n", " | ")
            last_error = f"device={device}, rate={rate}, error={stderr or 'failed'}"

    raise RuntimeError(f"Unable to record audio. Last attempt: {last_error}")



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

    play_path = path
    scaled_tmp_path = None

    volume = getattr(cfg, "playback_volume", 100)
    try:
        volume = int(volume)
    except Exception:
        volume = 100

    if volume < 100:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                scaled_tmp_path = Path(tmp.name)
            volume_factor = max(0.0, min(2.0, volume / 100.0))
            ff_proc = run_cmd(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(path),
                    "-filter:a",
                    f"volume={volume_factor}",
                    str(scaled_tmp_path),
                ],
                check=False,
            )
            if ff_proc.returncode == 0:
                play_path = scaled_tmp_path
            else:
                if scaled_tmp_path is not None:
                    scaled_tmp_path.unlink(missing_ok=True)
                    scaled_tmp_path = None
        except Exception as e:
            if not _AUDIO_WARNING_SHOWN:
                print(f"Warning: could not scale playback volume ({e}); using default volume.")
                _AUDIO_WARNING_SHOWN = True
            play_path = path

    cmd = ["aplay", "-q"]
    if cfg.output_device:
        cmd.extend(["-D", cfg.output_device])
    cmd.append(str(play_path))

    proc = run_cmd(cmd, check=False)
    if proc.returncode == 0:
        if play_path != path:
            play_path.unlink(missing_ok=True)
        return

    err = (proc.stderr or proc.stdout or "").strip()

    # Some Pi setups report a playback card but cannot open it (e.g. inactive HDMI).
    # Switch to silent null sink for this run so chat flow keeps working.
    if not cfg.output_device:
        null_target = play_path
        null_proc = run_cmd(["aplay", "-D", "null", "-q", str(null_target)], check=False)
        if null_proc.returncode == 0:
            _AUDIO_PLAYBACK_DISABLED = True
            if not _AUDIO_WARNING_SHOWN:
                print(
                    "Warning: no usable audio output sink; running in silent mode. "
                    "Set OUTPUT_DEVICE once speaker output is available."
                )
                _AUDIO_WARNING_SHOWN = True
            if scaled_tmp_path is not None:
                scaled_tmp_path.unlink(missing_ok=True)
            return

    _AUDIO_PLAYBACK_DISABLED = True
    if not _AUDIO_WARNING_SHOWN:
        print(f"Warning: audio playback disabled for this session ({err})")
        _AUDIO_WARNING_SHOWN = True

    if scaled_tmp_path is not None:
        scaled_tmp_path.unlink(missing_ok=True)


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


def telegram_send_message(bot_token: str, chat_id: str, text: str, timeout: int = 20) -> None:
    if not bot_token or not chat_id or not text.strip():
        return

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram HTTP {e.code}: {details}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Telegram network error: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid Telegram JSON response: {raw[:500]}") from e

    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {data}")


def _build_telegram_routes(message: str, cfg: Config) -> List[Tuple[str, str, str, str]]:
    normalized = " ".join(message.casefold().split())
    compact = normalized.replace(" ", "")
    routes: List[Tuple[str, str, str, str]] = []

    if "花花" in message or "fa fa" in normalized or "fafa" in compact:
        routes.append(
            ("Chief Fafa", "TELEGRAM_CHIEF_FAFA_CHAT_ID", cfg.chief_fafa_chat_id, cfg.chief_fafa_prefix)
        )
    if "園園" in message or "园园" in message or "yuen yuen" in normalized or "yuenyuen" in compact:
        routes.append(
            ("Yuen Yuen", "TELEGRAM_YUEN_YUEN_CHAT_ID", cfg.yuen_yuen_chat_id, cfg.yuen_yuen_prefix)
        )
    return routes


def forward_mentions_to_telegram(message: str, cfg: Config) -> None:
    if not message.strip() or not cfg.telegram_bot_token:
        return

    routes = _build_telegram_routes(message, cfg)
    if not routes:
        return

    for route_name, env_name, chat_id, prefix in routes:
        if not chat_id:
            print(f"Warning: {route_name} mention detected but {env_name} is not configured.")
            continue
        outbound = f"{prefix}{message}" if prefix else message
        try:
            telegram_send_message(cfg.telegram_bot_token, chat_id, outbound)
            print(f"Forwarded message to {route_name} on Telegram.")
        except Exception as e:
            print(f"Warning: failed to forward to {route_name} on Telegram ({e})")


def synthesize_speech(text: str, path: Path, cfg: Config) -> None:
    if not text.strip():
        raise ValueError("No text provided for speech synthesis.")

    payload = {
        "model": cfg.tts_model,
        "input": text,
        "voice": cfg.tts_voice,
        "response_format": "wav",
    }
    req = urllib.request.Request(
        cfg.base_url + "/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + cfg.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/wav",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            audio = resp.read()
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="replace")
        raise RuntimeError("TTS request failed (" + str(e.code) + "): " + details) from e
    except urllib.error.URLError as e:
        raise RuntimeError("Network error calling " + cfg.base_url + "/audio/speech: " + str(e)) from e

    if not audio:
        raise RuntimeError("TTS response was empty.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio)

def run_voice_turn(cfg: Config, history: List[Dict[str, str]], board=None, leds=None) -> None:
    with tempfile.TemporaryDirectory(prefix="aiyvoice_") as tmp:
        in_wav = Path(tmp) / "input.wav"
        out_wav = Path(tmp) / "reply.wav"

        set_led(board, "LISTENING", leds=leds)
        print(f"Recording for {cfg.record_seconds}s...")
        record_wav(in_wav, cfg)

        rms, peak = wav_signal_levels(in_wav)
        if peak == 0:
            raise RuntimeError(
                "No microphone signal detected (recording is all zeros). " +
                "Check VoiceBonnet mic path or INPUT_DEVICE/RECORD_* settings."
            )

        set_led(board, "TRANSCRIBING", leds=leds)
        print("Transcribing...")
        user_text = transcribe_audio(in_wav, cfg)
        if not user_text:
            print("I did not catch that. Please try again.")
            set_led(board, "READY", leds=leds)
            return
        print(f"You: {user_text}")
        forward_mentions_to_telegram(user_text, cfg)

        set_led(board, "THINKING", leds=leds)
        reply = chat_reply(user_text, history, cfg)
        print(textwrap.fill(f"AIYVoice: {reply}", width=92))

        set_led(board, "SPEAKING", leds=leds)
        synthesize_speech(reply, out_wav, cfg)
        play_wav(out_wav, cfg)
        set_led(board, "READY", leds=leds)

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
    idle_timeout = 60
    last_activity = time.monotonic()
    led_dimmed = False

    try:
        with Board() as board:
            rgb_leds = None
            if Leds is not None and Color is not None:
                try:
                    rgb_leds = Leds()
                except Exception:
                    rgb_leds = None

            set_led(board, "READY", leds=rgb_leds)
            print("AIY mode ready. Press the board button to talk. Ctrl+C to quit.")

            while True:
                now = time.monotonic()
                if now - last_activity >= idle_timeout:
                    if not led_dimmed:
                        set_led(board, "DIM", leds=rgb_leds)
                        led_dimmed = True
                elif led_dimmed:
                    set_led(board, "READY", leds=rgb_leds)
                    led_dimmed = False

                wait_timeout = 1.0
                if now - last_activity < idle_timeout:
                    remaining = idle_timeout - (now - last_activity)
                    if remaining < 1.0:
                        wait_timeout = remaining

                if not board.button.wait_for_press(timeout=wait_timeout):
                    continue

                led_dimmed = False
                last_activity = time.monotonic()
                board.button.wait_for_release()

                try:
                    run_voice_turn(cfg, history, board=board, leds=rgb_leds)
                except Exception as e:
                    set_led(board, "ERROR", leds=rgb_leds)
                    print(f"Error: {e}")
                finally:
                    last_activity = time.monotonic()
                    set_led(board, "READY", leds=rgb_leds)
                    led_dimmed = False

                if once:
                    return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except Exception as e:
        print(f"AIY board error: {e}. Falling back to voice/text mode.")
        if has_capture_device() and has_playback_device():
            return run_voice_loop(cfg, once)
        return run_text_loop(cfg, once)



def speak_text(text: str, cfg: Config) -> int:
    if not text.strip():
        return 0

    if not has_playback_device():
        print("No playback device detected. Cannot speak text.")
        return 1

    with tempfile.TemporaryDirectory(prefix="aiyvoice_") as tmp:
        out_wav = Path(tmp) / "speech.wav"
        synthesize_speech(text, out_wav, cfg)
        play_wav(out_wav, cfg)
    return 0


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
            forward_mentions_to_telegram(user_text, cfg)
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
    parser.add_argument("--mode", choices=["auto", "aiy", "voice", "text"], default="aiy")
    parser.add_argument("--once", action="store_true", help="Run one turn and exit")
    parser.add_argument("--self-test", action="store_true", help="Print local checks and exit")
    parser.add_argument("--speak", type=str, help="Speak provided text and exit")
    args = parser.parse_args()

    cfg = build_config()

    if args.self_test:
        return print_self_test(cfg)

    if args.speak is not None:
        return speak_text(args.speak, cfg)

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
