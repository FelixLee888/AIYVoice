# AIYVoice Bot

Local voice assistant bot for the AIYVoice Raspberry Pi.

## What it does
- AIY button mode (`--mode aiy`): press board button to record and get spoken reply
- LED feedback in AIY mode (listening/transcribing/speaking/error states)
- Records microphone audio with `arecord` (voice mode)
- Transcribes speech using OpenAI audio transcription
- Generates assistant replies using OpenAI chat completions
- Speaks replies using OpenAI TTS + `aplay`
- Falls back to text mode if no microphone is detected

## Files
- `bot.py` - main bot loop
- `.env.example` - environment template
- `run.sh` - run helper

## Setup
1. Create env file:
   ```bash
   cd ~/aiyvoice-bot
   cp .env.example .env
   nano .env
   ```
2. Set `OPENAI_API_KEY` in `.env` (or globally in shell profile).

## Run
- Auto mode (prefers AIY mode if AIY library is available):
  ```bash
  cd ~/aiyvoice-bot
  ./run.sh --mode auto
  ```
- AIY button mode:
  ```bash
  ./run.sh --mode aiy
  ```
- Standard keyboard-triggered voice mode:
  ```bash
  ./run.sh --mode voice
  ```
- Text mode:
  ```bash
  ./run.sh --mode text
  ```
- Self-test:
  ```bash
  ./run.sh --self-test
  ```

## Audio notes
- Current host playback device: HDMI (`card 0`)
- If using USB microphone, set `INPUT_DEVICE` in `.env` (example: `hw:1,0`).
- If playback should use non-default output, set `OUTPUT_DEVICE`.
