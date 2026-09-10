/*
 * ESP32-S3 Voice Assistant — Stage 3: full loop (no KWS yet)
 * -----------------------------------------------------------------
 * INMP441 mic -> WebSocket -> PC (ASR -> Qwen3 -> Piper TTS) -> HTTP
 * fetch -> ESP32-audioI2S -> MAX98357A speaker
 *
 * TRIGGER (temporary, until KWS is added):
 *   Hold the BOOT button (GPIO0) to record, release to send.
 *   This is a placeholder for the custom wake-word model. The two
 *   functions onWakeTriggered() / onWakeEnded() are the seam where
 *   the KWS callback will plug in later — everything downstream
 *   (I2S capture -> WebSocket streaming -> JSON response -> playback)
 *   does not need to change when that happens.
 *
 * Pins (UNCHANGED from previous stages):
 *   MAX98357A (speaker): BCLK=3  LRC=8  DIN=16
 *   INMP441   (mic):     BCLK=18 WS=17 DOUT=15
 *
 * New libraries required (install via Arduino Library Manager):
 *   - "WebSockets" by Markus Sattler (Links2004)   -> WebSocketsClient.h
 *   - "ArduinoJson" by Benoit Blanchon             -> ArduinoJson.h
 *   (WiFi.h, "ESP_I2S.h" I2SClass, and Audio.h/ESP32-audioI2S are
 *    already part of your existing setup / core.)
 *
 * IMPORTANT: set WS_HOST below to your laptop's IP address (ipconfig).
 */

#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include "ESP_I2S.h"
#include "Audio.h"

// ---------------- Wi-Fi ----------------
const char* ssid     = "NSUT-Campus";
const char* password = "";

const char* WS_HOST = "10.50.40.102";
const uint16_t SERVER_PORT = 8000;
const char* WS_PATH = "/ws/assistant";
// ---------------- Speaker (MAX98357A) — unchanged ----------------
#define SPK_BCLK 3
#define SPK_LRC  8
#define SPK_DOUT 16

// ---------------- Mic (INMP441) ----------------
#define MIC_BCLK 18
#define MIC_WS   17
#define MIC_DIN  15
#define MIC_SAMPLE_RATE 16000

// ---------------- Trigger (temporary stand-in for KWS) ----------------
#define TRIGGER_BUTTON_PIN 0   // BOOT button, active LOW
#define DEBOUNCE_MS 50
#define MAX_RECORD_MS 8000      // safety cap in case a release event is missed

WebSocketsClient webSocket;
Audio audio(1);
I2SClass i2sMic;

// ---------------- State machine ----------------
enum AssistantState { STATE_IDLE, STATE_RECORDING, STATE_WAITING_FOR_ANSWER, STATE_SPEAKING };
volatile AssistantState assistantState = STATE_IDLE;

bool lastButtonReading = HIGH;
bool buttonPressed = false;
unsigned long lastDebounceTime = 0;
unsigned long recordStartTime = 0;

#define MIC_CHUNK_SAMPLES 512
int16_t micChunk[MIC_CHUNK_SAMPLES];

// ---------------- Latency measurement (ESP32 side) ----------------
unsigned long t_recordStart = 0;
unsigned long t_recordEnd = 0;
unsigned long t_answerReceived = 0;

// ==========================================================================
// I2S mic setup
// ==========================================================================
void setupMic() {
  // txData = -1 (unused), rxData = MIC_DIN
  i2sMic.setPins(MIC_BCLK, MIC_WS, -1, MIC_DIN);
  i2sMic.setTimeout(1000);

  // NOTE: INMP441 is wired for the LEFT channel only. MONO slot mode is
  // used here to get 16kHz mono PCM16 directly with no channel-stripping
  // needed on-device. If you get silence or garbled audio, the most
  // likely fix is switching I2S_SLOT_MODE_MONO -> I2S_SLOT_MODE_STEREO
  // below and reading every OTHER int16 sample (the left channel) in
  // captureAndSendChunk() instead.
  if (!i2sMic.begin(I2S_MODE_STD, MIC_SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("ERROR: microphone I2S init failed");
  } else {
    Serial.println("Microphone I2S initialized");
  }
}

// ==========================================================================
// Trigger handling — TEMPORARY manual push-to-talk, stand-in for KWS
// ==========================================================================

// TODO(KWS): replace the call site of onWakeTriggered() (currently the
// button-press branch in checkTriggerButton()) with the custom wake-word
// model's detection callback. onWakeEnded() would similarly be called by
// silence/VAD detection instead of a button release.
void onWakeTriggered() {
  if (assistantState != STATE_IDLE) return;   // ignore while busy
  Serial.println("Recording...");
  assistantState = STATE_RECORDING;
  t_recordStart = millis();
  recordStartTime = t_recordStart;
}

void onWakeEnded() {
  if (assistantState != STATE_RECORDING) return;
  t_recordEnd = millis();
  Serial.print("Recording stopped. Duration: ");
  Serial.print(t_recordEnd - t_recordStart);
  Serial.println(" ms");

  webSocket.sendTXT("{\"event\":\"end\"}");
  assistantState = STATE_WAITING_FOR_ANSWER;
  Serial.println("Waiting for answer from server...");
}

void checkTriggerButton() {
  bool reading = digitalRead(TRIGGER_BUTTON_PIN);

  if (reading != lastButtonReading) {
    lastDebounceTime = millis();
  }

  if ((millis() - lastDebounceTime) > DEBOUNCE_MS) {
    bool isPressedNow = (reading == LOW);
    if (isPressedNow && !buttonPressed) {
      buttonPressed = true;
      onWakeTriggered();
    } else if (!isPressedNow && buttonPressed) {
      buttonPressed = false;
      onWakeEnded();
    }
  }

  lastButtonReading = reading;

  // Safety cap: force-stop recording if it runs too long (e.g. a missed
  // release edge), so the ESP32 never gets stuck streaming forever.
  if (assistantState == STATE_RECORDING && (millis() - recordStartTime > MAX_RECORD_MS)) {
    Serial.println("Max record time reached, stopping.");
    onWakeEnded();
  }
}

// ==========================================================================
// Mic capture -> WebSocket binary frames
// ==========================================================================
void captureAndSendChunk() {
  size_t bytesRead = i2sMic.readBytes((char*)micChunk, sizeof(micChunk));

  if (bytesRead > 0) {

    static int debugCount = 0;

    if (debugCount < 5) {
      Serial.print("MIC samples: ");

      for (int i = 0; i < 10; i++) {
        Serial.print(micChunk[i]);
        Serial.print(" ");
      }

      Serial.println();

      debugCount++;
    }

    webSocket.sendBIN((uint8_t*)micChunk, bytesRead);
  }
}

// ==========================================================================
// WebSocket event handling
// ==========================================================================
void handleAssistantResponse(const char* jsonText, size_t length) {
  t_answerReceived = millis();

  StaticJsonDocument<1024> doc;
  DeserializationError err = deserializeJson(doc, jsonText, length);
  if (err) {
    Serial.print("JSON parse error: ");
    Serial.println(err.c_str());
    assistantState = STATE_IDLE;
    return;
  }

  bool success = doc["success"] | false;
  if (!success) {
    const char* errMsg = doc["error"] | "unknown error";
    Serial.print("Server error: ");
    Serial.println(errMsg);
    assistantState = STATE_IDLE;
    return;
  }

  const char* question = doc["question"] | "";
  const char* answer = doc["answer"] | "";
  const char* audioUrl = doc["audio_url"] | "/answer.wav";

  Serial.println("---- Assistant response ----");
  Serial.print("Question: "); Serial.println(question);
  Serial.print("Answer:   "); Serial.println(answer);

  if (doc.containsKey("timings")) {
    JsonObject timings = doc["timings"];
    Serial.print("ASR time:   "); Serial.print((double)(timings["asr_sec"] | 0.0)); Serial.println(" s");
    Serial.print("LLM time:   "); Serial.print((double)(timings["llm_sec"] | 0.0)); Serial.println(" s");
    Serial.print("TTS time:   "); Serial.print((double)(timings["tts_sec"] | 0.0)); Serial.println(" s");
    Serial.print("Server total: "); Serial.print((double)(timings["total_sec"] | 0.0)); Serial.println(" s");
  }

  Serial.print("Round trip (send-end -> answer received): ");
  Serial.print(t_answerReceived - t_recordEnd);
  Serial.println(" ms  [buffered prototype latency, NOT real-time/incremental]");
  Serial.println("-----------------------------");

  // Build the full URL and hand off to the ALREADY-WORKING ESP32-audioI2S
  // playback path — same mechanism used for connecttospeech() earlier.
 String url = "http://" + String(WS_HOST) + ":" + String(SERVER_PORT) + String(audioUrl);

Serial.print("Fetching and playing: ");
Serial.println(url);

assistantState = STATE_SPEAKING;

bool started = audio.connecttohost(url.c_str());

Serial.print("connecttohost() returned: ");
Serial.println(started ? "true" : "false");

  if (!started) {
    Serial.println("Playback failed to start.");
    assistantState = STATE_IDLE;
  }
}

void webSocketEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {

    case WStype_DISCONNECTED:
      Serial.println("[WS] Disconnected");
      break;

    case WStype_CONNECTED:
      Serial.println("[WS] Connected to /ws/assistant");
      break;

    case WStype_TEXT: {
      StaticJsonDocument<1024> doc;
      DeserializationError err = deserializeJson(doc, payload, length);

      if (err) {
        Serial.print("[WS] JSON parse error: ");
        Serial.println(err.c_str());
        break;
      }

      // Dashboard -> ESP32 commands
      if (doc.containsKey("command")) {
        const char* command = doc["command"];

        if (strcmp(command, "start") == 0) {
          Serial.println("[WS] Dashboard START");
          onWakeTriggered();

        } else if (strcmp(command, "stop") == 0) {
          Serial.println("[WS] Dashboard STOP");
          onWakeEnded();

        } else {
          Serial.print("[WS] Unknown command: ");
          Serial.println(command);
        }

        break;
      }

      // Server -> ESP32 assistant response
      if (doc.containsKey("success")) {
        handleAssistantResponse((const char*)payload, length);
        break;
      }

      Serial.println("[WS] Unknown JSON message");
      break;
    }

    case WStype_ERROR:
      Serial.println("[WS] Error");
      break;

    default:
      break;
  }
}


// ==========================================================================
// ESP32-audioI2S callback — fires when playback of the answer finishes
// ==========================================================================
void audio_eof_stream(const char *info) {
  Serial.println("Playback finished. Ready for next question.");
  webSocket.sendTXT("{\"event\":\"playback_done\"}");
  assistantState = STATE_IDLE;
}
void audio_info(const char *info) {
  Serial.print("[audio_info] ");
  Serial.println(info);
}

// ==========================================================================
// Setup
// ==========================================================================
void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(TRIGGER_BUTTON_PIN, INPUT_PULLUP);

  Serial.println();
  Serial.println("WiFi connecting...");
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  unsigned long startAttempt = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print(".");
    if (millis() - startAttempt > 20000) {
      Serial.println();
      Serial.println("WiFi connect timeout, retrying...");
      WiFi.disconnect();
      WiFi.begin(ssid, password);
      startAttempt = millis();
    }
  }
  Serial.println();
  Serial.println("WiFi connected");
  Serial.print("IP address: ");
  Serial.println(WiFi.localIP());

  // Existing, unchanged speaker setup
  audio.setPinout(SPK_BCLK, SPK_LRC, SPK_DOUT);
  audio.setVolume(18);

  setupMic();

  Serial.print("Connecting WebSocket to ws://");
  Serial.print(WS_HOST);
  Serial.print(":");
  Serial.print(SERVER_PORT);
  Serial.println(WS_PATH);
  webSocket.begin(WS_HOST, SERVER_PORT, WS_PATH);
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(3000);

  Serial.println("Ready. Hold the BOOT button to ask a question.");
}

// ==========================================================================
// Loop — nothing here may use delay(); everything is polled every pass
// ==========================================================================
void loop() {
  webSocket.loop();   // must run continuously for WS to work
  audio.loop();        // must run continuously for playback to work

  checkTriggerButton();

  if (assistantState == STATE_RECORDING) {
    captureAndSendChunk();
  }
}
