#!/usr/bin/env python3
"""Pretend to be the camera, for testing the presence logic without JebCam.

    python tools/fake_presence.py --host localhost
Then type:  2      (two people, close)
            2 600  (two people, 600 px away — too far to engage)
            0      (everyone left)
            q      (quit)
Publishes once a second, like a real tracker would.
"""

import argparse
import json
import threading
import time

import paho.mqtt.client as mqtt

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="localhost")
ap.add_argument("--port", type=int, default=1883)
ap.add_argument("--topic", default="halloween/presence")
args = ap.parse_args()

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.connect(args.host, args.port)
client.loop_start()

state = {"count": 0, "nearest": None}


def publisher():
    while True:
        client.publish(args.topic, json.dumps(state))
        time.sleep(1)


threading.Thread(target=publisher, daemon=True).start()
print("count [distance] — e.g. '2', '1 600', '0', 'q'")
while (line := input("> ").strip()) != "q":
    parts = line.split()
    if not parts:
        continue
    state["count"] = int(parts[0])
    state["nearest"] = float(parts[1]) if len(parts) > 1 else (100 if state["count"] else None)
    print(f"publishing {state}")
