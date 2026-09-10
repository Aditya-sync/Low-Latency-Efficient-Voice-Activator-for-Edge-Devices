/* ==========================================================================
 * Hey Bitsy — Low Latency Wake Word Detection on Edge
 * Smart India Hackathon 2026 | PS SIH26172 (ISRO)
 *
 * Custom-trained wake word runs fully on-device. No cloud, no internet,
 * no pre-trained model. Saying "Hey Bitsy" toggles an LED on GPIO 7.
 *
 * HARDWARE
 *   Board : 7Semi ESP32-S3-WROOM-1 N16R8 (16MB flash / 8MB OPI PSRAM)
 *   Mic   : INMP441 I2S MEMS microphone
 *   Out   : LED on GPIO 7 via 220R resistor to GND
 *
 * WIRING
 *   INMP441 VDD -> 3V3        (3.3V ONLY — 5V will kill the mic)
 *   INMP441 GND -> GND
 *   INMP441 L/R -> GND        (grounding L/R selects the LEFT channel,
 *                              which is why I2S is set to ONLY_LEFT below)
 *   INMP441 SCK -> GPIO 4     (bit clock)
 *   INMP441 WS  -> GPIO 5     (word select / LR clock)
 *   INMP441 SD  -> GPIO 6     (serial data out of mic, into ESP32)
 *   LED anode   -> GPIO 7
 *   LED cathode -> 220R -> GND
 *
 *   Avoid GPIO 26-37 entirely on the N16R8. Those pins are bonded to the
 *   internal OPI PSRAM. Using them for peripherals corrupts memory in ways
 *   that look like random crashes rather than a pin fault.
 *
 * ARDUINO IDE BOARD SETTINGS (Tools menu — all of these matter)
 *   Board            : ESP32S3 Dev Module
 *   USB CDC On Boot  : Enabled          <- without this, Serial prints nothing
 *   CPU Frequency    : 240MHz (WiFi)
 *   Flash Mode       : QIO 80MHz
 *   Flash Size       : 16MB (128Mb)
 *   Partition Scheme : Huge APP (3MB No OTA/1MB SPIFFS)  <- else "sketch too big"
 *   PSRAM            : OPI PSRAM        <- QSPI or Disabled will not work
 *   Upload Speed     : 921600
 *
 *   Upload through the UART USB-C port (left), not the native USB port.
 *
 * MODEL SETUP
 *   1. Download the Edge Impulse Arduino library .zip (linked in the repo).
 *   2. Remove any previously installed Edge Impulse library from
 *      Documents\Arduino\libraries\ first. Two copies of the SDK produce a
 *      wall of "multiple definition" linker errors that name the wrong file.
 *   3. Sketch -> Include Library -> Add .ZIP Library -> select the zip.
 *   4. Apply the two arena patches described in ARENA NOTES below.
 *
 * ARENA NOTES (required — the stock export crashes on this board)
 *   The EON-compiled model ships with a tensor arena sized for a smaller
 *   target. On boot it fails to place its persistent buffers and aborts with:
 *      "does not fit in tensor arena and reached EI_MAX_OVERFLOW_BUFFER_COUNT"
 *
 *   Two edits inside the installed library fix it:
 *
 *   (a) src/model-parameters/model_metadata.h
 *         #define EI_CLASSIFIER_TFLITE_LARGEST_ARENA_SIZE   524288
 *       (default is ~162284)
 *
 *   (b) src/tflite-model/tflite_learn_*_compiled.cpp, near line 86.
 *       Replace the #ifndef guard so the value cannot be overridden by an
 *       earlier definition:
 *         #undef  EI_MAX_OVERFLOW_BUFFER_COUNT
 *         #define EI_MAX_OVERFLOW_BUFFER_COUNT 40
 *
 *   If the arena size still prints correctly at boot but the overflow error
 *   persists, the EON model is allocating from its own local arena constant
 *   rather than the metadata one. Search the compiled .cpp for "kTensorArena"
 *   or "arena[" and raise that constant instead.
 *
 *   After editing library files, clear the build cache or the IDE silently
 *   reuses the previous binary:
 *     Remove-Item "$env:LOCALAPPDATA\arduino\sketches" -Recurse -Force
 *   The "ELF file SHA256" line printed on crash confirms this — if the hash
 *   is unchanged between uploads, the edits were never compiled in.
 *
 * VERIFYING A GOOD RUN
 *   Open Serial Monitor at 115200 and press RST. The boot banner should show
 *   the patched arena size, 8388608 bytes of PSRAM, and the three model
 *   labels. After "Listening..." a score line prints roughly every 250 ms.
 *   The first full window is skipped while the ring buffer primes.
 * ========================================================================== */

#include <rishikeshsinha7091-project-1_inferencing.h>
#include <driver/i2s.h>

/* --------------------------------------------------------------------------
 * Pin map
 * ------------------------------------------------------------------------ */
#define I2S_SCK_PIN   4
#define I2S_WS_PIN    5
#define I2S_SD_PIN    6
#define LED_PIN       7

/* --------------------------------------------------------------------------
 * Tuning constants — these are the knobs worth touching
 * ------------------------------------------------------------------------ */

// Label string must match the Edge Impulse output class exactly, including
// case. The boot banner prints every label so this can be checked on device.
#define WAKE_LABEL            "hey_bitsy"

// The INMP441 pushes 24-bit samples inside a 32-bit frame. The classifier
// wants 16-bit. Shifting right by 14 keeps the useful bits and lands the
// signal at roughly the level the training samples were recorded at.
// Lower value = louder. Raise to 15/16 if loud speech clips, drop to 12/13
// if the wake score never rises above the noise floor.
#define GAIN_SHIFT            14

// Minimum confidence before a detection counts. 0.80 is a deliberately
// conservative starting point: false triggers are far more annoying in a
// live demo than the occasional missed word. Drop toward 0.65 if the model
// is consistently peaking in the 0.7 range.
#define CONFIDENCE_THRESHOLD  0.80f

// A single utterance spans several inference windows, so without a lockout
// one "Hey Bitsy" toggles the LED two or three times and appears to do
// nothing. 1500 ms is long enough to cover the tail of the word.
#define COOLDOWN_MS           1500

/* --------------------------------------------------------------------------
 * Audio ring buffer shared between the two cores
 * ------------------------------------------------------------------------ */
typedef struct {
  int16_t          *buffer;      // one slice worth of 16-bit samples
  uint32_t          buf_count;   // write cursor
  uint32_t          n_samples;   // slice length, set from the model
  volatile uint8_t  buf_ready;   // set by core 0, cleared by core 1
} inference_t;

static inference_t   inference;
static bool          debug_nn        = false;  // true dumps DSP features
static bool          led_state       = false;  // current LED state
static unsigned long last_trigger_ms = 0;      // timestamp of last accepted hit
static int           slices_seen     = 0;      // counts up to one full window

/* --------------------------------------------------------------------------
 * I2S peripheral setup
 *
 * Sample rate is pulled from the model rather than hardcoded, so a retrained
 * model at a different rate stays in sync automatically.
 * ------------------------------------------------------------------------ */
static void i2s_init() {
  i2s_config_t cfg = {
    .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate          = EI_CLASSIFIER_FREQUENCY,
    .bits_per_sample      = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT,  // matches L/R -> GND
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count        = 8,      // 8 x 512 frames of headroom keeps the
    .dma_buf_len          = 512,    // capture task from ever dropping audio
    .use_apll             = false,
    .tx_desc_auto_clear   = false,
    .fixed_mclk           = 0
  };

  i2s_pin_config_t pins = {
    .mck_io_num   = I2S_PIN_NO_CHANGE,   // INMP441 needs no master clock
    .bck_io_num   = I2S_SCK_PIN,
    .ws_io_num    = I2S_WS_PIN,
    .data_out_num = I2S_PIN_NO_CHANGE,   // receive only
    .data_in_num  = I2S_SD_PIN
  };

  i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_0, &pins);
  i2s_zero_dma_buffer(I2S_NUM_0);
}

/* --------------------------------------------------------------------------
 * Capture task — pinned to core 0
 *
 * Inference takes long enough that doing capture and inference on the same
 * core drops audio mid-word and tanks accuracy. Core 0 does nothing but
 * drain the I2S DMA buffer; core 1 runs the classifier. This split is what
 * keeps end-to-end latency low enough to feel instant.
 * ------------------------------------------------------------------------ */
static void capture_task(void *arg) {
  const size_t chunk = 512;
  int32_t *raw = (int32_t *)malloc(chunk * sizeof(int32_t));

  if (raw == NULL) {
    Serial.println("ERROR: capture task allocation failed");
    vTaskDelete(NULL);
  }

  while (true) {
    size_t bytes_read = 0;

    // Blocks until the DMA buffer has data, so this loop costs no CPU while
    // idle and never needs a delay().
    i2s_read(I2S_NUM_0, raw, chunk * sizeof(int32_t), &bytes_read, portMAX_DELAY);

    int n = bytes_read / sizeof(int32_t);
    for (int i = 0; i < n; i++) {
      inference.buffer[inference.buf_count++] = (int16_t)(raw[i] >> GAIN_SHIFT);

      if (inference.buf_count >= inference.n_samples) {
        inference.buf_count = 0;
        inference.buf_ready = 1;   // hand this slice to core 1
      }
    }
  }
}

/* --------------------------------------------------------------------------
 * Callback the Edge Impulse SDK uses to pull audio out of the ring buffer.
 * It expects normalised floats, so int16 is converted on the way out.
 * ------------------------------------------------------------------------ */
static int microphone_audio_signal_get_data(size_t offset, size_t length, float *out_ptr) {
  numpy::int16_to_float(&inference.buffer[offset], out_ptr, length);
  return 0;
}

void setup() {
  Serial.begin(115200);
  delay(2000);                    // gives the USB CDC port time to enumerate

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  // Two blinks on boot. Confirms the LED is wired correctly before any of
  // the audio path is involved, which separates wiring faults from model
  // faults during bring-up.
  for (int i = 0; i < 2; i++) {
    digitalWrite(LED_PIN, HIGH); delay(150);
    digitalWrite(LED_PIN, LOW);  delay(150);
  }

  // Boot banner. Arena size and PSRAM are printed because the two most
  // common failures on this board are an unpatched arena and PSRAM set to
  // the wrong mode in Tools. Both are visible here in one line each.
  Serial.println("=== Hey Bitsy — wake word detection ===");
  Serial.printf("Arena size  : %d bytes\n", EI_CLASSIFIER_TFLITE_LARGEST_ARENA_SIZE);
  Serial.printf("PSRAM found : %d bytes\n", ESP.getPsramSize());
  Serial.printf("Free heap   : %d bytes\n", ESP.getFreeHeap());
  Serial.printf("Sample rate : %d Hz\n", EI_CLASSIFIER_FREQUENCY);
  Serial.printf("Window      : %d ms\n",
                EI_CLASSIFIER_RAW_SAMPLE_COUNT * 1000 / EI_CLASSIFIER_FREQUENCY);
  Serial.printf("Slice size  : %d samples\n", EI_CLASSIFIER_SLICE_SIZE);

  Serial.println("Model labels:");
  for (int i = 0; i < EI_CLASSIFIER_LABEL_COUNT; i++) {
    Serial.printf("  [%d] %s\n", i, ei_classifier_inferencing_categories[i]);
  }
  Serial.printf("Listening for: \"%s\"\n\n", WAKE_LABEL);

  // Slice size comes from the model so the buffer always matches whatever
  // window and stride the impulse was designed with.
  inference.n_samples = EI_CLASSIFIER_SLICE_SIZE;
  inference.buffer    = (int16_t *)malloc(inference.n_samples * sizeof(int16_t));

  if (inference.buffer == NULL) {
    Serial.println("ERROR: audio buffer allocation failed — halting");
    while (1) delay(1000);
  }

  inference.buf_count = 0;
  inference.buf_ready = 0;

  i2s_init();
  run_classifier_init();   // sets up the rolling feature matrix

  // Priority 5 keeps capture above the idle task without starving the
  // system tasks that also live on core 0.
  xTaskCreatePinnedToCore(capture_task, "capture", 4096, NULL, 5, NULL, 0);

  Serial.println("Listening...\n");
}

void loop() {
  // Wait for core 0 to fill a slice. delay(1) yields to the scheduler
  // instead of spinning, which matters because a busy-wait here would
  // starve the WiFi and system tasks sharing this core.
  while (inference.buf_ready == 0) delay(1);
  inference.buf_ready = 0;

  signal_t signal;
  signal.total_length = EI_CLASSIFIER_SLICE_SIZE;
  signal.get_data     = &microphone_audio_signal_get_data;

  // run_classifier_continuous keeps a rolling feature matrix across calls,
  // so only the newest slice is processed each time rather than re-running
  // DSP over a full second of audio. This is the main reason inference
  // stays fast enough for a responsive wake word.
  ei_impulse_result_t result = { 0 };
  EI_IMPULSE_ERROR r = run_classifier_continuous(&signal, &result, debug_nn);

  if (r != EI_IMPULSE_OK) {
    Serial.printf("Classifier error (%d)\n", r);
    return;
  }

  // The feature matrix is only partially filled until enough slices have
  // arrived to cover one full window. Classifying before that produces
  // garbage scores and spurious triggers on startup.
  if (++slices_seen < EI_CLASSIFIER_SLICES_PER_MODEL_WINDOW) return;

  // Pull out both the wake word score and the overall winner. Requiring the
  // wake word to actually win — not just clear the threshold — rejects the
  // case where two classes both score high and the wake word is the weaker
  // of them.
  float       wake_score = 0.0f;
  float       best_score = 0.0f;
  const char *best_label = "";

  for (size_t ix = 0; ix < EI_CLASSIFIER_LABEL_COUNT; ix++) {
    float v = result.classification[ix].value;

    if (strcmp(result.classification[ix].label, WAKE_LABEL) == 0) {
      wake_score = v;
    }
    if (v > best_score) {
      best_score = v;
      best_label = result.classification[ix].label;
    }
  }

  // Continuous score output. Useful during tuning: watch the wake value
  // while speaking to decide whether the threshold or the gain needs
  // adjusting. Comment out for a quieter demo.
  Serial.printf("%-10s %.2f   (wake %.2f)\n", best_label, best_score, wake_score);

  bool is_wake = (wake_score >= CONFIDENCE_THRESHOLD) &&
                 (strcmp(best_label, WAKE_LABEL) == 0);

  if (is_wake && (millis() - last_trigger_ms > COOLDOWN_MS)) {
    last_trigger_ms = millis();

    led_state = !led_state;
    digitalWrite(LED_PIN, led_state ? HIGH : LOW);

    Serial.printf(">>> WAKE WORD DETECTED — LED %s\n", led_state ? "ON" : "OFF");
  }
}
