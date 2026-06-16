#!/usr/bin/env python3
"""
Lamp intercom controller — Raspberry Pi 3 Model B
--------------------------------------------------
Hardware (BCM GPIO numbering):

  LED strip 1  DIN → BCM GPIO12 (physical pin 32)
  LED strip 2  DIN → BCM GPIO13 (physical pin 33)
  Button DIAL      → BCM GPIO17 (physical pin 11)
  Button RECORD    → BCM GPIO18 (physical pin 12)
  Button PLAY      → BCM GPIO27 (physical pin 13)
  Speaker          → 3.5mm audio jack (plughw:1,0)
  Mic              → USB (plughw:2,0)

LED states:
  Idle             → warm white (static)
  Incoming call    → flashing warm white
  In call          → green
  Recording        → red
  Playing message  → blue

Requirements:
  sudo pip3 install rpi_ws281x RPi.GPIO --break-system-packages
  baresip must be running with ctrl_tcp_listen 127.0.0.1:4444

Run with sudo:
  sudo python3 lamp_control.py
"""

import time
import signal
import sys
import socket
import subprocess
import os
import threading
import RPi.GPIO as GPIO
from rpi_ws281x import PixelStrip, Color

# ── Configuration ──────────────────────────────────────────────────────────

REMOTE_IP      = "192.168.10.1"      # <-- change to other Pi's IP
REMOTE_USER    = "lampa"             # <-- change to other Pi's SIP user
LOCAL_USER     = "lampb"             # <-- change per Pi

BARESIP_HOST   = "127.0.0.1"
BARESIP_PORT   = 4444

MESSAGE_PATH   = "/home/group66/message.wav"

MIC_DEVICE     = "plughw:2,0"
SPEAKER_DEVICE = "plughw:1,0"

BTN_DIAL       = 17
BTN_RECORD     = 18
BTN_PLAY       = 27

# ── LED configuration ──────────────────────────────────────────────────────

LED_COUNT      = 12
LED_FREQ_HZ    = 800000
LED_DMA        = 10
LED_BRIGHTNESS = 128
LED_INVERT     = False

STRIP1_PIN     = 12
STRIP2_PIN     = 13

# Colors
COLOR_IDLE     = Color(255, 147, 41)   # warm white
COLOR_CALL     = Color(0,   255,  0)   # green
COLOR_RECORD   = Color(255,   0,  0)   # red
COLOR_PLAY     = Color(0,     0, 255)  # blue
COLOR_OFF      = Color(0,     0,  0)

# ── State ──────────────────────────────────────────────────────────────────
in_call        = False
is_recording   = False
record_proc    = None
play_proc      = None

# LED flash thread control
flash_thread   = None
flash_active   = False

# ── LED helpers ────────────────────────────────────────────────────────────
def fill_strips(strip1, strip2, color):
    for i in range(LED_COUNT):
        strip1.setPixelColor(i, color)
        strip2.setPixelColor(i, color)
    strip1.show()
    strip2.show()

def set_idle(strip1, strip2):
    global flash_active
    flash_active = False
    time.sleep(0.05)  # let flash thread stop
    fill_strips(strip1, strip2, COLOR_IDLE)

def set_color(strip1, strip2, color):
    global flash_active
    flash_active = False
    time.sleep(0.05)
    fill_strips(strip1, strip2, color)

def start_flash(strip1, strip2):
    global flash_thread, flash_active
    flash_active = True
    def _flash():
        state = True
        while flash_active:
            fill_strips(strip1, strip2, COLOR_IDLE if state else COLOR_OFF)
            state = not state
            time.sleep(0.4)
    flash_thread = threading.Thread(target=_flash, daemon=True)
    flash_thread.start()

# ── Baresip control ────────────────────────────────────────────────────────
def baresip_cmd(cmd):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((BARESIP_HOST, BARESIP_PORT))
        s.send((cmd + "\n").encode())
        s.close()
        print(f"[baresip] {cmd}")
    except Exception as e:
        print(f"[baresip] ERROR sending '{cmd}': {e}")

# ── Baresip event listener (detects incoming calls) ────────────────────────
def start_event_listener(strip1, strip2):
    """Listen on baresip control socket for incoming call events."""
    def _listen():
        while True:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5)
                s.connect((BARESIP_HOST, BARESIP_PORT))
                while True:
                    data = s.recv(1024).decode(errors="ignore")
                    if not data:
                        break
                    if "INCOMING" in data or "CALL_INCOMING" in data:
                        print("[EVENT] Incoming call — flashing")
                        start_flash(strip1, strip2)
                    elif "CALL_ESTABLISHED" in data:
                        print("[EVENT] Call established — green")
                        set_color(strip1, strip2, COLOR_CALL)
                    elif "CALL_CLOSED" in data:
                        print("[EVENT] Call ended — idle")
                        global in_call
                        in_call = False
                        set_idle(strip1, strip2)
                s.close()
            except Exception:
                time.sleep(2)  # retry on disconnect
    t = threading.Thread(target=_listen, daemon=True)
    t.start()

# ── Button actions ─────────────────────────────────────────────────────────
def action_dial(strip1, strip2):
    global in_call
    if not in_call:
        print("[DIAL] Calling other lamp...")
        baresip_cmd(f"/dial sip:{REMOTE_USER}@{REMOTE_IP}")
        in_call = True
        set_color(strip1, strip2, COLOR_CALL)
    else:
        print("[DIAL] Hanging up...")
        baresip_cmd("/hangup")
        in_call = False
        set_idle(strip1, strip2)

def action_record_start(strip1, strip2):
    global is_recording, record_proc
    if is_recording or in_call:
        return
    print(f"[RECORD] Recording to {MESSAGE_PATH}...")
    set_color(strip1, strip2, COLOR_RECORD)
    record_proc = subprocess.Popen([
        "arecord", "-D", MIC_DEVICE, "-f", "cd", "-t", "wav", MESSAGE_PATH
    ])
    is_recording = True

def action_record_stop(strip1, strip2):
    global is_recording, record_proc
    if not is_recording or record_proc is None:
        return
    record_proc.terminate()
    record_proc.wait()
    record_proc = None
    is_recording = False
    print("[RECORD] Saved.")
    set_idle(strip1, strip2)

def action_play(strip1, strip2):
    global play_proc
    if in_call or is_recording:
        return
    if not os.path.exists(MESSAGE_PATH):
        print("[PLAY] No message found.")
        return
    if play_proc and play_proc.poll() is None:
        play_proc.terminate()
        play_proc.wait()
    print(f"[PLAY] Playing {MESSAGE_PATH}...")
    set_color(strip1, strip2, COLOR_PLAY)
    play_proc = subprocess.Popen([
        "aplay", "-D", SPEAKER_DEVICE, MESSAGE_PATH
    ])
    # Return to idle when playback finishes
    def _wait():
        play_proc.wait()
        set_idle(strip1, strip2)
    threading.Thread(target=_wait, daemon=True).start()

# ── GPIO setup ─────────────────────────────────────────────────────────────
def setup_buttons():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in (BTN_DIAL, BTN_RECORD, BTN_PLAY):
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def setup_strips():
    strip1 = PixelStrip(LED_COUNT, STRIP1_PIN, LED_FREQ_HZ, LED_DMA,
                        LED_INVERT, LED_BRIGHTNESS, 0)
    strip2 = PixelStrip(LED_COUNT, STRIP2_PIN, LED_FREQ_HZ, LED_DMA + 1,
                        LED_INVERT, LED_BRIGHTNESS, 1)
    strip1.begin()
    strip2.begin()
    return strip1, strip2

# ── Cleanup ────────────────────────────────────────────────────────────────
def cleanup(strip1, strip2):
    global flash_active
    print("\n[EXIT] Cleaning up...")
    flash_active = False
    action_record_stop(strip1, strip2)
    if in_call:
        baresip_cmd("/hangup")
    fill_strips(strip1, strip2, COLOR_OFF)
    GPIO.cleanup()

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    strip1, strip2 = setup_strips()
    setup_buttons()
    set_idle(strip1, strip2)
    start_event_listener(strip1, strip2)

    def handle_exit(sig, frame):
        cleanup(strip1, strip2)
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    print("Lamp controller ready.")
    print("  BTN_DIAL   (BCM17) — call / hang up")
    print("  BTN_RECORD (BCM18) — hold to record")
    print("  BTN_PLAY   (BCM27) — play last message")
    print("Ctrl+C to quit.\n")

    last_state = {
        BTN_DIAL:   GPIO.HIGH,
        BTN_RECORD: GPIO.HIGH,
        BTN_PLAY:   GPIO.HIGH,
    }

    while True:
        for pin in (BTN_DIAL, BTN_RECORD, BTN_PLAY):
            current = GPIO.input(pin)
            prev    = last_state[pin]

            if current == GPIO.LOW and prev == GPIO.HIGH:
                time.sleep(0.02)
                if GPIO.input(pin) == GPIO.LOW:
                    if pin == BTN_DIAL:
                        action_dial(strip1, strip2)
                    elif pin == BTN_RECORD:
                        action_record_start(strip1, strip2)
                    elif pin == BTN_PLAY:
                        action_play(strip1, strip2)

            if current == GPIO.HIGH and prev == GPIO.LOW:
                if pin == BTN_RECORD:
                    action_record_stop(strip1, strip2)

            last_state[pin] = current

        time.sleep(0.01)

if __name__ == "__main__":
    main()
