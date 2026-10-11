#!/usr/bin/env python3
"""Check the API keys and voice settings in .env, with readable error messages.

The live bot talks to ElevenLabs over a WebSocket, and when that's refused
you only see "HTTP 400". This makes the equivalent plain HTTP requests so
you see the actual reason.

    .venv/bin/python tools/check_apis.py
"""

import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)


def call(name, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = r.read()
            print(f"  OK   {name} ({len(payload)} bytes)")
            return payload
    except urllib.error.HTTPError as e:
        print(f"  FAIL {name}: HTTP {e.code}\n       {e.read().decode(errors='replace')[:400]}")
    except Exception as e:
        print(f"  FAIL {name}: {e}")
    return None


def val(name):
    v = os.getenv(name, "")
    if v != v.strip() or v.startswith(("'", '"')):
        print(f"  WARN {name} has spaces or quotes around it: {v!r}")
    return v.strip().strip("'\"")


el_key, voice = val("ELEVENLABS_API_KEY"), val("ELEVENLABS_VOICE_ID")
model = val("ELEVENLABS_MODEL") or "eleven_flash_v2_5"
rate = val("SAMPLE_RATE") or "16000"
h = {"xi-api-key": el_key}

print("ElevenLabs")
call("API key", "https://api.elevenlabs.io/v1/user", h)
v = call(f"voice {voice!r} is in your library", f"https://api.elevenlabs.io/v1/voices/{voice}", h)
if v:
    print(f"       name: {json.loads(v).get('name')}")
if model.startswith(("eleven_v3", "eleven_v4")):
    call(f"speech with {model} (Text to Dialogue)",
         f"https://api.elevenlabs.io/v1/text-to-dialogue?output_format=pcm_{rate}", h,
         {"model_id": model, "inputs": [{"text": "Greetings, mortal.", "voice_id": voice}]})
else:
    call(f"speech with {model} at pcm_{rate}",
         f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_{rate}", h,
         {"text": "Greetings, mortal.", "model_id": model})

print("Anthropic")
call(f"model {val('LLM_MODEL') or 'claude-haiku-5-5'}", "https://api.anthropic.com/v1/messages",
     {"x-api-key": val("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"},
     {"model": val("LLM_MODEL") or "claude-haiku-5-5", "max_tokens": 10,
      "messages": [{"role": "user", "content": "Say boo."}]})

print("Deepgram")
call("API key", "https://api.deepgram.com/v1/projects", {"Authorization": f"Token {val('DEEPGRAM_API_KEY')}"})
sys.exit(0)
