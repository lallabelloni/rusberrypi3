#!/usr/bin/env python3
"""
Lamp intercom controller — Raspberry Pi 3 Model B
--------------------------------------------------

HARDWARE WIRING (BCM GPIO numbering / physical pin):
=====================================================

── RASPBERRY PI J8 HEADER ──────────────────────────────────────────────────

  Physical Pin 4  (5V)       → SN74LS125AN Pin 14 (VCC)
  Physical Pin 9  (GND)      → SN74LS125AN Pin 7 (GND)  [shared ground bus]
  Physical Pin 11 (GPIO 17)  → Button DIAL   (other leg to GND)
  Physical Pin 12 (GPIO 18)  → SN74LS125AN Pin 2 (1A)   [LED data signal]
                               SN74LS125AN Pin 5 (2A)   [jumper same row]
  Physical Pin 13 (GPIO 27)  → Button PLAY   (other leg to GND)
  Physical Pin 14 (GND)      → all buttons other leg (common GND)
  Physical Pin 18 (GPIO 24)  → Button RECORD (other leg to GND)
  Physical Pin 21 (GND)      → External 5V supply GND   [shared ground!]

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

  VCC  → External 5V supply +5V
  GND  → External 5V supply GND  (shared ground)
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

── BUTTONS ──────────────────────────────────────────────────────────────────

  DIAL   button: one leg → Pi Physical Pin 11 (GPIO 17)
                 other leg → Pi Physical Pin 14 (GND)
  RECORD button: one leg → Pi Physical Pin 18 (GPIO 24)
                 other leg → Pi Physical Pin 14 (GND)
  PLAY   button: one leg → Pi Physical Pin 13 (GPIO 27)
                 other leg → Pi Physical Pin 14 (GND)
  (Internal pull-up resistors used — no external resistors needed)

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

REMOTE_IP      = "192.168.10.1"
REMOTE_USER    = "lampa"
LOCAL_USER     = "lampb"

BARESIP_HOST   = "127.0.0.1"
BARESIP_PORT   = 4444

MESSAGE_PATH   = "/home/group66/message.wav"

MIC_DEVICE     = "plughw:2,0"
SPEAKER_DEVICE = "plughw:1,0"

BTN_DIAL       = 17   # Physical pin 11
BTN_RECORD     = 24   # Physical pin 18
BTN_PLAY       = 27   # Physical pin 13

LED_PIN        = board.D18
LEDS_PER_STRIP = 15
STRIP_COUNT    = 2
LED_TOTAL      = LEDS_PER_STRIP * STRIP_COUNT   # 30
LED_BRIGHTNESS = 0.2

COLOR_IDLE     = (255, 147,  41)   # amber
COLOR_CALL     = (  0, 255,   0)   # green
COLOR_RECORD   = (255,   0,   0)   # red
COLOR_PLAY     = (  0,   0, 255)   # blue
COLOR_RING     = (255, 147,  41)   # amber flash
COLOR_ERROR    = (255,   0, 128)   # magenta — something went wrong
COLOR_OFF      = (  0,   0,   0)

# ── State ──────────────────────────────────────────────────────────────────

in_call        = False
is_recording   = False
record_proc    = None
play_proc      = None
flash_active   = False
_current_color = None   # track last LED color to avoid redundant writes

# ── LED setup ──────────────────────────────────────────────────────────────

try:
    pixels = neopixel.NeoPixel(
        LED_PIN,
        LED_TOTAL,
        brightness=LED_BRIGHTNESS,
        auto_write=False,
        pixel_order=neopixel.GRB
    )
    print("[LED] NeoPixel init OK")
except Exception as e:
    print(f"[LED] FATAL: could not init NeoPixel: {e}")
    sys.exit(1)

# ── LED helpers ────────────────────────────────────────────────────────────

def fill_pixels(color):
    """Write color to all pixels. Skips write if color unchanged."""
    global _current_color
    if color == _current_color:
        return
    try:
        pixels.fill(color)
        pixels.show()
        _current_color = color
    except Exception as e:
        print(f"[LED] ERROR during fill: {e}")

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

def flash_error():
    """Brief magenta flash to signal something went wrong, then return to idle."""
    def _flash():
        for _ in range(3):
            fill_pixels(COLOR_ERROR)
            time.sleep(0.2)
            fill_pixels(COLOR_OFF)
            time.sleep(0.2)
        set_idle()
    threading.Thread(target=_flash, daemon=True).start()

def start_flash():
    """Flash amber for incoming call."""
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

def baresip_cmd(cmd, retries=2):
    """
    Send a command to baresip over TCP. Retries on failure.
    Returns True if sent successfully, False otherwise.
    """
    for attempt in range(1, retries + 1):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect((BARESIP_HOST, BARESIP_PORT))
            s.send((cmd + "\n").encode())
            s.close()
            print(f"[baresip] sent: {cmd}")
            return True
        except ConnectionRefusedError:
            print(f"[baresip] ERROR: connection refused — is baresip running? (attempt {attempt}/{retries})")
        except socket.timeout:
            print(f"[baresip] ERROR: timeout sending '{cmd}' (attempt {attempt}/{retries})")
        except OSError as e:
            print(f"[baresip] ERROR: {e} (attempt {attempt}/{retries})")
        if attempt < retries:
            time.sleep(0.5)
    print(f"[baresip] gave up sending: {cmd}")
    return False

def baresip_running():
    """Quick check: can we reach the baresip control socket?"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect((BARESIP_HOST, BARESIP_PORT))
        s.close()
        return True
    except Exception:
        return False

# ── Baresip event listener ─────────────────────────────────────────────────

def start_event_listener():
    """Listen for baresip events in a background thread."""
    def _listen():
        global in_call
        backoff = 2
        while True:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(10)
                s.connect((BARESIP_HOST, BARESIP_PORT))
                backoff = 2   # reset backoff on successful connect
                print("[EVENT] Connected to baresip")
                buf = ""
                while True:
                    chunk = s.recv(1024).decode(errors="ignore")
                    if not chunk:
                        print("[EVENT] Socket closed by baresip")
                        break
                    buf += chunk
                    # Process line by line
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        print(f"[EVENT] {line}")
                        if "INCOMING" in line or "CALL_INCOMING" in line:
                            print("[EVENT] Incoming call — flashing")
                            in_call = False   # not established yet
                            start_flash()
                        elif "CALL_ESTABLISHED" in line:
                            print("[EVENT] Call established")
                            in_call = True
                            set_color(COLOR_CALL)
                        elif "CALL_CLOSED" in line:
                            print("[EVENT] Call ended")
                            in_call = False
                            set_idle()
                        elif "CALL_LOCAL_SDP" in line or "CALL_REMOTE_SDP" in line:
                            pass   # noisy, ignore
                s.close()
            except socket.timeout:
                print("[EVENT] Socket timeout — retrying")
            except ConnectionRefusedError:
                print(f"[EVENT] baresip not reachable — retrying in {backoff}s")
            except OSError as e:
                print(f"[EVENT] Socket error: {e} — retrying in {backoff}s")
            except Exception as e:
                print(f"[EVENT] Unexpected error: {e} — retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)   # exponential backoff, cap at 30s
    threading.Thread(target=_listen, daemon=True).start()

# ── Button actions ─────────────────────────────────────────────────────────

def action_dial():
    """Dial the other lamp or hang up if already in call."""
    global in_call

    if not baresip_running():
        print("[DIAL] baresip not running — cannot dial")
        flash_error()
        return

    if not in_call:
        target = f"sip:{REMOTE_USER}@{REMOTE_IP}"
        print(f"[DIAL] Calling {target} ...")
        ok = baresip_cmd(f"/dial {target}")
        if ok:
            in_call = True
            set_color(COLOR_CALL)
        else:
            print("[DIAL] Failed to send dial command")
            flash_error()
    else:
        print("[DIAL] Hanging up...")
        ok = baresip_cmd("/hangup")
        if not ok:
            print("[DIAL] Hangup command failed — resetting state anyway")
        in_call = False
        set_idle()

def action_record_start():
    """Start recording a voice message via USB mic."""
    global is_recording, record_proc

    if in_call:
        print("[RECORD] In call — ignoring")
        return
    if is_recording:
        print("[RECORD] Already recording — ignoring")
        return

    # Make sure output directory exists
    msg_dir = os.path.dirname(MESSAGE_PATH)
    if msg_dir and not os.path.isdir(msg_dir):
        try:
            os.makedirs(msg_dir, exist_ok=True)
        except OSError as e:
            print(f"[RECORD] Cannot create directory {msg_dir}: {e}")
            flash_error()
            return

    print(f"[RECORD] Recording to {MESSAGE_PATH} ...")
    try:
        record_proc = subprocess.Popen(
            ["arecord", "-D", MIC_DEVICE, "-f", "S16_LE",
             "-r", "44100", "-c", "1", "-t", "wav", MESSAGE_PATH],
            stderr=subprocess.PIPE
        )
        is_recording = True
        set_color(COLOR_RECORD)   # LED after process starts → less click
    except FileNotFoundError:
        print("[RECORD] ERROR: arecord not found — install alsa-utils")
        flash_error()
    except Exception as e:
        print(f"[RECORD] ERROR starting arecord: {e}")
        flash_error()

def action_record_stop():
    """Stop recording and save the message."""
    global is_recording, record_proc

    if not is_recording or record_proc is None:
        return

    try:
        record_proc.terminate()
        record_proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        print("[RECORD] arecord did not stop — killing")
        record_proc.kill()
        record_proc.wait()
    except Exception as e:
        print(f"[RECORD] ERROR stopping arecord: {e}")
    finally:
        record_proc = None
        is_recording = False

    if os.path.exists(MESSAGE_PATH) and os.path.getsize(MESSAGE_PATH) > 44:
        print(f"[RECORD] Saved {os.path.getsize(MESSAGE_PATH)} bytes to {MESSAGE_PATH}")
    else:
        print(f"[RECORD] WARNING: message file missing or empty at {MESSAGE_PATH}")

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
        print(f"[PLAY] No message found at {MESSAGE_PATH}")
        flash_error()
        return
    if os.path.getsize(MESSAGE_PATH) <= 44:
        print(f"[PLAY] Message file is empty (header only) — nothing to play")
        flash_error()
        return

    # Stop any current playback
    if play_proc and play_proc.poll() is None:
        try:
            play_proc.terminate()
            play_proc.wait(timeout=2)
        except Exception:
            play_proc.kill()
        time.sleep(0.2)

    print(f"[PLAY] Playing {MESSAGE_PATH} via {SPEAKER_DEVICE} ...")
    try:
        play_proc = subprocess.Popen(
            ["aplay", "-D", SPEAKER_DEVICE, MESSAGE_PATH],
            stderr=subprocess.PIPE
        )
        time.sleep(0.1)           # let audio start before LED burst → less click
        set_color(COLOR_PLAY)

        def _wait_for_end():
            try:
                stdout, stderr = play_proc.communicate(timeout=120)
                if play_proc.returncode != 0 and stderr:
                    print(f"[PLAY] aplay error: {stderr.decode(errors='ignore').strip()}")
            except subprocess.TimeoutExpired:
                print("[PLAY] Playback timed out — killing")
                play_proc.kill()
            except Exception as e:
                print(f"[PLAY] Error waiting for playback: {e}")
            finally:
                time.sleep(0.2)
                set_idle()

        threading.Thread(target=_wait_for_end, daemon=True).start()

    except FileNotFoundError:
        print("[PLAY] ERROR: aplay not found — install alsa-utils")
        flash_error()
    except Exception as e:
        print(f"[PLAY] ERROR starting aplay: {e}")
        flash_error()

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

    # Stop recording if active
    if is_recording:
        action_record_stop()

    # Stop playback if active
    if play_proc and play_proc.poll() is None:
        try:
            play_proc.terminate()
            play_proc.wait(timeout=2)
        except Exception:
            play_proc.kill()

    # Hang up if in call
    if in_call:
        baresip_cmd("/hangup")

    time.sleep(0.1)

    # Always try to turn off LEDs
    try:
        fill_pixels(COLOR_OFF)
    except Exception as e:
        print(f"[EXIT] Could not turn off LEDs: {e}")

    try:
        GPIO.cleanup()
    except Exception as e:
        print(f"[EXIT] GPIO cleanup error: {e}")

    print("[EXIT] Done.")

# ── Main ──────────────────────────────────────────────────────────────────

def main():
    setup_buttons()
    set_idle()
    start_event_listener()

    def handle_exit(sig, frame):
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT,  handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # Warn if baresip isn't reachable at startup
    if not baresip_running():
        print("[WARN] baresip not reachable on startup — dial button will not work until it is running")
        print("[WARN] Start it with: baresip -d")

    print("\n══════════════════════════════════════")
    print("  Lamp intercom controller ready")
    print("══════════════════════════════════════")
    print(f"  DIAL   (BCM17 / Pin 11) — call / hang up")
    print(f"  RECORD (BCM24 / Pin 18) — hold to record, release to save")
    print(f"  PLAY   (BCM27 / Pin 13) — play last message")
    print(f"  LEDs   (BCM18 / Pin 12) → SN74LS125AN → 2× strips (30 LEDs)")
    print(f"  Speaker via GF1002 on 3.5mm aux")
    print(f"  Mic    via USB ({MIC_DEVICE})")
    print(f"  Remote  sip:{REMOTE_USER}@{REMOTE_IP}")
    print("  Ctrl+C to quit\n")

    last_state = {
        BTN_DIAL:   GPIO.HIGH,
        BTN_RECORD: GPIO.HIGH,
        BTN_PLAY:   GPIO.HIGH,
    }

    try:
        while True:
            for pin in (BTN_DIAL, BTN_RECORD, BTN_PLAY):
                try:
                    current = GPIO.input(pin)
                except Exception as e:
                    print(f"[GPIO] Error reading pin {pin}: {e}")
                    continue

                prev = last_state[pin]

                # Pressed (active LOW)
                if current == GPIO.LOW and prev == GPIO.HIGH:
                    time.sleep(0.02)   # debounce
                    try:
                        if GPIO.input(pin) == GPIO.LOW:
                            if pin == BTN_DIAL:
                                action_dial()
                            elif pin == BTN_RECORD:
                                action_record_start()
                            elif pin == BTN_PLAY:
                                action_play()
                    except Exception as e:
                        print(f"[GPIO] Error handling press on pin {pin}: {e}")
                        flash_error()

                # Released
                if current == GPIO.HIGH and prev == GPIO.LOW:
                    try:
                        if pin == BTN_RECORD:
                            action_record_stop()
                    except Exception as e:
                        print(f"[GPIO] Error handling release on pin {pin}: {e}")

                last_state[pin] = current

            time.sleep(0.01)

    except Exception as e:
        print(f"[MAIN] Unexpected error in main loop: {e}")
        cleanup()
        sys.exit(1)

if __name__ == "__main__":
    main()