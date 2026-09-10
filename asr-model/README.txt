Put test WAV files here for use with test_client.py.

Any standard WAV file works for the /transcribe endpoint (mono or
stereo, most sample rates) since faster-whisper decodes it internally.
For realistic testing of the eventual ESP32 pipeline, use a mono,
16 kHz, 16-bit PCM WAV file.

Example:
    python test_client.py audio/test.wav
