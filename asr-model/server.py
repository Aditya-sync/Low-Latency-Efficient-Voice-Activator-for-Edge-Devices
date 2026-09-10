"""
ASR Server — SIH project (KWS + remote ASR pipeline)
-----------------------------------------------------
Receives audio from an edge device (eventually an ESP32-S3 running a
locally trained custom wake-word KWS model) and transcribes it using
an open-source speech recognition model (faster-whisper / CTranslate2,
a reimplementation of OpenAI's Whisper — no proprietary/cloud STT
API is used).

Two ways audio reaches this server:

  1. POST /transcribe
     Upload a complete audio file (wav/mp3/etc.). Simple, good for
     first testing with prerecorded files (test_client.py uses this).

  2. WS /ws/transcribe
     Streaming endpoint. Client sends raw PCM16LE mono 16 kHz audio
     as binary WebSocket frames, then a text frame {"event":"end"}
     when the utterance is finished. This is the shape ESP32 -> server
     streaming will eventually use — no WAV header overhead, minimal
     per-chunk cost. NOTE: this is "buffer, then transcribe on end",
     not true incremental streaming transcription yet — see README
     section "How to later modify for low-latency streaming".

-----------------------------------------------------
STAGE 3 ADDITIONS (voice-assistant loop: ASR -> local LLM -> local TTS)
-----------------------------------------------------
Everything above (/transcribe, /ws/transcribe) is UNCHANGED from the
previous stage. Added on top of it:

  POST /ask
      {"question": "..."} -> {"question": "...", "answer": "...", "llm_time_sec": ...}
      Sends text straight to a local Ollama model (Qwen3-4B) and
      returns its answer. Lets you test the LLM leg independently of
      audio.

  POST /pipeline
      Upload a complete audio file -> runs ASR -> LLM -> TTS in one
      shot and returns JSON with the question, the answer, a URL to
      the generated answer audio, and per-stage timings. Useful for
      testing the full loop without an ESP32.

  WS /ws/assistant
      Same wire protocol as /ws/transcribe (binary PCM16 chunks, then
      {"event":"end"}), but instead of just returning transcribed
      text, it runs the full ASR -> LLM -> TTS pipeline and replies
      with JSON: {success, question, answer, audio_url, sample_rate,
      timings}. This is what the ESP32 voice-assistant client uses.

  GET /answer.wav
      Serves the most recently generated TTS answer as a WAV file.
      The ESP32 fetches this with the existing, already-working
      ESP32-audioI2S playback path (audio.connecttohost()) rather
      than receiving pushed PCM over the WebSocket — this reuses a
      pipeline you've already tested successfully instead of
      building a second, riskier one.

LLM: local Ollama server (http://localhost:11434), model
"hf.co/Qwen/Qwen3-4B-GGUF:Q4_K_M" — no cloud LLM API.

TTS: Piper (piper-tts, OHF-Voice/piper1-gpl) — a local, open-source
neural TTS engine. No cloud TTS API.

Still deliberately excluded: authentication, database, Docker, cloud
deployment, wake-word/KWS (that's a separate, later stage).

-----------------------------------------------------
STAGE 4 ADDITIONS (browser dashboard as the trigger, instead of the
ESP32's physical BOOT button)
-----------------------------------------------------
The browser cannot talk to the ESP32 directly (the ESP32 is a
WebSocket *client*, not a server, and isn't reachable by IP for
control). So the dashboard talks to THIS server, and this server
relays commands to the ESP32 over the ESP32's own already-open
/ws/assistant connection — the same connection it already uses to
send mic audio and receive the pipeline result. No second connection
on the ESP32 side, no new port, no new protocol for it to speak:

    Browser --WS /ws/dashboard--> Server --(same open WS)--> ESP32
       ^                             |
       |                             v
       +---- status/result JSON <----+  (broadcast to all dashboard clients)

  GET /
      Serves the dashboard HTML page.

  WS /ws/dashboard
      Browser control + status channel. Browser sends
      {"event":"start"} / {"event":"stop"}; server relays
      {"command":"start"} / {"command":"stop"} to the ESP32 over its
      existing /ws/assistant socket. Server pushes status/result JSON
      to every connected dashboard client as the pipeline progresses.

  WS /ws/assistant (internals extended, wire protocol for the ESP32
  is otherwise UNCHANGED)
      Now also: tracks the single connected ESP32 socket so the
      dashboard can command it; accepts an incoming
      {"event":"playback_done"} text message (new, sent by the ESP32
      once MAX98357A playback finishes, so the dashboard can show
      "Ready" again); and broadcasts per-stage progress to dashboard
      clients while running the pipeline. The BOOT button flow is
      completely unaffected — it still sends {"event":"end"} exactly
      as before and gets the same JSON back.
"""

from email.mime import text
import io
import json
import logging
import re
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
import requests
from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from faster_whisper import WhisperModel
from piper import PiperVoice

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
MODEL_SIZE = "small"     # tiny/base/small/medium/large-v3 — "small" is a good
                          # accuracy/speed tradeoff for a laptop CPU during dev
DEVICE = "cpu"            # switch to "cuda" if you have a working NVIDIA GPU setup
COMPUTE_TYPE = "int8"     # fastest practical setting on CPU
LANGUAGE = "en"           # set to None to let the model auto-detect language
BEAM_SIZE = 5

AUDIO_DIR = Path(__file__).parent / "audio"
AUDIO_DIR.mkdir(exist_ok=True)

# Documented target format for audio coming from the ESP32-S3 (used by the
# streaming endpoint's minimum-length sanity check below).
EXPECTED_SAMPLE_RATE = 16000
EXPECTED_SAMPLE_WIDTH_BYTES = 2   # 16-bit PCM
EXPECTED_CHANNELS = 1              # mono
MIN_PCM_BYTES = EXPECTED_SAMPLE_RATE * EXPECTED_SAMPLE_WIDTH_BYTES // 10  # ~100ms

# ---- Ollama (local LLM) ----
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
OLLAMA_MODEL = "hf.co/Qwen/Qwen3-4B-GGUF:Q4_K_M"
OLLAMA_TIMEOUT_SEC = 120
OLLAMA_SYSTEM_PROMPT = (
    "You are a concise voice assistant. Answer in 1-3 short spoken sentences. "
    "Do not use markdown, bullet points, or headings."
)

# ---- Piper (local TTS) ----
PIPER_VOICES_DIR = Path(__file__).parent / "piper_voices"
PIPER_VOICE_NAME = "en_US-lessac-medium"
PIPER_MODEL_PATH = PIPER_VOICES_DIR / f"{PIPER_VOICE_NAME}.onnx"
PIPER_CONFIG_PATH = PIPER_VOICES_DIR / f"{PIPER_VOICE_NAME}.onnx.json"

# ---- Generated answer audio, served to the ESP32 over HTTP ----
ANSWERS_DIR = Path(__file__).parent / "answers"
ANSWERS_DIR.mkdir(exist_ok=True)
LATEST_ANSWER_WAV = ANSWERS_DIR / "latest_answer.wav"

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("asr_server")

app = FastAPI(title="SIH ASR Server", version="0.1.0")

# --------------------------------------------------------------------------
# Model loading — once, at startup, kept in memory for every request
# --------------------------------------------------------------------------
model: Optional[WhisperModel] = None
model_ready = False
model_load_error: Optional[str] = None


@app.on_event("startup")
def load_model():
    global model, model_ready, model_load_error
    log.info(f"Loading faster-whisper model '{MODEL_SIZE}' (device={DEVICE}, compute_type={COMPUTE_TYPE})...")
    t0 = time.time()
    try:
        model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
        model_ready = True
        log.info(f"Model loaded in {time.time() - t0:.2f}s and ready.")
    except Exception as e:
        model_load_error = str(e)
        log.error(f"Failed to load model: {e}")


piper_voice: Optional[PiperVoice] = None
piper_ready = False
piper_load_error: Optional[str] = None


@app.on_event("startup")
def load_piper_voice():
    global piper_voice, piper_ready, piper_load_error
    if not PIPER_MODEL_PATH.exists() or not PIPER_CONFIG_PATH.exists():
        piper_load_error = (
            f"Voice files not found at {PIPER_MODEL_PATH} / {PIPER_CONFIG_PATH}. "
            f"Run: python -m piper.download_voices {PIPER_VOICE_NAME} --data-dir {PIPER_VOICES_DIR}"
        )
        log.error(piper_load_error)
        return

    log.info(f"Loading Piper voice '{PIPER_VOICE_NAME}'...")
    t0 = time.time()
    try:
        piper_voice = PiperVoice.load(str(PIPER_MODEL_PATH), config_path=str(PIPER_CONFIG_PATH))
        piper_ready = True
        log.info(f"Piper voice loaded in {time.time() - t0:.2f}s and ready.")
    except Exception as e:
        piper_load_error = str(e)
        log.error(f"Failed to load Piper voice: {e}")


def check_ollama_reachable() -> bool:
    try:
        r = requests.get(OLLAMA_TAGS_URL, timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


# --------------------------------------------------------------------------
# Dashboard <-> ESP32 relay state
#
# The ESP32 is a WebSocket CLIENT (it connects out to us), so it has no
# listening address the browser could hit directly. This server is the
# only thing both sides are already connected to, so it's the natural
# relay point: dashboard commands come in on /ws/dashboard and get
# forwarded over the ESP32's own already-open /ws/assistant socket.
# --------------------------------------------------------------------------
dashboard_clients: "set[WebSocket]" = set()
esp32_socket: Optional[WebSocket] = None
current_state = "esp32_disconnected"   # ready / listening / processing_speech /
                                         # generating_response / generating_audio /
                                         # speaking / error / esp32_disconnected


async def broadcast_to_dashboards(message: dict):
    """Send a JSON message to every connected dashboard browser tab."""
    dead = []
    for ws in dashboard_clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        dashboard_clients.discard(ws)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class TranscriptionResponse(BaseModel):
    success: bool
    text: str
    language: Optional[str] = None
    language_probability: Optional[float] = None
    duration_sec: Optional[float] = None
    processing_time_sec: float
    error: Optional[str] = None


# --------------------------------------------------------------------------
# GET /health
# --------------------------------------------------------------------------
@app.get("/health")
def health():
    if model_load_error is not None:
        status = "error"
    elif model_ready:
        status = "ok"
    else:
        status = "loading"

    return {
        "status": status,
        "asr": {
            "model_ready": model_ready,
            "model_size": MODEL_SIZE,
            "device": DEVICE,
            "compute_type": COMPUTE_TYPE,
            "error": model_load_error,
        },
        "tts": {
            "piper_ready": piper_ready,
            "voice": PIPER_VOICE_NAME,
            "error": piper_load_error,
        },
        "llm": {
            "ollama_reachable": check_ollama_reachable(),
            "model": OLLAMA_MODEL,
        },
    }


# --------------------------------------------------------------------------
# POST /transcribe — complete-file upload
# --------------------------------------------------------------------------
@app.post("/transcribe", response_model=None)
async def transcribe(file: UploadFile = File(...)):
    if not model_ready:
        return JSONResponse(
            status_code=503,
            content={"success": False, "text": "", "processing_time_sec": 0.0,
                     "error": "Model not ready yet (still loading or failed to load)"},
        )

    t_start = time.time()
    try:
        raw_bytes = await file.read()
        if len(raw_bytes) == 0:
            return JSONResponse(
                status_code=400,
                content={"success": False, "text": "", "processing_time_sec": 0.0, "error": "Empty file"},
            )

        audio_buffer = io.BytesIO(raw_bytes)
        segments, info = model.transcribe(audio_buffer, beam_size=BEAM_SIZE, language=LANGUAGE)

        text_parts = [seg.text.strip() for seg in segments]
        full_text = " ".join(t for t in text_parts if t)

        processing_time = time.time() - t_start
        log.info(f"[/transcribe] '{file.filename}' ({len(raw_bytes)} bytes) -> "
                  f"'{full_text}' in {processing_time:.2f}s")

        return TranscriptionResponse(
            success=True,
            text=full_text,
            language=info.language,
            language_probability=round(info.language_probability, 3) if info.language_probability else None,
            duration_sec=round(info.duration, 3) if info.duration else None,
            processing_time_sec=round(processing_time, 3),
        )

    except Exception as e:
        processing_time = time.time() - t_start
        log.exception("Transcription failed")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "text": "",
                "processing_time_sec": round(processing_time, 3),
                "error": f"{type(e).__name__}: {e}",
            },
        )


# --------------------------------------------------------------------------
# WS /ws/transcribe — streaming (buffer raw PCM16, transcribe on "end")
# --------------------------------------------------------------------------
async def _transcribe_pcm_and_reply(websocket: WebSocket, pcm_bytes: bytes):
    t0 = time.time()

    if not model_ready:
        await websocket.send_json({"success": False, "error": "Model not ready yet"})
        return

    if len(pcm_bytes) < MIN_PCM_BYTES:
        await websocket.send_json({"success": False, "error": "Audio too short (need >= ~100ms)"})
        return

    try:
        # Raw PCM16LE mono -> float32 in [-1, 1], which faster-whisper accepts
        # directly as a numpy array (no WAV header / no ffmpeg decode needed).
        audio_i16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        audio_f32 = audio_i16.astype(np.float32) / 32768.0

        segments, info = model.transcribe(audio_f32, beam_size=BEAM_SIZE, language=LANGUAGE)
        text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())

        processing_time = time.time() - t0
        log.info(f"[/ws/transcribe] {len(pcm_bytes)} bytes -> '{text}' in {processing_time:.2f}s")

        await websocket.send_json({
            "success": True,
            "text": text,
            "language": info.language,
            "duration_sec": round(info.duration, 3) if info.duration else None,
            "processing_time_sec": round(processing_time, 3),
        })
    except Exception as e:
        log.exception("Streaming transcription failed")
        await websocket.send_json({"success": False, "error": f"{type(e).__name__}: {e}"})


@app.websocket("/ws/transcribe")
async def ws_transcribe(websocket: WebSocket):
    await websocket.accept()
    log.info("WebSocket client connected")
    pcm_buffer = bytearray()

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                pcm_buffer.extend(message["bytes"])

            elif message.get("text") is not None:
                try:
                    payload = json.loads(message["text"])
                except json.JSONDecodeError:
                    payload = {}

                event = payload.get("event")
                if event == "end":
                    await _transcribe_pcm_and_reply(websocket, bytes(pcm_buffer))
                    pcm_buffer = bytearray()
                elif event == "reset":
                    pcm_buffer = bytearray()

    except WebSocketDisconnect:
        log.info("WebSocket client disconnected")


# --------------------------------------------------------------------------
# Local LLM (Ollama / Qwen3) — text in, text out
# --------------------------------------------------------------------------
def strip_thinking(text: str) -> str:
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, maxsplit=1, flags=re.IGNORECASE)[1]

    return text.strip()
def ask_qwen(question: str) -> "tuple[str, float]":
    """Send text to the local Ollama model and return (answer, elapsed_sec)."""
    t0 = time.time()
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": OLLAMA_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        "stream": False,
        "think": False,  # ask Ollama to skip Qwen3's chain-of-thought output
    }
    response = requests.post(OLLAMA_CHAT_URL, json=payload, timeout=OLLAMA_TIMEOUT_SEC)
    response.raise_for_status()
    data = response.json()
    raw_answer = data.get("message", {}).get("content", "")
    answer = strip_thinking(raw_answer)
    elapsed = time.time() - t0
    return answer, elapsed


# --------------------------------------------------------------------------
# Local TTS (Piper) — text in, WAV bytes out
# --------------------------------------------------------------------------
def synthesize_speech(text: str) -> "tuple[bytes, float, int]":
    """Return (wav_bytes, elapsed_sec, sample_rate)."""
    t0 = time.time()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        piper_voice.synthesize_wav(text, wav_file)
    elapsed = time.time() - t0
    buffer.seek(0)
    wav_bytes = buffer.read()
    sample_rate = piper_voice.config.sample_rate
    return wav_bytes, elapsed, sample_rate


# --------------------------------------------------------------------------
# Combined pipeline: audio -> ASR -> Qwen -> Piper -> saved answer WAV
# --------------------------------------------------------------------------
def run_full_pipeline(audio_input) -> dict:
    """audio_input: anything faster-whisper's transcribe() accepts —
    a file path, a file-like object (io.BytesIO), or a float32 numpy array.
    """
    timings = {}
    t_total0 = time.time()

    # 1. ASR
    t0 = time.time()
    segments, info = model.transcribe(audio_input, beam_size=BEAM_SIZE, language=LANGUAGE)
    question = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
    timings["asr_sec"] = round(time.time() - t0, 3)

    if not question:
        timings["total_sec"] = round(time.time() - t_total0, 3)
        return {"success": False, "error": "No speech detected", "timings": timings}

    # 2. LLM
    try:
        answer, llm_time = ask_qwen(question)
    except requests.exceptions.ConnectionError:
        timings["total_sec"] = round(time.time() - t_total0, 3)
        return {
            "success": False,
            "question": question,
            "error": f"Could not reach Ollama at {OLLAMA_CHAT_URL}. Is 'ollama serve' running?",
            "timings": timings,
        }
    timings["llm_sec"] = round(llm_time, 3)

    if not answer:
        timings["total_sec"] = round(time.time() - t_total0, 3)
        return {"success": False, "question": question, "error": "LLM returned an empty answer", "timings": timings}

    # 3. TTS
    wav_bytes, tts_time, sample_rate = synthesize_speech(answer)
    timings["tts_sec"] = round(tts_time, 3)

    # Save for the ESP32 (or anyone) to fetch over HTTP
    LATEST_ANSWER_WAV.write_bytes(wav_bytes)

    timings["total_sec"] = round(time.time() - t_total0, 3)

    log.info(
        f"[pipeline] Q='{question}' -> A='{answer}' | "
        f"asr={timings['asr_sec']}s llm={timings['llm_sec']}s "
        f"tts={timings['tts_sec']}s total={timings['total_sec']}s"
    )

    return {
        "success": True,
        "question": question,
        "answer": answer,
        "audio_url": "/answer.wav",
        "sample_rate": sample_rate,
        "timings": timings,
    }


# --------------------------------------------------------------------------
# Dashboard-aware variant of run_full_pipeline().
#
# run_full_pipeline() above is unchanged and still used by /pipeline
# (a plain file upload has no live audience watching progress). This
# version is used by /ws/assistant so the dashboard can show
# "Processing speech" / "Generating response" / "Generating audio" as
# each stage actually starts, instead of only seeing the final result.
# `notify` is an async callable: `await notify("generating_response")`.
# --------------------------------------------------------------------------
async def run_pipeline_with_progress(audio_input, notify) -> dict:
    timings = {}
    t_total0 = time.time()

    await notify("processing_speech")
    t0 = time.time()
    segments, info = model.transcribe(audio_input, beam_size=BEAM_SIZE, language=LANGUAGE)
    question = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
    timings["asr_sec"] = round(time.time() - t0, 3)

    if not question:
        timings["total_sec"] = round(time.time() - t_total0, 3)
        return {"success": False, "error": "No speech detected", "timings": timings}

    await notify("generating_response")
    try:
        answer, llm_time = ask_qwen(question)
    except requests.exceptions.ConnectionError:
        timings["total_sec"] = round(time.time() - t_total0, 3)
        return {
            "success": False,
            "question": question,
            "error": f"Could not reach Ollama at {OLLAMA_CHAT_URL}. Is 'ollama serve' running?",
            "timings": timings,
        }
    timings["llm_sec"] = round(llm_time, 3)

    if not answer:
        timings["total_sec"] = round(time.time() - t_total0, 3)
        return {"success": False, "question": question, "error": "LLM returned an empty answer", "timings": timings}

    await notify("generating_audio")
    wav_bytes, tts_time, sample_rate = synthesize_speech(answer)
    timings["tts_sec"] = round(tts_time, 3)

    LATEST_ANSWER_WAV.write_bytes(wav_bytes)
    timings["total_sec"] = round(time.time() - t_total0, 3)

    log.info(
        f"[pipeline] Q='{question}' -> A='{answer}' | "
        f"asr={timings['asr_sec']}s llm={timings['llm_sec']}s "
        f"tts={timings['tts_sec']}s total={timings['total_sec']}s"
    )

    return {
        "success": True,
        "question": question,
        "answer": answer,
        "audio_url": "/answer.wav",
        "sample_rate": sample_rate,
        "timings": timings,
    }


# --------------------------------------------------------------------------
# Schemas for /ask
# --------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    question: str
    answer: str
    llm_time_sec: float


# --------------------------------------------------------------------------
# POST /ask — text in, text out (test the LLM leg independently)
# --------------------------------------------------------------------------
@app.post("/ask", response_model=None)
def ask(req: AskRequest):
    question = req.question.strip()
    if not question:
        return JSONResponse(status_code=400, content={"error": "Empty question"})

    try:
        answer, llm_time = ask_qwen(question)
    except requests.exceptions.ConnectionError:
        return JSONResponse(
            status_code=503,
            content={"error": f"Could not reach Ollama at {OLLAMA_CHAT_URL}. Is 'ollama serve' running?"},
        )
    except Exception as e:
        log.exception("Ollama call failed")
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})

    if not answer:
        return JSONResponse(status_code=500, content={"error": "LLM returned an empty answer"})

    return AskResponse(question=question, answer=answer, llm_time_sec=round(llm_time, 3))


# --------------------------------------------------------------------------
# POST /pipeline — complete-file upload, full ASR -> LLM -> TTS loop
# (for testing the full loop from a laptop without an ESP32)
# --------------------------------------------------------------------------
@app.post("/pipeline", response_model=None)
async def pipeline_from_file(file: UploadFile = File(...)):
    if not model_ready:
        return JSONResponse(status_code=503, content={"success": False, "error": "ASR model not ready yet"})
    if not piper_ready:
        return JSONResponse(status_code=503, content={"success": False, "error": "Piper TTS voice not ready yet"})

    raw_bytes = await file.read()
    if not raw_bytes:
        return JSONResponse(status_code=400, content={"success": False, "error": "Empty file"})

    try:
        result = run_full_pipeline(io.BytesIO(raw_bytes))
    except Exception as e:
        log.exception("Pipeline failed")
        return JSONResponse(status_code=500, content={"success": False, "error": f"{type(e).__name__}: {e}"})

    return result


# --------------------------------------------------------------------------
# GET /answer.wav — serves the most recently generated TTS answer
# --------------------------------------------------------------------------
@app.get("/answer.wav")
def get_answer_wav():
    if not LATEST_ANSWER_WAV.exists():
        return JSONResponse(status_code=404, content={"error": "No answer has been generated yet"})
    return FileResponse(str(LATEST_ANSWER_WAV), media_type="audio/wav")


# --------------------------------------------------------------------------
# WS /ws/assistant — full voice-assistant loop over WebSocket
# Same wire protocol as /ws/transcribe (binary PCM16 chunks, then
# {"event":"end"}), but runs ASR -> LLM -> TTS and replies with the
# full pipeline result instead of just the transcription.
#
# STAGE 4: this is also the ESP32's control channel now. The dashboard
# never talks to the ESP32 directly — it can't, the ESP32 has no
# listening address — so /ws/dashboard forwards {"command":"start"} /
# {"command":"stop"} over THIS already-open socket instead. The ESP32
# additionally sends {"event":"playback_done"} once MAX98357A finishes
# speaking, purely so the dashboard can show "Ready" again. None of
# this changes what the ESP32 sends for a normal recording (binary PCM
# + {"event":"end"}) or what it gets back (the same JSON as before) —
# the physical BOOT button flow is untouched.
# --------------------------------------------------------------------------
@app.websocket("/ws/assistant")
async def ws_assistant(websocket: WebSocket):
    global esp32_socket, current_state

    await websocket.accept()
    esp32_socket = websocket
    current_state = "ready"
    log.info("[/ws/assistant] ESP32 connected")
    await broadcast_to_dashboards({"type": "esp32_status", "connected": True})
    await broadcast_to_dashboards({"type": "status", "state": "ready"})

    pcm_buffer = bytearray()

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                pcm_buffer.extend(message["bytes"])

            elif message.get("text") is not None:
                try:
                    payload = json.loads(message["text"])
                except json.JSONDecodeError:
                    payload = {}

                event = payload.get("event")

                if event == "end":
                    if not model_ready:
                        result = {"success": False, "error": "ASR model not ready yet"}
                    elif not piper_ready:
                        result = {"success": False, "error": "Piper TTS voice not ready yet"}
                    elif len(pcm_buffer) < MIN_PCM_BYTES:
                        result = {"success": False, "error": "Audio too short (need >= ~100ms)"}
                    else:
                        async def notify(state: str):
                            global current_state
                            current_state = state
                            await broadcast_to_dashboards({"type": "status", "state": state})

                        try:
                            audio_i16 = np.frombuffer(bytes(pcm_buffer), dtype=np.int16)
                            audio_f32 = audio_i16.astype(np.float32) / 32768.0
                            result = await run_pipeline_with_progress(audio_f32, notify)
                        except Exception as e:
                            log.exception("[/ws/assistant] pipeline failed")
                            result = {"success": False, "error": f"{type(e).__name__}: {e}"}

                    pcm_buffer = bytearray()

                    # This send is what the ESP32 has always relied on to
                    # know when to call audio.connecttohost() — unchanged.
                    await websocket.send_json(result)

                    if result.get("success"):
                        current_state = "speaking"
                        await broadcast_to_dashboards({"type": "result", **result})
                        await broadcast_to_dashboards({"type": "status", "state": "speaking"})
                    else:
                        current_state = "ready"
                        await broadcast_to_dashboards(
                            {"type": "error", "message": result.get("error", "Unknown error")}
                        )
                        await broadcast_to_dashboards({"type": "status", "state": "ready"})

                elif event == "reset":
                    pcm_buffer = bytearray()

                elif event == "playback_done":
                    # Sent by the ESP32 once MAX98357A finishes playing the
                    # answer, so the dashboard can leave the "Speaking..."
                    # state and go back to "Ready".
                    current_state = "ready"
                    await broadcast_to_dashboards({"type": "status", "state": "ready"})

    except WebSocketDisconnect:
        pass
    finally:
        log.info("[/ws/assistant] ESP32 disconnected")
        if esp32_socket is websocket:
            esp32_socket = None
        current_state = "esp32_disconnected"
        await broadcast_to_dashboards({"type": "esp32_status", "connected": False})


# --------------------------------------------------------------------------
# WS /ws/dashboard — browser control + status channel (STAGE 4, new)
# --------------------------------------------------------------------------
@app.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket):
    global current_state

    await websocket.accept()
    dashboard_clients.add(websocket)
    log.info("[/ws/dashboard] browser connected")

    # Bring the newly-connected dashboard up to date immediately.
    await websocket.send_json({"type": "esp32_status", "connected": esp32_socket is not None})
    await websocket.send_json({"type": "status", "state": current_state})

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if message.get("text") is not None:
                try:
                    payload = json.loads(message["text"])
                except json.JSONDecodeError:
                    payload = {}

                event = payload.get("event")

                if event == "start":
                    if esp32_socket is None:
                        await broadcast_to_dashboards({"type": "error", "message": "ESP32 is not connected"})
                    else:
                        await esp32_socket.send_json({"command": "start"})
                        # The ESP32 doesn't ack this separately; we optimistically
                        # reflect the new state so the button flips immediately.
                        current_state = "listening"
                        await broadcast_to_dashboards({"type": "status", "state": "listening"})

                elif event == "stop":
                    if esp32_socket is None:
                        await broadcast_to_dashboards({"type": "error", "message": "ESP32 is not connected"})
                    else:
                        await esp32_socket.send_json({"command": "stop"})
                        # Actual processing kicks off once the ESP32 relays
                        # {"event":"end"} back over /ws/assistant.

    except WebSocketDisconnect:
        pass
    finally:
        dashboard_clients.discard(websocket)
        log.info("[/ws/dashboard] browser disconnected")


# --------------------------------------------------------------------------
# GET / — dashboard page (STAGE 4, new)
# --------------------------------------------------------------------------
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ESP32 Voice Assistant</title>
<style>
  :root {
    --bg: #0d0f14;
    --panel: #171a21;
    --panel-border: #262b36;
    --text: #e8eaed;
    --muted: #8b93a3;
    --accent: #4f8cff;
    --accent-dim: #2b3f66;
    --green: #3ddc84;
    --red: #ff5a5a;
    --amber: #ffb648;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    background: radial-gradient(circle at 50% 0%, #161a22 0%, var(--bg) 60%);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    display: flex;
    justify-content: center;
    padding: 24px 16px 48px;
  }
  .app { width: 100%; max-width: 480px; }
  h1 {
    text-align: center;
    font-size: 18px;
    letter-spacing: 0.08em;
    font-weight: 700;
    color: var(--muted);
    margin: 4px 0 28px;
  }

  .mic-wrap { display: flex; flex-direction: column; align-items: center; margin-bottom: 28px; }
  .mic-btn {
    width: 112px; height: 112px; border-radius: 50%;
    border: none; cursor: pointer;
    background: linear-gradient(160deg, var(--accent), #3766c9);
    display: flex; align-items: center; justify-content: center;
    font-size: 44px;
    box-shadow: 0 0 0 0 rgba(79,140,255,0.5);
    transition: transform 0.15s ease, box-shadow 0.2s ease, background 0.2s ease;
  }
  .mic-btn:active { transform: scale(0.96); }
  .mic-btn:disabled { background: #33384a; cursor: not-allowed; opacity: 0.6; }
  .mic-btn.recording {
    background: linear-gradient(160deg, var(--red), #b32d2d);
    animation: pulse 1.4s infinite;
  }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(255, 90, 90, 0.55); }
    70%  { box-shadow: 0 0 0 22px rgba(255, 90, 90, 0); }
    100% { box-shadow: 0 0 0 0 rgba(255, 90, 90, 0); }
  }
  .mic-label { margin-top: 14px; font-size: 15px; color: var(--muted); min-height: 20px; text-align: center; }

  .panel {
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 14px;
    padding: 18px 20px;
    margin-bottom: 16px;
  }
  .panel-title {
    font-size: 11px; letter-spacing: 0.1em; font-weight: 700;
    color: var(--muted); margin-bottom: 8px; text-transform: uppercase;
  }
  .transcript-text { font-size: 17px; line-height: 1.5; min-height: 24px; }
  .transcript-text.placeholder { color: var(--muted); font-style: italic; }

  .status-row { display: flex; align-items: center; gap: 8px; font-size: 14px; margin-bottom: 4px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
  .dot.green { background: var(--green); }
  .dot.red { background: var(--red); }
  .dot.amber { background: var(--amber); animation: blink 1s infinite; }
  @keyframes blink { 50% { opacity: 0.35; } }

  .latency-grid { display: grid; grid-template-columns: 1fr auto; row-gap: 6px; font-size: 14px; }
  .latency-grid .label { color: var(--muted); }
  .latency-grid .value { text-align: right; font-variant-numeric: tabular-nums; }
  .latency-grid .total .label, .latency-grid .total .value { color: var(--text); font-weight: 700; }
  .latency-grid .total { border-top: 1px solid var(--panel-border); margin-top: 4px; padding-top: 6px; }

  .device-row {
    display: flex; justify-content: space-between; align-items: center;
    font-size: 14px; padding: 4px 0;
  }
  .device-row .name { color: var(--text); }
  .device-row .val { display: flex; align-items: center; gap: 6px; color: var(--muted); }

  .error-banner {
    background: #3a1a1a; border: 1px solid #6b2b2b; color: #ff9a9a;
    border-radius: 10px; padding: 10px 14px; font-size: 13px; margin-bottom: 16px;
    display: none;
  }
  .error-banner.show { display: block; }
</style>
</head>
<body>
<div class="app">
  <h1>ESP32 VOICE ASSISTANT</h1>

  <div class="error-banner" id="errorBanner"></div>

  <div class="mic-wrap">
    <button class="mic-btn" id="micBtn">🎙</button>
    <div class="mic-label" id="micLabel">Ready</div>
  </div>

  <div class="panel">
    <div class="panel-title">You said</div>
    <div class="transcript-text placeholder" id="questionText">Nothing yet — click the mic to ask something.</div>
  </div>

  <div class="panel">
    <div class="panel-title">Assistant</div>
    <div class="transcript-text placeholder" id="answerText">Waiting for a question...</div>
  </div>

  <div class="panel">
    <div class="panel-title">Status</div>
    <div class="status-row">
      <span class="dot" id="connDot"></span>
      <span id="connText">Connecting to server...</span>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">Latency</div>
    <div class="latency-grid">
      <div class="label">ASR</div><div class="value" id="latAsr">—</div>
      <div class="label">LLM</div><div class="value" id="latLlm">—</div>
      <div class="label">TTS</div><div class="value" id="latTts">—</div>
      <div class="label total">Total</div><div class="value total" id="latTotal">—</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">Device</div>
    <div class="device-row"><span class="name">ESP32-S3</span><span class="val" id="devEsp32"><span class="dot red"></span>Disconnected</span></div>
    <div class="device-row"><span class="name">INMP441</span><span class="val" id="devMic"><span class="dot red"></span>Unknown</span></div>
    <div class="device-row"><span class="name">MAX98357A</span><span class="val" id="devSpk"><span class="dot red"></span>Unknown</span></div>
  </div>
</div>

<script>
const STATE_LABELS = {
  esp32_disconnected: "ESP32 not connected",
  ready: "Ready — click to speak",
  listening: "Listening...",
  processing_speech: "Processing speech...",
  generating_response: "Generating response...",
  generating_audio: "Generating audio...",
  speaking: "Speaking...",
  error: "Error",
};

const micBtn = document.getElementById('micBtn');
const micLabel = document.getElementById('micLabel');
const questionText = document.getElementById('questionText');
const answerText = document.getElementById('answerText');
const connDot = document.getElementById('connDot');
const connText = document.getElementById('connText');
const errorBanner = document.getElementById('errorBanner');
const devEsp32 = document.getElementById('devEsp32');
const devMic = document.getElementById('devMic');
const devSpk = document.getElementById('devSpk');
const latAsr = document.getElementById('latAsr');
const latLlm = document.getElementById('latLlm');
const latTts = document.getElementById('latTts');
const latTotal = document.getElementById('latTotal');

let ws = null;
let esp32Connected = false;
let currentState = "esp32_disconnected";
let isRecording = false;

function showError(msg) {
  errorBanner.textContent = msg;
  errorBanner.classList.add('show');
  setTimeout(() => errorBanner.classList.remove('show'), 4000);
}

function setDeviceRow(el, ok, textOk, textBad) {
  el.innerHTML = `<span class="dot ${ok ? 'green' : 'red'}"></span>${ok ? textOk : textBad}`;
}

function applyEsp32Status(connected) {
  esp32Connected = connected;
  setDeviceRow(devEsp32, connected, "Connected", "Disconnected");
  // We only know the ESP32's overall link status, not per-peripheral
  // health, so INMP441/MAX98357A mirror it rather than showing
  // independently-verified state.
  setDeviceRow(devMic, connected, "Connected", "Unknown");
  setDeviceRow(devSpk, connected, "Connected", "Unknown");
  updateMicButton();
}

function applyStatus(state) {
  currentState = state;
  isRecording = (state === 'listening');
  micLabel.textContent = STATE_LABELS[state] || state;
  updateMicButton();
}

function updateMicButton() {
  micBtn.classList.toggle('recording', isRecording);
  const busy = ['processing_speech', 'generating_response', 'generating_audio', 'speaking'].includes(currentState);
  micBtn.disabled = !esp32Connected || busy;
}

function applyResult(result) {
  questionText.textContent = result.question || "(no speech detected)";
  questionText.classList.remove('placeholder');
  answerText.textContent = result.answer || "";
  answerText.classList.remove('placeholder');

  const t = result.timings || {};
  latAsr.textContent = t.asr_sec != null ? t.asr_sec.toFixed(2) + ' s' : '—';
  latLlm.textContent = t.llm_sec != null ? t.llm_sec.toFixed(2) + ' s' : '—';
  latTts.textContent = t.tts_sec != null ? t.tts_sec.toFixed(2) + ' s' : '—';
  latTotal.textContent = t.total_sec != null ? t.total_sec.toFixed(2) + ' s' : '—';
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/dashboard`);

  ws.onopen = () => {
    connDot.className = 'dot green';
    connText.textContent = 'Connected to server';
  };

  ws.onclose = () => {
    connDot.className = 'dot red';
    connText.textContent = 'Disconnected from server — retrying...';
    applyEsp32Status(false);
    setTimeout(connect, 2000);
  };

  ws.onerror = () => { ws.close(); };

  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch (e) { return; }

    if (msg.type === 'esp32_status') {
      applyEsp32Status(msg.connected);
    } else if (msg.type === 'status') {
      applyStatus(msg.state);
    } else if (msg.type === 'result') {
      applyResult(msg);
    } else if (msg.type === 'error') {
      showError(msg.message || 'Unknown error');
      applyStatus('error');
    }
  };
}

micBtn.addEventListener('click', () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (!esp32Connected) { showError('ESP32 is not connected'); return; }

  if (!isRecording) {
    ws.send(JSON.stringify({ event: 'start' }));
  } else {
    ws.send(JSON.stringify({ event: 'stop' }));
  }
});

connect();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
