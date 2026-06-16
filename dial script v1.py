#!/usr/bin/env python3
"""
Lamp intercom controller — Raspberry Pi 3 Model B
--------------------------------------------------
Hardware (BCM GPIO numbering):

  Button DIAL   → BCM GPIO17 (physical pin 11)
  Button RECORD → BCM GPIO18 (physical pin 12)
  Button PLAY   → BCM GPIO27 (physical pin 13)
  Mic           → USB (plughw:2,0)
  Speaker       → 3.5mm jack (plughw:1,0)

Button behaviour:
  DIAL   — if idle, call the other lamp. If in call, hang up.
  RECORD — record a message to MESSAGE_PATH (stops on button release)
  PLAY   — play back the last recorded message

Requirements:
  sudo pip3 install RPi.GPIO --break-system-packages
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
import RPi.GPIO as GPIO

# ── Configuration ──────────────────────────────────────────────────────────

# IP of the OTHER lamp (change per Pi)
# Pi A: REMOTE_IP = "192.168.10.1"
# Pi B: REMOTE_IP = "192.168.10.1"
REMOTE_IP      = "192.168.10.1"      # <-- change to other Pi's IP
REMOTE_USER    = "lampa"              # <-- change to other Pi's SIP user

# Local SIP user (must match ~/.baresip/accounts)
LOCAL_USER     = "lampb"             # <-- change per Pi

# Baresip control socket
BARESIP_HOST   = "127.0.0.1"
BARESIP_PORT   = 4444

# Message file path
MESSAGE_PATH   = "/home/group66/message.wav"

# Audio devices
MIC_DEVICE     = "plughw:2,0"
SPEAKER_DEVICE = "plughw:1,0"

# GPIO pins (BCM)
BTN_DIAL   = 17
BTN_RECORD = 18
BTN_PLAY   = 27

# ── State ──────────────────────────────────────────────────────────────────
in_call      = False
is_recording = False
record_proc  = None
play_proc    = None

# ── Baresip control ────────────────────────────────────────────────────────
def baresip_cmd(cmd):
    """Send a command to baresip over TCP control socket."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((BARESIP_HOST, BARESIP_PORT))
        s.send((cmd + "\n").encode())
        s.close()
        print(f"[baresip] {cmd}")
    except Exception as e:
        print(f"[baresip] ERROR sending '{cmd}': {e}")

# ── Button actions ─────────────────────────────────────────────────────────
def action_dial():
    global in_call
    if not in_call:
        print("[DIAL] Calling other lamp...")
        baresip_cmd(f"/dial sip:{REMOTE_USER}@{REMOTE_IP}")
        in_call = True
    else:
        print("[DIAL] Hanging up...")
        baresip_cmd("/hangup")
        in_call = False

def action_record_start():
    global is_recording, record_proc
    if is_recording:
        return
    if in_call:
        print("[RECORD] Cannot record during a call.")
        return
    print(f"[RECORD] Recording to {MESSAGE_PATH}...")
    record_proc = subprocess.Popen([
        "arecord",
        "-D", MIC_DEVICE,
        "-f", "cd",
        "-t", "wav",
        MESSAGE_PATH
    ])
    is_recording = True

def action_record_stop():
    global is_recording, record_proc
    if not is_recording or record_proc is None:
        return
    record_proc.terminate()
    record_proc.wait()
    record_proc = None
    is_recording = False
    print("[RECORD] Saved.")

def action_play():
    global play_proc
    if in_call:
        print("[PLAY] Cannot play during a call.")
        return
    if is_recording:
        print("[PLAY] Cannot play while recording.")
        return
    if not os.path.exists(MESSAGE_PATH):
        print("[PLAY] No message found.")
        return
    # Stop any current playback first
    if play_proc and play_proc.poll() is None:
        play_proc.terminate()
        play_proc.wait()
    print(f"[PLAY] Playing {MESSAGE_PATH}...")
    play_proc = subprocess.Popen([
        "aplay",
        "-D", SPEAKER_DEVICE,
        MESSAGE_PATH
    ])

# ── GPIO setup ─────────────────────────────────────────────────────────────
def setup_buttons():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in (BTN_DIAL, BTN_RECORD, BTN_PLAY):
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# ── Cleanup ────────────────────────────────────────────────────────────────
def cleanup():
    print("\n[EXIT] Cleaning up...")
    action_record_stop()
    if in_call:
        baresip_cmd("/hangup")
    GPIO.cleanup()

# ── Main loop ──────────────────────────────────────────────────────────────
def main():
    setup_buttons()

    def handle_exit(sig, frame):
        cleanup()
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    print("Lamp controller ready.")
    print("  BTN_DIAL   (BCM17) — call / hang up")
    print("  BTN_RECORD (BCM18) — hold to record message")
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

            # Falling edge — button pressed
            if current == GPIO.LOW and prev == GPIO.HIGH:
                time.sleep(0.02)  # debounce
                if GPIO.input(pin) == GPIO.LOW:
                    if pin == BTN_DIAL:
                        action_dial()
                    elif pin == BTN_RECORD:
                        action_record_start()
                    elif pin == BTN_PLAY:
                        action_play()

            # Rising edge — button released
            if current == GPIO.HIGH and prev == GPIO.LOW:
                if pin == BTN_RECORD:
                    action_record_stop()

            last_state[pin] = current

        time.sleep(0.01)  # poll at ~100 Hz

if __name__ == "__main__":
    main()