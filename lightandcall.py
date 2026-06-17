#!/usr/bin/env python3
"""
Lamp intercom controller — Raspberry Pi 3 Model B
--------------------------------------------------

HARDWARE WIRING (BCM GPIO numbering / physical pin):
=====================================================

── RASPBERRY PI J8 HEADER ──────────────────────────────────────────────────

  Physical Pin 2  (5V)       → GF1002 VCC
  Physical Pin 4  (5V)       → SN74LS125AN Pin 14 (VCC)
  Physical Pin 6  (GND)      → GF1002 GND
  Physical Pin 9  (GND)      → SN74LS125AN Pin 7 (GND)  [shared ground bus]
  Physical Pin 11 (GPIO 17)  → Button DIAL   (other leg to GND)
  Physical Pin 12 (GPIO 18)  → SN74LS125AN Pin 2 (1A)   [LED data signal]
                               SN74LS125AN Pin 5 (2A)   [jumper same row]
  Physical Pin 13 (GPIO 27)  → Button PLAY   (other leg to GND)
  Physical Pin 14 (GND)      → all buttons other leg (common GND)
  Physical Pin 18 (GPIO 24)  → Button RECORD (other leg to GND)
  Physical Pin 21 (GND)      → External 5V supply GND   [shared ground!]
  Physical Pin 4  (5V)       → 3.5mm jack tip (audio in) → GF1002 AIN+
  Physical Pin 6  (GND)      → 3.5mm jack sleeve          → GF1002 AIN-
  3.5mm jack (audio out)     → GF1002 AIN+ (tip) / AIN- (sleeve/GND)

── SN74LS125AN (Quad Buffer IC on breadboard) ───────────────────────────────

  Pin  1 (1OE)  → GND  (always enabled)
  Pin  2 (1A)   → Pi Physical Pin 12 (GPIO 18) — DATA IN
  Pin  3 (1Y)   → LED Strip 1 Green (Data)
  Pin  4 (2OE)  → GND  (always enabled)
  Pin  5 (2A)   → Pi Physical Pin 12 (GPIO 18) — DATA IN (jumper)
  Pin  6 (2Y)   → LED Strip 2 Green (Data)
  Pin  7 (GND)  → Pi Physical Pin 9 (GND) + External supply GND
  Pin 14 (VCC)  → Pi Physical Pin 4 (5V)

── EXTERNAL 5V SUPPLY ───────────────────────────────────────────────────────

  +5V → LED Strip 1 Red (VCC)
  +5V → LED Strip 2 Red (VCC)
  GND → LED Strip 1 Black (GND)
  GND → LED Strip 2 Black (GND)
  GND → Pi Physical Pin 9 (GND)  ← SHARED GROUND — essential!

── GF1002 AMPLIFIER ─────────────────────────────────────────────────────────

  VCC  → Pi Physical Pin 2 (5V)
  GND  → Pi Physical Pin 6 (GND)
  AIN+ → Pi 3.5mm jack tip   (left audio channel)
  AIN- → Pi 3.5mm jack sleeve (ground)
  OUT+ → Speaker +
  OUT- → Speaker -

── LED STRIPS (SJ-10060-2811 / WS2812B) ────────────────────────────────────

  Strip 1:
    Red   → External 5V supply +5V
    Black → External 5V supply GND
    Green → SN74LS125AN Pin 3 (1Y)

  Strip 2:
    Red   → External 5V supply +5V
    Black → External 5V supply GND
    Green → SN74LS125AN Pin 6 (2Y)

  Both strips are chained logically as one 30-LED virtual strip
  driven from GPIO 18 via the SN74LS125AN buffer.

── BUTTONS ──────────────────────────────────────────────────────────────────

  DIAL   button: one leg → Pi Physical Pin 11 (GPIO 17)
                 other leg → Pi Physical Pin 14 (GND)
  RECORD button: one leg → Pi Physical Pin 18 (GPIO 24)
                 other leg → Pi Physical Pin 14 (GND)
  PLAY   button: one leg → Pi Physical Pin 13 (GPIO 27)
                 other leg → Pi Physical Pin 14 (GND)
  (Internal pull-up resistors used — no external resistors needed)

── USB MIC ──────────────────────────────────────────────────────────────────

  Plug into any USB port. Find device index with:
    python3 -c "import pyaudio; p=pyaudio.PyAudio(); [print(i,p.get_device_info_by_index(i)['name']) for i in range(p.get_device_count())]"

── INSTALL DEPENDENCIES ─────────────────────────────────────────────────────

  sudo apt update
  sudo apt install python3-rpi.gpio
  sudo pip3 install rpi_ws281x adafruit-circuitpython-neopixel adafruit-blinka --break-system-packages
  baresip must be running: baresip -d
  baresip config must have: ctrl_tcp_listen 127.0.0.1:4444

── RUN ──────────────────────────────────────────────────────────────────────

  sudo python3 lamp_control.py

"""

import time
import signal
import sys
import socket
import subprocess
import os
import threading
import board
import neopixel
import RPi.GPIO as GPIO

# ── Configuration ──────────────────────────────────────────────────────────

REMOTE_IP      = "192.168.10.1"   # other Pi's IP address
REMOTE_USER    = "lampa"          # other Pi's SIP username
LOCAL_USER     = "lampb"          # this Pi's SIP username

BARESIP_HOST   = "127.0.0.1"
BARESIP_PORT   = 4444

MESSAGE_PATH   = "/home/group66/message.wav"

# Audio devices — adjust index numbers if needed
# Run the pyaudio command above to find your device numbers
MIC_DEVICE     = "plughw:2,0"    # USB mic — change index if different
SPEAKER_DEVICE = "plughw:1,0"    # GF1002 via 3.5mm — change index if different

# GPIO pins (BCM numbering)
BTN_DIAL       = 17   # Physical pin 11
BTN_RECORD     = 24   # Physical pin 18
BTN_PLAY       = 27   # Physical pin 13

# ── LED configuration ──────────────────────────────────────────────────────

LED_PIN        = board.D18        # Physical pin 12 → SN74LS125AN → both strips
LEDS_PER_STRIP = 15               # LEDs per strip (SJ-10060-2811 = 15 LEDs)
STRIP_COUNT    = 2                # Two strips chained via SN74LS125AN
LED_TOTAL      = LEDS_PER_STRIP * STRIP_COUNT  # 30 total
LED_BRIGHTNESS = 0.2              # Keep low — powered from external 5V

# Colors (R, G, B) for WS2812B strips
COLOR_IDLE     = (255, 147, 41)   # warm white / amber
COLOR_CALL     = (0,   255,  0)   # green — in call
COLOR_RECORD   = (255,   0,  0)   # red — recording
COLOR_PLAY     = (0,     0, 255)  # blue — playing message
COLOR_RING     = (255, 147, 41)   # warm white flash for incoming call
COLOR_OFF      = (0,     0,  0)   # all off

# ── State ──────────────────────────────────────────────────────────────────

in_call        = False
is_recording   = False
record_proc    = None
play_proc      = None
flash_active   = False

# ── LED setup ──────────────────────────────────────────────────────────────

pixels = neopixel.NeoPixel(
    LED_PIN,
    LED_TOTAL,
    brightness=LED_BRIGHTNESS,
    auto_write=False,
    pixel_order=neopixel.GRB   # WS2812B uses GRB order
)

# ── LED helpers ────────────────────────────────────────────────────────────

def fill_pixels(color):
    pixels.fill(color)
    pixels.show()

def set_idle():
    global flash_active
    flash_active = False
    time.sleep(0.05)
    fill_pixels(COLOR_IDLE)

def set_color(color):
    global flash_active
    flash_active = False
    time.sleep(0.05)
    fill_pixels(color)

def start_flash():
    """Flash warm white for incoming call."""
    global flash_active
    flash_active = True
    def _flash():
        state = True
        while flash_active:
            fill_pixels(COLOR_RING if state else COLOR_OFF)
            state = not state
            time.sleep(0.4)
        fill_pixels(COLOR_OFF)
    threading.Thread(target=_flash, daemon=True).start()

# ── Baresip control ────────────────────────────────────────────────────────

def baresip_cmd(cmd):
    """Send a command to baresip over TCP control socket."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((BARESIP_HOST, BARESIP_PORT))
        s.send((cmd + "\n").encode())
        s.close()
        print(f"[baresip] sent: {cmd}")
    except Exception as e:
        print(f"[baresip] ERROR sending '{cmd}': {e}")

# ── Baresip event listener ─────────────────────────────────────────────────

def start_event_listener():
    """Listen for baresip events in a background thread."""
    def _listen():
        global in_call
        while True:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5)
                s.connect((BARESIP_HOST, BARESIP_PORT))
                print("[EVENT] Connected to baresip event socket")
                while True:
                    data = s.recv(1024).decode(errors="ignore")
                    if not data:
                        break
                    print(f"[EVENT] {data.strip()}")
                    if "INCOMING" in data or "CALL_INCOMING" in data:
                        print("[EVENT] Incoming call — flashing")
                        start_flash()
                    elif "CALL_ESTABLISHED" in data:
                        print("[EVENT] Call established — green")
                        in_call = True
                        set_color(COLOR_CALL)
                    elif "CALL_CLOSED" in data:
                        print("[EVENT] Call ended — idle")
                        in_call = False
                        set_idle()
                s.close()
            except Exception as e:
                print(f"[EVENT] Socket error: {e} — retrying in 2s")
                time.sleep(2)
    threading.Thread(target=_listen, daemon=True).start()

# ── Button actions ─────────────────────────────────────────────────────────

def action_dial():
    """Dial the other lamp or hang up if already in call."""
    global in_call
    if not in_call:
        print(f"[DIAL] Calling sip:{REMOTE_USER}@{REMOTE_IP} ...")
        baresip_cmd(f"/dial sip:{REMOTE_USER}@{REMOTE_IP}")
        in_call = True
        set_color(COLOR_CALL)
    else:
        print("[DIAL] Hanging up...")
        baresip_cmd("/hangup")
        in_call = False
        set_idle()

def action_record_start():
    """Start recording a voice message via USB mic."""
    global is_recording, record_proc
    if is_recording or in_call:
        print("[RECORD] Busy — ignoring")
        return
    print(f"[RECORD] Recording to {MESSAGE_PATH} ...")
    set_color(COLOR_RECORD)
    record_proc = subprocess.Popen([
        "arecord",
        "-D", MIC_DEVICE,
        "-f", "S16_LE",
        "-r", "44100",
        "-c", "1",
        "-t", "wav",
        MESSAGE_PATH
    ])
    is_recording = True

def action_record_stop():
    """Stop recording and save the message."""
    global is_recording, record_proc
    if not is_recording or record_proc is None:
        return
    record_proc.terminate()
    record_proc.wait()
    record_proc = None
    is_recording = False
    print(f"[RECORD] Saved to {MESSAGE_PATH}")
    set_idle()

def action_play():
    """Play the last recorded voice message through GF1002 speaker."""
    global play_proc
    if in_call:
        print("[PLAY] In call — ignoring")
        return
    if is_recording:
        print("[PLAY] Recording active — ignoring")
        return
    if not os.path.exists(MESSAGE_PATH):
        print(f"[PLAY] No message at {MESSAGE_PATH}")
        return
    # Stop any current playback
    if play_proc and play_proc.poll() is None:
        play_proc.terminate()
        play_proc.wait()
        time.sleep(0.3)
    print(f"[PLAY] Playing {MESSAGE_PATH} via {SPEAKER_DEVICE} ...")
    set_color(COLOR_PLAY)
    play_proc = subprocess.Popen([
        "aplay",
        "-D", SPEAKER_DEVICE,
        MESSAGE_PATH
    ])
    def _wait_for_end():
        play_proc.wait()
        time.sleep(0.3)
        set_idle()
    threading.Thread(target=_wait_for_end, daemon=True).start()

# ── GPIO button setup ──────────────────────────────────────────────────────

def setup_buttons():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in (BTN_DIAL, BTN_RECORD, BTN_PLAY):
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    print(f"[GPIO] Buttons ready on BCM {BTN_DIAL}, {BTN_RECORD}, {BTN_PLAY}")

# ── Cleanup ────────────────────────────────────────────────────────────────

def cleanup():
    global flash_active
    print("\n[EXIT] Cleaning up...")
    flash_active = False
    action_record_stop()
    if in_call:
        baresip_cmd("/hangup")
    time.sleep(0.1)
    fill_pixels(COLOR_OFF)
    GPIO.cleanup()
    print("[EXIT] Done.")

# ── Main loop ──────────────────────────────────────────────────────────────

def main():
    setup_buttons()
    set_idle()
    start_event_listener()

    def handle_exit(sig, frame):
        cleanup()
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    print("\n══════════════════════════════════════")
    print("  Lamp intercom controller ready")
    print("══════════════════════════════════════")
    print(f"  DIAL   (BCM17 / Pin 11) — call / hang up")
    print(f"  RECORD (BCM24 / Pin 18) — hold to record message")
    print(f"  PLAY   (BCM27 / Pin 13) — play last message")
    print(f"  LEDs   (BCM18 / Pin 12) → SN74LS125AN → 2x strips (30 LEDs)")
    print(f"  Speaker via GF1002 on 3.5mm aux")
    print(f"  Mic    via USB ({MIC_DEVICE})")
    print("  Ctrl+C to quit\n")

    last_state = {
        BTN_DIAL:   GPIO.HIGH,
        BTN_RECORD: GPIO.HIGH,
        BTN_PLAY:   GPIO.HIGH,
    }

    while True:
        for pin in (BTN_DIAL, BTN_RECORD, BTN_PLAY):
            current = GPIO.input(pin)
            prev    = last_state[pin]

            # Button pressed (LOW = pressed, pull-up active)
            if current == GPIO.LOW and prev == GPIO.HIGH:
                time.sleep(0.02)  # debounce
                if GPIO.input(pin) == GPIO.LOW:
                    if pin == BTN_DIAL:
                        action_dial()
                    elif pin == BTN_RECORD:
                        action_record_start()
                    elif pin == BTN_PLAY:
                        action_play()

            # Button released
            if current == GPIO.HIGH and prev == GPIO.LOW:
                if pin == BTN_RECORD:
                    action_record_stop()

            last_state[pin] = current

        time.sleep(0.01)  # 10ms poll interval

if __name__ == "__main__":
    main()