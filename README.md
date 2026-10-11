# Demon Robot

A talking Halloween figure for the front garden. When JebCam's camera sees
people walk up, the demon greets them and chats in a pitched-down, growly
voice until they leave.

```
camera (JebCam Pi 5) ──MQTT halloween/presence──▶ demon Pi 3A+
                                                    │
 XVF3800 mic ─▶ gate ─▶ Deepgram STT ─▶ Claude Haiku ─▶ safety filter
                                                    │
 speaker ◀─ XVF3800 ◀─ demon FX ◀─ ElevenLabs TTS ◀─┘
```

| File | What it does |
|---|---|
| `bot.py` | Builds and runs the Pipecat pipeline |
| `demon/persona.py` | **The personality and rules. Edit this most.** |
| `demon/safety.py` | Checks every sentence before it's spoken; strips `*stage directions*` |
| `demon/fx.py` | Voice effects: low-latency pitch-down, growl, small reverb, limiter |
| `demon/presence.py` | Camera-triggered start/stop; mic is closed when nobody's there |
| `jebcam_addon/presencePublisher.py` | Goes into JebCam to publish who's in front of the house |
| `pi/install.sh` | One-shot Pi setup |
| `pi/audio-setup.sh` | Makes the XVF3800 the default device, with a volume ceiling |
| `tools/fake_presence.py` | Pretends to be the camera, for testing |
| `tests/test_offline.py` | Tests that need no keys or hardware |

---

## 1. Before the hardware arrives (laptop)

**Get three API keys** (all have free credit to start):
[Deepgram](https://console.deepgram.com) (speech to text),
[Anthropic](https://console.anthropic.com) (the brain),
[ElevenLabs](https://elevenlabs.io) (the voice).

**Make the voice.** In ElevenLabs, create a voice from a text description
(Voice Design), e.g. *"ancient gravelly demon, deep, slow, amused, slightly
hoarse, British"*, or pick a deep voice from the library. Copy its voice ID.
`demon/fx.py` pitches it down a further 4 semitones (`FX_SEMITONES`), so a
voice that's merely deep is fine.

**Run it on the laptop.** Use headphones: the laptop has no echo
cancellation, so through speakers it will hear itself and talk over itself
(the XVF3800 fixes this on the real thing).

```bash
# macOS: brew install portaudio      Debian/Ubuntu: sudo apt install portaudio19-dev
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in the keys and voice ID
PYTHONPATH=. python tests/test_offline.py   # should end with ALL PASSED
python bot.py --always-present  # talk to it
```

This is where to spend the time before Tuesday: refine the persona in
`demon/persona.py`, try the voice settings and `FX_PRESET`/`FX_SEMITONES`,
and try to break the safety rules the way a cheeky 10-year-old would.

**Test the camera trigger** (optional), with a local broker:
```bash
mosquitto -v &                          # or point MQTT_HOST at your real broker
python bot.py                           # waits silently
python tools/fake_presence.py           # type "2" -> it greets; "0" -> it resets
```

## 2. Prepare the SD card now

1. In **Raspberry Pi Imager**: device *Raspberry Pi 3*, OS *Raspberry Pi OS Lite (64-bit)*.
2. In the customisation settings: hostname `demon`, your user, the garden
   Wi-Fi extender's SSID and password, Wi-Fi country GB, and enable SSH (with your key).
3. After flashing, open the card's **bootfs** partition and copy this whole
   `demon-robot` folder onto it, **including your filled-in `.env`**.

## 3. Tuesday

```bash
ssh demon.local
bash /boot/firmware/demon-robot/pi/install.sh   # ~15–30 min on a 3A+
# plug in the XVF3800 + speaker, then:
bash ~/demon-robot/pi/audio-setup.sh
speaker-test -D default -c 1 -t wav -l 1        # hear it?
arecord -D default -f S16_LE -r 16000 -c 1 -d 5 /tmp/t.wav && aplay /tmp/t.wav
sudo systemctl start demon && journalctl -u demon -f
```

Once it's all working, switch on the read-only overlay
(`sudo raspi-config` → Performance → Overlay File System) so pulling the plug
can't corrupt the card. Turn it off again to change anything.

**Things I couldn't test without the hardware:**
- `audio-setup.sh` assumes the XVF3800's USB firmware sends 2 capture channels
  with the speech-recognition channel second (per Seeed's wiki). If recording is
  silent or sounds wrong, change `ttable.0.1` to `ttable.0.0` in `/etc/asound.conf`.
- CPU headroom on the 3A+. `tests/test_offline.py` prints how long the effects take
  to process 1 s of audio; if it's above ~500 ms, set `FX_PRESET=lite`.

## 4. JebCam side

Copy `jebcam_addon/presencePublisher.py` into `JebCam/src/`, then in
`targetAcquisition.py`:

```python
from presencePublisher import PresencePublisher
# in __init__:
self.presence = PresencePublisher(reference_point=reference_point)
# in run(), right after: tracks = self.tracker.get_all_tracks()
self.presence.update(tracks)
```

It publishes `{"count": 2, "nearest": 180}` to `halloween/presence` on the same
broker as the head and eyes topics. People only, not dogs. `nearest` is in
**pixels** from the reference point, measured from the bottom-middle of the box,
like `HeadActuator`.

## Tuning

| Setting (`.env`) | Default | Turn it… |
|---|---|---|
| `ENGAGE_DIST` | 250 px | down if it greets people walking past; up if people have to come too close |
| `LEAVE_SECS` | 8 | up if it resets while people are still there but briefly untracked |
| `MAX_SESSION_SECS` | 180 | it says goodbye after this, then waits for that group to leave |
| `VAD_MIN_VOLUME` | 0.6 | up if wind/traffic triggers it; down if quiet children get ignored |
| `VAD_STOP_SECS` | 0.2 | up if it interrupts people mid-sentence (Pipecat's smart turn detection does most of this work) |
| `ELEVENLABS_MODEL` | eleven_flash_v2_5 | `eleven_v4_turbo` for a more expressive voice that acts out `[evil laugh]`, `[whispers]` etc. (tags listed in `persona.py`) |
| `FX_PRESET` | demon | `lite` (no pitch shift) if the Pi struggles; `off` to compare |
| Volume ceiling | -6 dB | `bash pi/audio-setup.sh -10` for quieter; everyday level in `alsamixer` |

Safety: the persona's rules do the main work; `demon/safety.py` is the backstop.
Add words to `BLOCKED_WORDS` as you find things. No audio is saved anywhere on
the Pi. At the default `LOG_LEVEL=INFO`, visitors' words aren't written to the
log; `DEBUG` logs transcripts, so only use that while testing.
