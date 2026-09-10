# Low-Latency and Efficient Voice Activator for Edge Devices

> **Smart India Hackathon 2026 — Problem Statement ID: 26172**  
> **Category:** Software / Embedded Systems  
> **Theme:** Space Technology / Edge AI

A low-latency, privacy-preserving, two-tier voice assistant designed for resource-constrained edge devices such as the **ESP32-S3**, paired with a local host backend for AI inference.

---

## 1. Project Overview

This project implements a **hybrid edge-host voice assistant architecture** designed to provide fast and efficient voice interaction without relying on cloud-based AI services.

The system uses an **ESP32-S3** as the edge device for capturing and playing audio, while computationally intensive AI tasks are handled by a local host running:

- `faster-whisper` for Speech-to-Text (ASR)
- Ollama with `Qwen3-4B` for language understanding and response generation
- `Piper TTS` for Text-to-Speech synthesis

The communication between the ESP32-S3 and the host backend is performed using **WebSockets**, enabling efficient real-time audio transfer.

---

## 2. Problem Statement

Running large Speech-to-Text and Generative AI models entirely on resource-constrained microcontrollers is computationally infeasible.

On the other hand, continuously sending audio to cloud infrastructure introduces several challenges:

- Privacy and data-security concerns
- Network dependency
- Bandwidth overhead
- Increased latency
- Reduced reliability in remote or tactical environments
- Dependence on external cloud services

These limitations make conventional cloud-based voice assistants unsuitable for certain **edge, remote, and space-oriented applications**.

---

## 3. Proposed Solution

The proposed system uses a **two-tier edge-host architecture**.

### Edge Tier — ESP32-S3

The ESP32-S3 is responsible for:

- Capturing audio from an **INMP441 I2S microphone**
- Sampling audio at **16 kHz, mono, PCM16**
- Streaming audio data to the host using WebSockets
- Receiving generated response information
- Fetching synthesized WAV audio over HTTP
- Playing the response through a **MAX98357A amplifier and speaker**

### Host Tier — Local AI Backend

The local backend is responsible for:

1. Receiving audio from the ESP32-S3
2. Performing Speech-to-Text using `faster-whisper`
3. Processing the transcribed text using **Qwen3-4B through Ollama**
4. Generating a textual response
5. Converting the response to speech using **Piper TTS**
6. Making the generated WAV file available to the ESP32-S3
7. Returning timing and audio information to the edge device

Because the AI pipeline runs locally, the system does not require a cloud AI service for its core functionality.

---

## 4. System Architecture

```text
                         ┌─────────────────────┐
                         │ Button / Dashboard  │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │     ESP32-S3        │
                         │                     │
                         │  INMP441 I2S Mic    │
                         │       ↓             │
                         │  Audio Capture      │
                         └──────────┬──────────┘
                                    │
                          WebSocket │
                          PCM16 Data│
                                    ▼
                    ┌───────────────────────────┐
                    │      FastAPI Backend      │
                    │       /ws/assistant       │
                    └─────────────┬─────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              │                   │                   │
              ▼                   ▼                   ▼
      ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
      │ faster-      │    │   Ollama     │    │   Piper TTS  │
      │ whisper      │    │   Qwen3-4B   │    │              │
      │              │    │              │    │              │
      │ Speech →     │    │ Intent /     │    │ Text →       │
      │ Text         │    │ Response     │    │ Speech       │
      └──────┬───────┘    └──────┬───────┘    └──────┬───────┘
             │                   │                   │
             └───────────────────┴───────────────────┘
                                 │
                                 ▼
                         ┌─────────────────┐
                         │  answer.wav     │
                         └────────┬────────┘
                                  │
                             HTTP Fetch
                                  │
                                  ▼
                         ┌─────────────────┐
                         │    ESP32-S3     │
                         │                 │
                         │ MAX98357A Amp   │
                         │       ↓         │
                         │    Speaker      │
                         └─────────────────┘