# Jarvis — Local Voice Assistant

A fully-local Jarvis-style personal assistant for Windows. Python handles the
audio pipeline, wake word, STT, speaker ID, TTS, skills and LLM; a small C++
[Dear ImGui](https://github.com/ocornut/imgui) HUD renders an always-on-top
Iron-Man-style orb that talks to the backend over a local WebSocket.

- **Wake word:** [openWakeWord](https://github.com/dscripka/openWakeWord) (`hey jarvis`)
- **VAD:** [silero-vad](https://github.com/snakers4/silero-vad)
- **STT:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- **Speaker ID:** [Resemblyzer](https://github.com/resemble-ai/Resemblyzer)
- **TTS:** [Piper](https://github.com/rhasspy/piper)
- **LLM:** [Ollama](https://ollama.com/) (local)
- **Frontend:** C++20 + Dear ImGui + GLFW + ixwebsocket

## Repo layout

```
backend/          Python package (jarvis.*)
frontend/         C++ HUD (CMake)
scripts/          Helper scripts (model downloader)
models/           Piper / Whisper cache (downloaded, gitignored)
profiles/         Saved speaker embeddings (*.npy, gitignored)
config.yaml       Runtime configuration
```

## 1. Prerequisites

- **Windows 10/11** (skills use `pycaw` / `pywin32`; other platforms partially work)
- **Python 3.10 – 3.12**
- **Ollama** running locally: [install](https://ollama.com/download)
  ```powershell
  ollama pull llama3.1:8b-instruct-q4_K_M
  ollama serve      # usually auto-starts
  ```
- **CMake 3.20+** and a C++20 toolchain (MSVC 2022, or Clang/MinGW)
- A **microphone** and speakers

## 2. Backend

### Install

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Download models (one-off)

```powershell
python ..\scripts\download_models.py
```

This fetches the Piper voice (`en_US-lessac-medium`), warms the
openWakeWord / silero-vad / faster-whisper / Resemblyzer caches.

### Run

```powershell
python -m jarvis.main --config ..\config.yaml
```

You should see a WebSocket server start on `ws://127.0.0.1:8765`. Say
**"hey jarvis"** to wake it, then speak your request.

## 3. Frontend (HUD)

Any C++20 toolchain works. Tested combos:

**MinGW-w64 via MSYS2** (lightest, ~500 MB of tools):

```powershell
winget install MSYS2.MSYS2
C:\msys64\usr\bin\bash.exe -l -c "pacman -S --noconfirm --needed mingw-w64-ucrt-x86_64-toolchain mingw-w64-ucrt-x86_64-cmake mingw-w64-ucrt-x86_64-ninja git"
$env:Path = "C:\msys64\ucrt64\bin;" + $env:Path

cd frontend
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
.\build\jarvis_hud.exe
```

**MSVC** (if you have VS 2022 with the C++ workload):

```powershell
cmake -S frontend -B frontend\build -G "Visual Studio 17 2022" -A x64
cmake --build frontend\build --config Release
.\frontend\build\Release\jarvis_hud.exe
```

Add `-DJARVIS_CONSOLE=ON` to build with a console attached (useful for seeing
log/error output during development).

The HUD appears in the top-right of your primary monitor as a transparent,
frameless, always-on-top orb. Colour changes indicate state:

| State        | Colour |
| ------------ | ------ |
| Disconnected | grey   |
| Idle         | cyan   |
| Listening    | blue   |
| Thinking     | amber  |
| Speaking     | green  |

**Hotkey:** `Ctrl + Space` → push-to-talk (skips the wake word).

Pass `--url ws://host:port` (or set `JARVIS_URL`) if the backend isn't on
localhost.

## 4. Speaker enrollment

Speaker recognition is off by default until you enroll at least one profile.
You can send an enrollment command over the WebSocket (any ws client):

```json
{ "cmd": "enroll_start", "name": "bertie" }
```

Jarvis will record 5 short samples. After the last sample it writes
`profiles/bertie.npy` and will start matching future utterances against it
(threshold configurable in `config.yaml`). Unknown speakers are reported as
`guest` and cannot run intents listed under `permissions.restricted_intents`
(default: `shutdown`, `sleep`, `lock`).

Set your own name in `config.yaml`:

```yaml
permissions:
  owner: "bertie"
  restricted_intents: [shutdown, sleep, lock]
```

## 5. What Jarvis can do out of the box

- **Info:** "what's the time", "what's the date", "weather", "system stats"
- **Volume:** "volume up/down", "mute/unmute", "set volume to 40 percent"
- **Media:** "play", "pause", "next track", "previous track", "stop music"
- **Apps:** "open Firefox", "launch notepad"
- **Web:** "search for cheesecake recipe", "open github.com"
- **System:** "lock the PC", "go to sleep", "shut down", "cancel shutdown"
- **Chat:** anything not matched above goes to Ollama as a free-form query

## 6. WebSocket protocol

Backend → client events:

| event | payload |
| ----- | ------- |
| `hello` | `{ state, muted, profiles[] }` |
| `state` | `{ state }` — idle \| listening \| thinking \| speaking |
| `wake`  | — |
| `listening` | — |
| `transcript` | `{ text, user, score }` |
| `reply` | `{ text, intent, success }` |
| `speaking_start` | `{ text }` |
| `speaking_end` | — |
| `muted` | `{ value }` |
| `enroll_progress` | `{ name, collected, target }` |
| `error` | `{ message }` |

Client → backend commands (all wrapped as `{ "cmd": "..." }`):

- `push_to_talk`
- `cancel`
- `mute` (with optional `value: bool`)
- `enroll_start` (with `name`)
- `enroll_cancel`
- `say` (with `text`) — forces Jarvis to speak a string

## 7. Configuration

Everything lives in [`config.yaml`](config.yaml). Common tweaks:

- `wake_word.threshold` — lower = more sensitive
- `stt.model` — `tiny.en` | `base.en` | `small.en` | `medium.en`
- `stt.device` — `auto` | `cpu` | `cuda`
- `llm.model` — any tag you've pulled with `ollama pull`
- `speaker_id.threshold` — cosine similarity cut-off (0.70 – 0.85 typical)

## 8. Troubleshooting

- **No audio / microphone errors** — list devices with
  `python -c "import sounddevice as sd; print(sd.query_devices())"` and set
  `audio.input_device` in `config.yaml`.
- **Wake word never triggers** — lower `wake_word.threshold` to `0.3`.
- **"I couldn't reach the language model"** — make sure `ollama serve` is
  running and `llm.model` matches a pulled tag (`ollama list`).
- **HUD is invisible** — check the top-right corner of your primary display;
  it's transparent by design. Alt-tab to focus.
- **Whisper is slow** — switch to `stt.model: tiny.en` or install CUDA and set
  `stt.device: cuda`.

## 9. Out of scope (v1)

- macOS / Linux parity (system skills are Windows-specific)
- Custom wake word training (use the bundled `hey_jarvis`)
- Cloud fallbacks
- Settings UI (edit `config.yaml`)

## License

MIT.
