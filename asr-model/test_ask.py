"""
Test client for the /ask endpoint (text -> Qwen -> text), independent
of any audio pipeline.

Usage:
    python test_ask.py "What is today's date?"
    python test_ask.py "What is today's date?" http://192.168.1.23:8000
"""

import sys
import time

import requests


def main():
    if len(sys.argv) < 2:
        print('Usage: python test_ask.py "your question" [server_url]')
        sys.exit(1)

    question = sys.argv[1]
    server_url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8000"
    url = f"{server_url.rstrip('/')}/ask"

    print(f"POST {url}")
    print(f"question: {question!r}")
    print("-" * 50)

    t0 = time.time()
    try:
        response = requests.post(url, json={"question": question}, timeout=120)
    except requests.exceptions.ConnectionError as e:
        print(f"ERROR: could not connect to {server_url}. Is the server running? Details: {e}")
        sys.exit(1)

    client_latency = time.time() - t0
    print(f"HTTP status: {response.status_code}")

    try:
        data = response.json()
    except ValueError:
        print("ERROR: response was not valid JSON")
        print(response.text)
        sys.exit(1)

    if response.status_code != 200:
        print(f"Server error: {data.get('error')}")
        sys.exit(1)

    print(f"Answer:                 {data.get('answer')!r}")
    print(f"Server LLM time:        {data.get('llm_time_sec')} s")
    print(f"Total client latency:   {client_latency:.3f} s")


if __name__ == "__main__":
    main()
