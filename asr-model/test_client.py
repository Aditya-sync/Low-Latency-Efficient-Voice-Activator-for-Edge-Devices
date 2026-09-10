"""
Test client for the ASR server's /transcribe endpoint.

Sends a local WAV file over HTTP and prints:
  - HTTP status
  - transcription text
  - server-reported processing time
  - total client-side latency (send + wait + receive)

Usage:
    python test_client.py path\\to\\audio.wav
    python test_client.py path\\to\\audio.wav http://192.168.1.23:8000
"""

import sys
import time

import requests


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_client.py <path_to_wav_file> [server_url]")
        print(r'Example: python test_client.py audio\test.wav http://127.0.0.1:8000')
        sys.exit(1)

    wav_path = sys.argv[1]
    server_url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
    url = f"{server_url.rstrip('/')}/transcribe"

    print(f"Server:  {server_url}")
    print(f"File:    {wav_path}")
    print(f"POST to: {url}")
    print("-" * 50)

    t_client_start = time.time()

    try:
        with open(wav_path, "rb") as f:
            files = {"file": (wav_path, f, "audio/wav")}
            response = requests.post(url, files=files, timeout=120)
    except FileNotFoundError:
        print(f"ERROR: file not found: {wav_path}")
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        print(f"ERROR: could not connect to {server_url}. Is the server running? Details: {e}")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("ERROR: request timed out after 120s")
        sys.exit(1)

    client_latency = time.time() - t_client_start

    print(f"HTTP status: {response.status_code}")

    try:
        data = response.json()
    except ValueError:
        print("ERROR: response was not valid JSON")
        print(response.text)
        sys.exit(1)

    print(f"Success:                {data.get('success')}")
    print(f"Transcription:          {data.get('text')!r}")
    print(f"Detected language:      {data.get('language')} "
          f"(confidence={data.get('language_probability')})")
    print(f"Audio duration:         {data.get('duration_sec')} s")
    print(f"Server processing time: {data.get('processing_time_sec')} s")
    print(f"Total client latency:   {client_latency:.3f} s")

    if data.get("error"):
        print(f"Server-reported error:  {data.get('error')}")


if __name__ == "__main__":
    main()
