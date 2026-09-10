# SIH Voice Assistant — Stage 3: Full Loop (ASR + local LLM + local TTS)

This extends the Stage 2 ASR-only server into a complete voice-assistant
loop, still with **no keyword spotting yet** — that's the next stage.

> Custom KWS on ESP32 → detect custom keyword → stream subsequent audio → remote ASR server

Right now, a physical button press stands in for the KWS wake event.
The code is structured so that swap is localized (see
[section 9](#9-where-kws-will-plug-in-later)).

---

## 1. Architecture

```
[BOOT button held] -> INMP441 -> ESP32-S3 (I2S mic, 16kHz mono PCM16)
                                    |  WebSocket binary frames
                                    v
                      FastAPI  WS /ws/assistant   (this server, on your PC)
                                    |
                     +--------------+---------------+
                     v              v                v
              faster-whisper   Ollama (Qwen3-4B)   Piper TTS
              (ASR, existing)   /api/chat, local     (new, local)
                     |              |                 |
               transcribed ->  answer text  ->   WAV bytes -> saved as
                 question                             /answer.wav
                                    |
                    JSON {question, answer, audio_url, timings}
                                    |  (same WebSocket)
                                    v
                      ESP32 parses JSON, then fetches the WAV over
                      plain HTTP using the ALREADY-WORKING
                      ESP32-audioI2S playback path:
                          audio.connecttohost(url)
                                    |
                                    v
                            MAX98357A -> speaker
```

Why the answer audio is delivered as an HTTP fetch rather than pushed
over the WebSocket: `ESP32-audioI2S` already has a tested, working
playback pipeline built around fetching a URL
(`connecttospeech()`/`connecttohost()`). Reusing that avoids building
a second, riskier "receive arbitrary PCM over WebSocket and hand it to
I2S" path on the ESP32 for this stage — matches the "prioritize a
WORKING prototype" instruction.

**No WAV upload in the mic -> server direction** — the mic path is
WebSocket binary PCM chunks the whole way, per your requirement. The
*only* HTTP fetch is the ESP32 pulling back the generated answer.

---

## 2. Folder structure

```
asr_server/
├── server.py                    # FastAPI app — extended, /transcribe & /ws/transcribe unchanged
├── requirements.txt              # Python dependencies (+ piper-tts)
├── README.md                      # this file
├── test_client.py                 # tests /transcribe (unchanged)
├── test_ask.py                     # NEW — tests /ask (LLM leg) independently
├── audio/                           # test .wav files
├── piper_voices/                     # NEW — downloaded Piper voice model lives here
├── answers/                           # NEW — auto-created; holds latest_answer.wav
└── esp32_voice_assistant/
    └── esp32_voice_assistant.ino       # NEW — ESP32 sketch (replaces your Stage-1 mic test sketch)
```

**Do not delete your Stage 2 files.** Before making changes, back them up:

```powershell
cd C:\Users\priya\OneDrive\Desktop\sih
copy server.py server_stage2_backup.py
copy requirements.txt requirements_stage2_backup.txt
```

Then drop in the new `server.py` / `requirements.txt` from this
response. `/transcribe` and `/ws/transcribe` behave exactly as before
— nothing was removed, only added.

---

## 3. Issues identified and resolved before writing this code

1. **Piper TTS is a new dependency.** Verified against the current
   package (`piper-tts` v1.4.2, OHF-Voice/piper1-gpl) — API used here
   (`PiperVoice.load()`, `voice.synthesize_wav()`) matches the current
   library source.
2. **Ollama's REST API must actually be serving**, separately from
   just having run `ollama run ...` once in a terminal — checked
   explicitly in the test procedure below and surfaced in `/health`.
3. **Qwen3's "thinking" mode** adds latency and `<think>` tags. The
   server passes Ollama's official `"think": false` parameter, and
   additionally strips any leftover `<think>...</think>` block
   defensively, since custom `hf.co/...` GGUF imports don't always
   fully honor the flag depending on their chat template.
4. **I2S peripheral contention.** ESP32-S3 has exactly two hardware
   I2S controllers. The speaker path already claims one via
   `audio.setPinout()`. The mic path claims the second via the core's
   built-in `ESP_I2S.h` (`I2SClass`) — this should coexist since both
   are IDF5-based in this core version, but it's the riskiest new
   integration point on this board+library combination. **Test the
   mic path alone (test 4 below) before combining it with playback.**
5. **INMP441 format**: reading directly at 16-bit width with the new
   `I2SClass` gives 16kHz mono PCM16 with no manual bit-shifting —
   confirmed against Espressif's own example using this exact mic.
   Slot mode (mono vs. stereo) may need adjusting for your specific
   L/R wiring; a fallback is noted directly in the sketch's comments.
6. **New Arduino libraries needed**: `WebSockets` (Links2004, for the
   WebSocket *client* — not included in core `WiFi.h`/`WebServer.h`)
   and `ArduinoJson` (to parse the server's JSON reply). Install both
   via Arduino IDE's Library Manager.
7. **Piper's default voice runs at 22050 Hz, not 16kHz** — no fix
   needed, since ESP32-audioI2S reads the WAV header and reconfigures
   its own I2S clock automatically.
8. **Blocking calls in async endpoints**: `/transcribe`,
   `/ws/transcribe`, and now the pipeline all call blocking CPU-bound
   or network code directly. Fine for a single ESP32 client in a
   demo/prototype; flagged as a known limitation, not fixed here (see
   section 12).

---

## 4. New installation steps

With your existing venv activated:

```powershell
cd C:\Users\priya\OneDrive\Desktop\sih
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 4a. Download the Piper voice

```powershell
python -m piper.download_voices en_US-lessac-medium --data-dir piper_voices
```

This downloads `en_US-lessac-medium.onnx` and
`en_US-lessac-medium.onnx.json` into `piper_voices/`. First run needs
internet; after that it's fully offline.

### 4b. Confirm Ollama is set up correctly

```powershell
ollama list
```

You should see `hf.co/Qwen/Qwen3-4B-GGUF:Q4_K_M` in the list. If
Ollama isn't running as a background service, start it explicitly in
its own terminal (see the terminal map in section 8):

```powershell
ollama serve
```

### 4c. Arduino libraries (new)

In Arduino IDE: **Tools → Manage Libraries...**, install:
- **WebSockets** by Markus Sattler (Links2004)
- **ArduinoJson** by Benoit Blanchon

`WiFi.h`, `ESP_I2S.h` (I2SClass), and `Audio.h` (ESP32-audioI2S) are
already part of your existing setup.

---

## 5. Files changed / added

| File | Change |
|---|---|
| `server.py` | Extended. `/transcribe`, `/ws/transcribe` unchanged. Added: `/health` now reports TTS/LLM status, `POST /ask`, `POST /pipeline`, `GET /answer.wav`, `WS /ws/assistant`. |
| `requirements.txt` | Added `piper-tts==1.4.2`. |
| `test_client.py` | Unchanged — still tests `/transcribe`. |
| `test_ask.py` | **New** — tests `/ask` (LLM leg) independently of audio. |
| `esp32_voice_assistant/esp32_voice_assistant.ino` | **New** — full ESP32 client (mic capture, WebSocket, playback). This replaces whatever sketch you were using for the Stage 1 mic test. |
| `piper_voices/`, `answers/` | **New** folders, auto-created / populated by the steps above. |

---

## 6. Exact API additions

### `POST /ask`
```json
{"question": "What is today's date?"}
```
→
```json
{"question": "What is today's date?", "answer": "Today is September 4, 2026.", "llm_time_sec": 0.87}
```

### `POST /pipeline` (multipart file upload, field name `file`)
```json
{
  "success": true,
  "question": "what is today's date",
  "answer": "Today is September 4, 2026.",
  "audio_url": "/answer.wav",
  "sample_rate": 22050,
  "timings": {"asr_sec": 0.41, "llm_sec": 0.87, "tts_sec": 0.35, "total_sec": 1.63}
}
```

### `WS /ws/assistant`
Same protocol as `/ws/transcribe`: send binary PCM16LE mono 16kHz
frames, then `{"event":"end"}`. Reply is the same JSON shape as
`/pipeline` above.

### `GET /answer.wav`
Returns the most recently generated answer as a WAV file
(`audio/wav`). 404 if nothing has been generated yet.

---

## 7. Testing procedure (do these in order)

### Test 1 — Qwen through Ollama, independently

**Terminal A** (keep open, this is the Ollama service):
```powershell
ollama serve
```

**Terminal B** (one-off check):
```powershell
ollama run hf.co/Qwen/Qwen3-4B-GGUF:Q4_K_M "What is today's date?"
```
Confirms the model itself works before anything else touches it.

### Test 2 — FastAPI → Qwen

**Terminal A**: `ollama serve` (still running from Test 1)

**Terminal C** (server):
```powershell
cd C:\Users\priya\OneDrive\Desktop\sih
venv\Scripts\Activate.ps1
python server.py
```

**Terminal D** (client):
```powershell
cd C:\Users\priya\OneDrive\Desktop\sih
venv\Scripts\Activate.ps1
python test_ask.py "What is today's date?"
```
Expect a printed answer and an `llm_time_sec`. Also check
`http://127.0.0.1:8000/health` — `llm.ollama_reachable` should be `true`.

### Test 3 — PC TTS independently

With Terminal C (server) still running, from Terminal D:
```powershell
python test_client.py audio\test.wav
```
Wait for it to succeed (this exercises ASR only, as before), then, to
exercise the *full* pipeline from a file (no ESP32 needed yet):

```powershell
curl.exe -F "file=@audio\test.wav" http://127.0.0.1:8000/pipeline
```

Check the JSON includes a non-empty `answer`, then confirm audio was
actually generated:
```powershell
curl.exe http://127.0.0.1:8000/answer.wav -o test_answer.wav
```
Play `test_answer.wav` on your laptop to confirm Piper is producing
real speech before ever touching the ESP32.

### Test 4 — ESP32 microphone → FastAPI (mic path ALONE, no LLM/TTS yet)

Before flashing the full sketch, this is the point to sanity-check the
I2S mic + WebSocket path in isolation, since that's the riskiest new
piece (see issue #4 in section 3). Simplest way: temporarily point the
sketch's `WS_PATH` at `/ws/transcribe` instead of `/ws/assistant` and
flash that — it uses the identical mic-capture code but only expects
plain ASR back, so you can confirm audio quality and the WebSocket
connection independently of Ollama/Piper.

**Terminal A**: `ollama serve` (can be left running or stopped, unused for this test)
**Terminal C**: `python server.py` (already running)

Update `WS_HOST` in the `.ino` file to your laptop's IP (`ipconfig`),
temporarily change `WS_PATH` to `"/ws/transcribe"`, upload, open Serial
Monitor at 115200 baud, hold the BOOT button, speak, release. Confirm
the server logs a `[/ws/transcribe]` line with recognizable
transcribed text.

Once that works, change `WS_PATH` back to `"/ws/assistant"` and
re-upload for the full loop.

### Test 5 — Full loop: speech → ASR → Qwen → TTS → ESP32 speaker

**Terminal A**: `ollama serve`
**Terminal C**: `python server.py`

Flash the full `esp32_voice_assistant.ino` (with `WS_PATH =
"/ws/assistant"`). Open Serial Monitor at 115200 baud. Hold the BOOT
button, ask a question out loud, release the button.

Expected Serial output:
```
Recording...
Recording stopped. Duration: 2140 ms
Waiting for answer from server...
---- Assistant response ----
Question: what is today's date
Answer:   Today is September 4, 2026.
ASR time:   0.41 s
LLM time:   0.87 s
TTS time:   0.35 s
Server total: 1.63 s
Round trip (send-end -> answer received): 1701 ms  [buffered prototype latency, NOT real-time/incremental]
Fetching and playing: http://192.168.1.23:8000/answer.wav
Playback finished. Ready for next question.
```
Expected behavior: after releasing the button, the speaker plays
Qwen's answer within a couple of seconds (CPU-dependent).

---

## 8. Which terminal runs what

| Terminal | Purpose | Command | When to open |
|---|---|---|---|
| A | Ollama service | `ollama serve` (if not already running as a background service) | Before Test 1, stays open the whole time |
| B | One-off Ollama CLI check | `ollama run hf.co/Qwen/Qwen3-4B-GGUF:Q4_K_M "..."` | Test 1 only, can close after |
| C | FastAPI server | `python server.py` | Before Test 2, stays open through Test 5 |
| D | Test clients | `python test_ask.py ...`, `python test_client.py ...`, `curl.exe ...` | As needed, one-off per command |
| (Arduino IDE Serial Monitor) | ESP32 logs | — | Tests 4 and 5 |

---

## 9. Where KWS will plug in later

In `esp32_voice_assistant.ino`, the entire trigger mechanism is
isolated in two functions:

```cpp
void onWakeTriggered() { ... }   // currently called on button press
void onWakeEnded()      { ... }   // currently called on button release
```

When the custom TinyML KWS model is integrated, its wake-word
detection callback replaces the button-press call site for
`onWakeTriggered()`, and its end-of-speech/VAD logic replaces the
button-release call site for `onWakeEnded()`. Nothing in
`captureAndSendChunk()`, the WebSocket protocol, or the server's
`/ws/assistant` pipeline needs to change.

---

## 10. Latency measurement

- **Server-side** (printed in `server.py`'s log and included in every
  `/ws/assistant` / `/pipeline` JSON response): `asr_sec`, `llm_sec`,
  `tts_sec`, `total_sec`.
- **ESP32-side** (printed to Serial): recording duration, and the
  round trip from sending `{"event":"end"}` to receiving the JSON
  answer.
- Audio *capture* latency itself isn't separately measurable without
  clock-synced devices; the recording duration plus round-trip time
  together bound the user-perceived delay.

---

## 11. On "real-time" — read this before demoing

**This is not real-time or incremental processing.** Both
`/ws/transcribe` and `/ws/assistant` buffer the *entire* utterance
before doing anything with it — ASR, LLM, and TTS each run once, after
the button is released, one after another. The user waits for the
full `asr_sec + llm_sec + tts_sec` (roughly the `total_sec` in the
JSON) before hearing anything.

This is a **buffered prototype**, not streaming/incremental ASR. True
streaming would mean transcribing partial results *while* the person
is still speaking. Don't describe this demo as "real-time" — describe
it as "buffered, end-to-end, and fully local," which is accurate and
still a solid result for this stage.

---

## 12. Known limitations / what to optimize next

- Buffered, not streaming (see section 11).
- Blocking calls inside async endpoints (ASR, Ollama HTTP call, Piper
  synthesis) block the event loop during processing — fine for one
  ESP32 client, would need offloading to a thread pool or separate
  workers for multiple concurrent users.
- No VAD — end-of-speech is entirely the button release (or the future
  KWS system's silence detection), not detected server-side.
- I2S mic + I2S speaker sharing the ESP32-S3's two hardware
  controllers is untested at scale — works for one mic + one speaker,
  but leaves no headroom for additional I2S peripherals.
- Piper's `en_US-lessac-medium` voice is a reasonable default; smaller
  Piper voices (`*-low`) would reduce `tts_sec` further at some
  quality cost if latency becomes the bottleneck.
- Qwen3-4B on CPU via Ollama is the likely biggest contributor to
  `total_sec`; a smaller quantization or GPU offload would help most.
- No automatic retry/backoff if the ESP32's WebSocket briefly drops
  mid-recording — the current buffer is simply lost.

---

## 13. Stage 4 — Browser dashboard as the trigger

### Architecture

The browser **cannot** control the ESP32 directly: the ESP32 is a
WebSocket *client* (it dials out to the server), not a server with a
listening address the browser could hit. The only thing both sides
are already connected to is this FastAPI server, so it's the natural
relay point — no new connection on the ESP32 side, no new port:

```
Browser  --WS /ws/dashboard-->  Server  --(same open /ws/assistant)-->  ESP32
   ^                               |
   |                               v
   +------ status/result JSON <----+   (broadcast to every dashboard tab)
```

- `GET /` serves the dashboard page.
- `WS /ws/dashboard` (new) is what the browser talks to: it sends
  `{"event":"start"}` / `{"event":"stop"}`, and receives `status`,
  `result`, and `esp32_status` / `error` messages as the pipeline runs.
- `WS /ws/assistant` (internals extended, ESP32-facing protocol
  **unchanged**) now also tracks the single connected ESP32 socket so
  the server can push `{"command":"start"}` / `{"command":"stop"}` to
  it, and accepts a new incoming `{"event":"playback_done"}` so the
  dashboard knows when to stop showing "Speaking...".

**Does the BOOT button still work?** Yes, unchanged. Both the button
and the dashboard button call the exact same `onWakeTriggered()` /
`onWakeEnded()` functions on the ESP32 — the button calls them
directly from `checkTriggerButton()`, the dashboard calls them
indirectly via the `{"command":"start"/"stop"}` messages relayed
through `/ws/assistant`. Either can be used interchangeably at any
time; there's no mode switch. This also means the button remains a
working fallback if the dashboard/browser isn't available.

### Files changed / added (Stage 4)

| File | Change |
|---|---|
| `server.py` | Added: dashboard relay state, `broadcast_to_dashboards()`, `run_pipeline_with_progress()` (dashboard-aware, used only by `/ws/assistant`; `run_full_pipeline()` is untouched and still used by `/pipeline`), `WS /ws/dashboard`, `GET /` (dashboard HTML). `/ws/assistant`'s internals were extended (tracks the ESP32 socket, broadcasts progress, handles `playback_done`) but its wire protocol with the ESP32 is unchanged. `/transcribe`, `/ws/transcribe`, `/ask`, `/pipeline`, `/answer.wav`, `/health` are all untouched. |
| `esp32_voice_assistant.ino` | `webSocketEvent`'s `WStype_TEXT` case now dispatches to a new `handleIncomingText()`, which tells apart a dashboard `{"command":...}` (routed to the existing `onWakeTriggered()`/`onWakeEnded()`) from the normal pipeline result (routed to `handleAssistantResponse()`, now taking an already-parsed `JsonDocument&` instead of re-parsing). `audio_eof_stream()` now also sends `{"event":"playback_done"}`. Mic capture, WiFi, and playback code are all unchanged. |

### Test procedure

1. **Open dashboard** — browse to `http://10.50.40.102:8000/` on your
   laptop or phone (same Wi-Fi).
2. **Verify ESP32 connected** — the "Device" panel should show
   ESP32-S3 as Connected (green dot) within a couple seconds of the
   ESP32 booting and joining Wi-Fi. If it stays red, check the ESP32's
   Serial Monitor for `[WS] Connected to /ws/assistant`.
3. **Click microphone** — button turns red and pulses, label changes
   to "Listening...". ESP32 Serial Monitor should print `[dashboard]
   start command received` then `Recording...`.
4. **Speak into the INMP441.**
5. **Click microphone again** — label moves through "Processing
   speech..." → "Generating response..." → "Generating audio...".
   ESP32 Serial Monitor prints `[dashboard] stop command received`
   then `Recording stopped...` then `Waiting for answer...`.
6. **Verify recognized speech appears** — the "You said" panel updates
   with your transcribed question.
7. **Verify Qwen answer appears** — the "Assistant" panel updates with
   the generated answer text, and the Latency panel fills in
   ASR/LLM/TTS/Total.
8. **Verify ESP32 speaks the answer** — label shows "Speaking...", the
   MAX98357A plays the answer, then the label returns to "Ready" once
   the ESP32 sends `playback_done`.

Throughout, the BOOT button can be used at any point instead of the
dashboard button — both drive the same state machine, so mixing them
(e.g. start via dashboard, stop via button) also works, though it's
not a normal use case worth relying on.

### Known limitations (Stage 4 additions)

- The dashboard shows one ESP32 connection status only — INMP441 and
  MAX98357A rows mirror that single link status rather than being
  independently verified, since the ESP32 doesn't currently report
  per-peripheral health.
- If two browser tabs are open, both receive all broadcasts and both
  mic buttons control the same single ESP32 — there's no per-client
  ownership/locking.
- The "listening" state shown to the dashboard right after clicking
  start is optimistic (the server doesn't wait for an ack from the
  ESP32) — matches the existing "prioritize working over perfect"
  approach for this stage.
