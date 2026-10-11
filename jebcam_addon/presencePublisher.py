"""Drop this into JebCam/src/. Publishes who's in front of the house for the
talking demon, on the same MQTT broker as the head/eyes topics.

Payload on halloween/presence (JSON):
    {"count": <people tracked>, "nearest": <pixels from reference_point>}

Wire it into TargetAcquisition.run(), just after `tracks = ...get_all_tracks()`:

    from presencePublisher import PresencePublisher
    ...
    # in __init__:
    self.presence = PresencePublisher(reference_point=reference_point)
    # in run(), after tracks = self.tracker.get_all_tracks():
    self.presence.update(tracks)
"""

import json
import math
import time

import paho.mqtt.client as mqtt

from jebsecrets import Secrets


class PresencePublisher:
    def __init__(self, reference_point=(500, 350), topic="halloween/presence",
                 min_interval=0.5, heartbeat=1.0, labels=("person",)):
        self.reference_point = reference_point
        self.topic = topic
        self.min_interval = min_interval  # don't publish faster than this
        self.heartbeat = heartbeat        # ...but at least this often
        self.labels = labels              # dogs don't get to chat
        self._last_payload = None
        self._last_pub = 0.0

        try:  # paho-mqtt 2.x
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        except AttributeError:  # paho-mqtt 1.x
            self.client = mqtt.Client()
        self.client.username_pw_set(Secrets.mqtt_user, Secrets.mqtt_pass)
        try:
            self.client.connect(Secrets.mqtt_server, Secrets.mqtt_port, keepalive=60)
            self.client.loop_start()
        except Exception as e:
            print(f"PresencePublisher(): MQTT connection failed: {e}")

    def _distance(self, track):
        # Same "feet position" as HeadActuator: bottom-middle of the box.
        x, y, w, h = track.get_state().astype(int)
        return math.hypot(x + w / 2 - self.reference_point[0], y + h - self.reference_point[1])

    def update(self, tracks):
        people = [t for t in tracks if getattr(t, "label", "person") in self.labels]
        payload = {"count": len(people)}
        if people:
            payload["nearest"] = round(min(self._distance(t) for t in people))

        now = time.time()
        changed = payload != self._last_payload
        if (changed and now - self._last_pub >= self.min_interval) or now - self._last_pub >= self.heartbeat:
            try:
                self.client.publish(self.topic, json.dumps(payload))
                self._last_payload = payload
                self._last_pub = now
            except Exception as e:
                print(f"PresencePublisher: publish failed: {e}")
