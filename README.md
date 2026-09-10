# Low-Latency and Efficient Voice Activator for Edge Devices

> **Smart India Hackathon 2026 — Problem Statement ID: 26172**  
> **Category:** Software / Embedded Systems  
> **Theme:** Space Technology / Edge AI

A low-latency, privacy-conscious voice assistant designed for **resource-constrained edge devices**, combining an **ESP32-S3** with a locally hosted AI backend for speech recognition, language processing, and speech synthesis.

---

## 🚀 Project Overview

This project implements a **hybrid edge-host voice assistant architecture** designed for environments where low latency, privacy, reduced bandwidth usage, and local processing are important.

The system divides the workload between two major tiers:

### Edge Device — ESP32-S3

The ESP32-S3 is responsible for:

- Capturing voice input through an **INMP441 I2S microphone**
- Sampling audio at **16 kHz, mono, PCM16**
- Streaming audio data to the host backend through **WebSockets**
- Receiving generated response information
- Fetching synthesized audio through HTTP
- Playing the response through a **MAX98357A amplifier and speaker**

### Local Host — AI Backend

The host system performs the computationally intensive tasks using:

- **faster-whisper** for Speech-to-Text
- **Ollama with Qwen3-4B** for language processing and response generation
- **Piper TTS** for Text-to-Speech synthesis
- **FastAPI** for backend services and communication

The core AI processing is designed to run locally, avoiding mandatory dependence on cloud-based AI services.

---

## 🎯 Problem Statement

Running large Speech-to-Text and Generative AI models directly on resource-constrained microcontrollers such as the ESP32-S3 is computationally challenging.

At the same time, continuously sending voice data to cloud infrastructure introduces several limitations:

- **Privacy risks** associated with transmitting voice data
- **Internet dependency**
- **Bandwidth overhead**
- **Network-induced latency**
- **Reduced reliability in remote environments**
- **Dependence on external cloud services**

For applications involving remote, tactical, embedded, or space-oriented environments, these limitations can become significant.

---

## 💡 Proposed Solution

The proposed solution uses a **two-tier edge-host architecture**.

Instead of running large AI models directly on the ESP32-S3, the edge device handles lightweight audio operations while a local host performs the computationally expensive AI inference.

### High-Level Flow

```text
User Voice
    │
    ▼
ESP32-S3 + INMP441
    │
    │ WebSocket Audio Stream
    ▼
FastAPI Backend
    │
    ├──► faster-whisper
    │        │
    │        ▼
    │   Transcription
    │
    ├──► Ollama + Qwen3-4B
    │        │
    │        ▼
    │   Response Generation
    │
    └──► Piper TTS
             │
             ▼
          answer.wav
             │
             │ HTTP
             ▼
        ESP32-S3
             │
             ▼
       MAX98357A
             │
             ▼
          Speaker
```

For the complete technical architecture, communication flow, and component-level details, see:

**[Architecture Documentation](docs/architecture.md)**

---

## ✨ Key Features

### 🎙️ Edge Audio Capture

- ESP32-S3 based audio capture
- INMP441 digital I2S microphone
- 16 kHz sampling
- Mono PCM16 audio
- Edge-side audio handling

### ⚡ Low-Latency Communication

- WebSocket-based communication
- Binary audio frame transmission
- Local AI inference
- HTTP-based synthesized audio delivery

### 🤖 Local AI Pipeline

The project uses a fully local AI processing pipeline consisting of:

| Task | Technology |
|---|---|
| Speech-to-Text | faster-whisper |
| Language Processing | Qwen3-4B |
| LLM Runtime | Ollama |
| Text-to-Speech | Piper TTS |
| Backend | FastAPI |

### 🔊 Local Audio Playback

The generated speech is returned to the ESP32-S3 as WAV audio and played through the audio output hardware.

```text
ESP32-S3
    ↓
MAX98357A
    ↓
Speaker
```

### 🧠 Wake-Word Capability

The repository also contains a dedicated wake-word implementation under:

```text
hey_bitsy_wakeword/
```

This provides the foundation for hands-free activation on the edge device.

### 📊 Dashboard Interface

The FastAPI backend includes a browser-based interface for interacting with the system and observing relevant system activity and telemetry.

---

## 🧰 Technology Stack

### Hardware

- **ESP32-S3**
- **INMP441 I2S Microphone**
- **MAX98357A Audio Amplifier**
- Speaker

### Firmware / Embedded

- Arduino IDE
- ESP32-S3
- I2S
- ESP32-audioI2S
- WebSockets
- ArduinoJson

### Backend

- Python
- FastAPI
- Uvicorn
- Python-Multipart

### AI / ML

- faster-whisper
- Ollama
- Qwen3-4B
- Piper TTS
- `en_US-lessac-medium` voice

### Edge AI / Wake Word

- Edge Impulse generated inference resources
- ESP32-S3 wake-word implementation

---

## 📁 Project Structure

```text
Low-Latency-Efficient-Voice-Activator-for-Edge-Devices/
│
├── asr-model/
│   ├── answers/
│   ├── audio/
│   ├── esp32_voice_assistant/
│   ├── piper_voices/
│   ├── Modelfile
│   ├── Modelfile.txt
│   ├── README.md
│   ├── README.txt
│   ├── requirements.txt
│   ├── server.py
│   ├── test_ask.py
│   └── test_client.py
│
├── docs/
│   └── architecture.md
│
├── hey_bitsy_wakeword/
│   ├── ei-rishikeshsinha7091-project-1-arduino-1.0.5-impulse-#1/
│   │   └── ...
│   └── hey_bitsy_wakeword.ino
│
├── submission/
│   ├── DEMO.md
│   ├── PRESENTATION.md
│   └── SIH2026_CodeCrusaders_26172.pptx
│
├── .gitignore
├── LICENSE
├── README.md
└── requirements.txt
```

### 📌 Directory & File Guide

| Path | Purpose |
|---|---|
| `asr-model/` | Main local voice-assistant and ASR-related resources |
| `asr-model/server.py` | Backend server for the voice-assistant pipeline |
| `asr-model/test_ask.py` | Assistant request testing |
| `asr-model/test_client.py` | Backend/client communication testing |
| `asr-model/esp32_voice_assistant/` | ESP32 voice-assistant resources |
| `asr-model/audio/` | Audio resources used by the project |
| `asr-model/answers/` | Generated assistant response resources |
| `asr-model/piper_voices/` | Piper TTS voice resources |
| `asr-model/Modelfile` | Local LLM model configuration |
| `docs/` | Project technical documentation |
| `docs/architecture.md` | Detailed system architecture and technical design |
| `hey_bitsy_wakeword/` | Wake-word and edge-device implementation |
| `hey_bitsy_wakeword/hey_bitsy_wakeword.ino` | Arduino firmware for the wake-word implementation |
| `hey_bitsy_wakeword/ei-.../` | Edge Impulse generated inference resources |
| `submission/` | Smart India Hackathon submission material |
| `submission/DEMO.md` | Demonstration video information |
| `submission/PRESENTATION.md` | Presentation documentation |
| `submission/SIH2026_CodeCrusaders_26172.pptx` | Final SIH presentation |
| `requirements.txt` | Root-level Python dependencies |
| `.gitignore` | Files and directories excluded from version control |
| `LICENSE` | Project license |
| `README.md` | Main project documentation |

> **Note:** Large model files, generated resources, dependencies, build files, and internal library files are intentionally not expanded individually in this project overview. The structure above focuses on the major project components.

---

## 🔄 Voice Assistant Workflow

The complete interaction can be summarized as:

```text
1. User activates the system
          ↓
2. ESP32-S3 captures microphone audio
          ↓
3. Audio is transmitted through WebSocket
          ↓
4. FastAPI backend receives the audio
          ↓
5. faster-whisper converts speech to text
          ↓
6. Qwen3-4B processes the request
          ↓
7. Generated response is passed to Piper TTS
          ↓
8. Piper generates WAV audio
          ↓
9. ESP32-S3 retrieves the generated audio
          ↓
10. MAX98357A drives the speaker
          ↓
11. User hears the response
```

---

## ⚙️ Installation

### Prerequisites

Before running the project, make sure the required software and hardware environment is available.

#### Software

- Python
- Arduino IDE
- Ollama
- ESP32-S3 board support for Arduino IDE

#### Hardware

- ESP32-S3
- INMP441 microphone
- MAX98357A amplifier
- Speaker

---

### 1. Clone the Repository

```bash
git clone https://github.com/Aditya-sync/Low-Latency-Efficient-Voice-Activator-for-Edge-Devices.git

cd Low-Latency-Efficient-Voice-Activator-for-Edge-Devices
```

---

### 2. Create a Python Virtual Environment

```bash
python -m venv venv
```

#### Linux / macOS

```bash
source venv/bin/activate
```

#### Windows PowerShell

```powershell
venv\Scripts\Activate.ps1
```

---

### 3. Install Python Dependencies

From the project root:

```bash
pip install -r requirements.txt
```

---

## 🔊 Piper TTS Setup

Download the required Piper voice model:

```bash
python -m piper.download_voices en_US-lessac-medium --data-dir piper_voices
```

The voice resources should be available in:

```text
piper_voices/
```

---

## 🧠 Ollama Setup

Install Ollama and ensure that the required **Qwen3** model is available locally.

Start the Ollama service:

```bash
ollama serve
```

Make sure the required Qwen3 model is configured before starting the backend.

---

## ▶️ Running the Backend

The main backend implementation is located at:

```text
asr-model/server.py
```

Start the backend using:

```bash
python server.py
```

Run the command from the directory containing `server.py`.

The backend provides the services required for:

- Audio processing
- Speech recognition
- LLM interaction
- Text-to-Speech
- WebSocket communication
- Generated audio delivery
- Dashboard interaction

---

## 🔌 ESP32-S3 Setup

The ESP32 voice-assistant resources are located under:

```text
asr-model/esp32_voice_assistant/
```

The wake-word implementation is located under:

```text
hey_bitsy_wakeword/
```

Open the appropriate Arduino sketch using **Arduino IDE**.

Before flashing the firmware, configure the required:

- Wi-Fi credentials
- Backend/server address
- I2S configuration
- Audio hardware configuration
- Device-specific settings

Install the required Arduino libraries and ESP32 board support before compiling.

---

## 🧪 Testing

The repository contains dedicated testing scripts for the local voice-assistant backend.

### Assistant Request Test

```bash
python test_ask.py
```

### Client / Communication Test

```bash
python test_client.py
```

These scripts allow the backend pipeline to be tested independently of the physical ESP32 hardware.

---

## 🖥️ Dashboard

The FastAPI backend includes a browser-based dashboard for interacting with the voice-assistant system.

The dashboard can be used for:

- Sending requests
- Triggering assistant interactions
- Observing system activity
- Monitoring relevant timing/telemetry information

---

## 🔐 Privacy & Security

A key objective of this project is to minimize dependence on cloud infrastructure.

The core processing pipeline is designed to run locally:

```text
Audio
  ↓
Local Speech Recognition
  ↓
Local LLM
  ↓
Local Text-to-Speech
  ↓
Local Audio Playback
```

This approach helps reduce the need to transmit voice data to external AI services.

### ⚠️ Before Pushing to GitHub

Do **not** commit sensitive or machine-specific information.

Make sure the repository does not contain:

- API keys
- Passwords
- Authentication tokens
- Wi-Fi credentials
- `.env` files
- Private configuration files
- Machine-specific paths
- Unnecessary generated files
- Temporary files

Use `.gitignore` to exclude sensitive, generated, and environment-specific files.

---

## 📚 Documentation

Detailed technical information is available in:

### Architecture

**[docs/architecture.md](docs/architecture.md)**

The architecture documentation contains the deeper technical explanation of the system, including its components, processing pipeline, communication flow, and architecture.

---

## 🖼️ Project Assets

Project-related screenshots, prototype images, wiring diagrams, and other visual material can be organized within the project's documentation/assets as applicable.

---

## 🎥 Demo

The project demonstration video information is available in:

**[submission/DEMO.md](submission/DEMO.md)**

The demo showcases the end-to-end voice assistant workflow.

---

## 📊 Presentation

The Smart India Hackathon presentation material is available under:

```text
submission/
├── PRESENTATION.md
└── SIH2026_CodeCrusaders_26172.pptx
```

The presentation contains the project's problem statement, proposed solution, architecture, implementation, and other SIH-related material.

---

## 🔮 Future Scope

### 1. On-Device Keyword Spotting

Integrate a lightweight TinyML wake-word model directly onto the ESP32-S3.

This would allow the system to transition from manual activation toward hands-free voice interaction.

### 2. Streaming Asynchronous ASR

Move from buffered audio processing toward incremental speech recognition.

This can reduce the user's perceived response latency by allowing transcription to begin while audio is still being captured.

### 3. Hardware Multi-Controller Optimization

Optimize ESP32-S3 and I2S resource usage to provide additional processing headroom for auxiliary sensors and other embedded functionality.

### 4. Expanded Edge Intelligence

Future versions can introduce additional lightweight edge models for:

- Wake-word detection
- Basic command recognition
- Intent classification
- Emergency command detection

This would allow more processing to happen directly on the edge device.

---

## 📈 Design Goals

The project is designed around the following principles:

| Goal | Approach |
|---|---|
| Low Latency | Local inference and WebSocket communication |
| Privacy | Local AI processing |
| Edge Efficiency | Lightweight ESP32-S3 responsibilities |
| Reduced Bandwidth | Event-based audio transmission |
| Modularity | Separate edge and host processing |
| Offline Capability | Local ASR, LLM, and TTS |
| Scalability | Modular hardware and software architecture |

---

## 🏆 Smart India Hackathon 2026

| | |
|---|---|
| **Problem Statement ID** | 26172 |
| **Project** | Low-Latency and Efficient Voice Activator for Edge Devices |
| **Category** | Software / Embedded Systems |
| **Theme** | Space Technology / Edge AI |

---

## 📜 License

This project is licensed under the terms specified in the [`LICENSE`](LICENSE) file.

Please refer to the license for information regarding the use, modification, and distribution of this project.

---

## 👥 Project

**Low-Latency and Efficient Voice Activator for Edge Devices**

Developed for **Smart India Hackathon 2026**.

> **Edge AI + Local Intelligence + Low-Latency Voice Interaction 🚀**