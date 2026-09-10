# Project Presentation

* **Presentation File:** [Download PowerPoint Deck](./SIH2026_CodeCrusaders_26172.pptx)

## Project Overview
* **Title:** Low Latency and Efficient Voice Activator for Edge Devices (Hey Bitsy)[cite: 2]
* **Problem Statement ID:** 26172 (ISRO / Space Technology)[cite: 1, 2]
* **Team:** Code Crusaders[cite: 2]

## Core Architecture
* **Model 1 (Edge):** ESP32-S3 microcontroller running INT8 Conv1D keyword spotting on MFCC features (50 ms average inference, 177 KB peak SRAM)[cite: 1, 2].
* **Model 2 (Host):** Persistent WebSocket streaming to a local FastAPI server running `faster-whisper`, Qwen3 LLM, and Piper TTS for spoken responses[cite: 1, 2].