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
  Physical Pin 19 (GPIO 10)  → LED data signal
  Physical Pin 13 (GPIO 27)  → Button PLAY   (other leg to GND)
  Physical Pin 14 (GND)      → all buttons other leg (common GND)
  Physical Pin 18 (GPIO 24)  → Button RECORD (other leg to GND)
  Physical Pin 21 (GND)      → External 5V supply GND   [shared ground!]

── EXTERNAL 5V SUPPLY ───────────────────────────────────────────────────────
  +5V → LED Strip Red (VCC)
  GND → LED Strip Black (GND)
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
    Green → Physical Pin 12 (GPIO 18)

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
  baresip config must have the ctrl_tcp module loaded, e.g.:
      module                  ctrl_tcp.so
  in the [modules] section of ~/.baresip/config

── RUN ──────────────────────────────────────────────────────────────────────
  sudo python3 lamp_control.py

── LED STATES ───────────────────────────────────────────────────────────────
  Amber  (solid)   → idle
  Green  (solid)   → in call / outgoing call connected
  White  (pulsing) → incoming call ringing
  Red    (solid)   → recording voice message
  Blue   (solid)   → playing voice message
  Magenta (flash)  → error

── ABOUT THE BARESIP ctrl_tcp PROTOCOL ──────────────────────────────────────
  ctrl_tcp speaks JSON framed as a netstring: "<byte-length>:<json-payload>,"
  Command:   {"command": "dial", "params": "sip:alice@atlanta.com"}
  Sent as:   55:{"command": "dial", "params": "sip:alice@atlanta.com"},
"""
import time
import math
import signal
import sys
import socket
import subprocess
import os
import json
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
BTN_COLOR      = 22   # Physical pin 15 

LED_PIN        = board.D10
LEDS_PER_STRIP = 15
STRIP_COUNT    = 2
LED_TOTAL      = LEDS_PER_STRIP * STRIP_COUNT   # 30
LED_BRIGHTNESS = 0.2

COLOR_IDLE     = (255, 147,  41)   # amber
COLOR_CALL     = (  0, 255,   0)   # green
COLOR_RECORD   = (255,   0,   0)   # red
COLOR_PLAY     = (  0,   0, 255)   # blue
COLOR_RING     = (255, 255, 255)   # white (pulsing for incoming call)
COLOR_ERROR    = (255,   0, 128)   # magenta
COLOR_OFF      = (  0,   0,   0)
COLOR_CYCLE = [
    (255,   0,   0),   # red
    (0,   255,   0),   # green
    (0,     0, 255),   # blue
    (255, 255,   0),   # yellow
    (0,   255, 255),   # cyan
    (255,   0, 255),   # magenta
    (255, 255, 255),   # white
]

COLOR_CYCLE_NAMES = [
    "Red",
    "Green",
    "Blue",
    "Yellow",
    "Cyan",
    "Magenta",
    "White",
]

_color_index = -1

# ── State ──────────────────────────────────────────────────────────────────
in_call      = False
is_recording = False
record_proc  = None
play_proc    = None

# ── LED engine ─────────────────────────────────────────────────────────────
# All NeoPixel writes happen on ONE dedicated thread (_led_worker).
# Other threads post requests via _post_led(); the worker wakes on _led_event.
# _led_stop tells the worker to exit cleanly before deinit() is called.

_led_lock    = threading.Lock()
_led_event   = threading.Event()
_led_stop    = threading.Event()
_led_request = {"mode": "solid", "color": COLOR_IDLE, "flashes": 6}

try:
    pixels = neopixel.NeoPixel(
        LED_PIN,
        LED_TOTAL,
        brightness=LED_BRIGHTNESS,
        auto_write=False,
        pixel_order=neopixel.GRB,
    )
    print("[LED] NeoPixel init OK")
except Exception as e:
    print(f"[LED] FATAL: could not init NeoPixel: {e}")
    sys.exit(1)


def _led_worker():
    """Single thread that owns all NeoPixel writes."""
    flash_count   = 0
    flash_state   = True
    pulse_phase   = 0.0
    current_mode  = None
    current_color = None

    while not _led_stop.is_set():
        with _led_lock:
            req = dict(_led_request)

        mode  = req["mode"]
        color = req["color"]

        # Reset animation state on mode/color change
        if mode != current_mode or color != current_color:
            current_mode  = mode
            current_color = color
            flash_count   = req.get("flashes", 6)
            flash_state   = True
            pulse_phase   = 0.0

        try:
            if mode == "solid":
                pixels.fill(color)
                pixels.show()
                _led_event.clear()
                # Wait with timeout so _led_stop is checked regularly
                _led_event.wait(timeout=1.0)

            elif mode == "flash":
                if flash_count > 0:
                    pixels.fill(color if flash_state else COLOR_OFF)
                    pixels.show()
                    flash_state = not flash_state
                    flash_count -= 1
                    _led_event.wait(timeout=0.15)
                    _led_event.clear()
                else:
                    # Finished flashing — go back to idle
                    _post_led("solid", COLOR_IDLE)

            elif mode == "pulse":
                brightness = (math.sin(pulse_phase) + 1.0) / 2.0
                scaled = tuple(int(c * brightness) for c in color)
                pixels.fill(scaled)
                pixels.show()
                pulse_phase += 0.12
                if pulse_phase > 2 * math.pi:
                    pulse_phase -= 2 * math.pi
                _led_event.wait(timeout=0.03)
                _led_event.clear()

        except Exception as e:
            print(f"[LED] worker error: {e}")
            _led_event.wait(timeout=0.5)

    print("[LED] worker exiting")


def _post_led(mode, color, flashes=6):
    """Thread-safe: post a new LED request and wake the worker."""
    with _led_lock:
        _led_request["mode"]    = mode
        _led_request["color"]   = color
        _led_request["flashes"] = flashes
    _led_event.set()


# Public LED helpers --------------------------------------------------------

def set_idle():
    _post_led("solid", COLOR_IDLE)

def set_color(color):
    _post_led("solid", color)

def flash_error():
    _post_led("flash", COLOR_ERROR, flashes=6)

def start_pulse(color=COLOR_RING):
    _post_led("pulse", color)

def action_color_cycle():
    global _color_index

    if in_call:
        print("[COLOR] In call")
        return

    if is_recording:
        print("[COLOR] Recording")
        return

    if play_proc and play_proc.poll() is None:
        print("[COLOR] Playing")
        return

    _color_index = (_color_index + 1) % len(COLOR_CYCLE)

    color = COLOR_CYCLE[_color_index]
    name = COLOR_CYCLE_NAMES[_color_index]

    print(f"[COLOR] {name}")

    _post_led("solid", color)
    
# Start LED worker thread ---------------------------------------------------
_led_thread = threading.Thread(target=_led_worker, daemon=True, name="led-worker")
_led_thread.start()


# ── Baresip — single shared persistent connection ──────────────────────────
_baresip_proc = None
_bs_socket    = None
_bs_lock      = threading.Lock()
_bs_connected = threading.Event()


def reset_audio():
    """Kill any leftover aplay/arecord from a previous run so ALSA is free."""
    for proc in ("aplay", "arecord"):
        try:
            result = subprocess.run(["pkill", "-9", "-f", proc], capture_output=True)
            if result.returncode == 0:
                print(f"[AUDIO] killed leftover {proc}")
        except Exception as e:
            print(f"[AUDIO] pkill error for {proc}: {e}")
    time.sleep(0.3)


def start_baresip(wait_secs=8):
    global _baresip_proc
    if _bs_connected.is_set():
        print("[baresip] already connected")
        return True
    try:
        probe = socket.create_connection((BARESIP_HOST, BARESIP_PORT), timeout=1)
        probe.close()
        print("[baresip] already reachable")
        return True
    except Exception:
        pass

    if _baresip_proc is None or _baresip_proc.poll() is not None:
        print("[baresip] launching /usr/bin/baresip -d ...")
        try:
            _baresip_proc = subprocess.Popen(
                ["/usr/bin/baresip", "-d", "-f", "/home/group66/.baresip"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            print("[baresip] ERROR: /usr/bin/baresip not found")
            return False
        except Exception as e:
            print(f"[baresip] ERROR launching: {e}")
            return False

    deadline = time.time() + wait_secs
    while time.time() < deadline:
        try:
            probe = socket.create_connection((BARESIP_HOST, BARESIP_PORT), timeout=1)
            probe.close()
            print("[baresip] socket is up")
            return True
        except Exception:
            time.sleep(0.5)

    print(f"[baresip] did not become ready within {wait_secs}s")
    return False


def baresip_running():
    return _bs_connected.is_set()


# ── Netstring framing ───────────────────────────────────────────────────────

def _netstring_encode(payload: bytes) -> bytes:
    return str(len(payload)).encode() + b":" + payload + b","


def _netstring_decode_buffer(buf: bytes):
    messages = []
    while True:
        colon = buf.find(b":")
        if colon == -1:
            break
        length_field = buf[:colon]
        if not length_field.isdigit():
            buf = buf[colon + 1:]
            continue
        length = int(length_field)
        start  = colon + 1
        end    = start + length
        if len(buf) < end + 1:
            break
        if buf[end:end + 1] != b",":
            print("[baresip] netstring framing error — resyncing")
            buf = buf[start:]
            continue
        messages.append(buf[start:end])
        buf = buf[end + 1:]
    return messages, buf


def baresip_cmd(command, params=None, token=None):
    global _bs_socket
    msg = {"command": command}
    if params:
        msg["params"] = params
    if token:
        msg["token"] = token
    payload = json.dumps(msg).encode("utf-8")
    framed  = _netstring_encode(payload)
    with _bs_lock:
        if _bs_socket is None:
            print(f"[baresip] not connected — cannot send: {command} {params or ''}")
            return False
        try:
            _bs_socket.sendall(framed)
            print(f"[baresip] sent: {command} {params or ''}")
            return True
        except Exception as e:
            print(f"[baresip] send error: {e}")
            return False


def start_event_listener():
    def _listen():
        global in_call, _bs_socket
        backoff = 2
        while True:
            while True:
                try:
                    sock = socket.create_connection((BARESIP_HOST, BARESIP_PORT), timeout=3)
                    sock.settimeout(None)
                    break
                except Exception:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30)
            with _bs_lock:
                _bs_socket = sock
            _bs_connected.set()
            backoff = 2
            print("[baresip] connected (shared socket)")
            buf = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        print("[baresip] socket closed by remote")
                        break
                    buf += chunk
                    msgs, buf = _netstring_decode_buffer(buf)
                    for raw in msgs:
                        try:
                            obj = json.loads(raw.decode(errors="ignore"))
                        except json.JSONDecodeError as e:
                            print(f"[baresip] bad JSON: {e}")
                            continue

                        if obj.get("event"):
                            etype = obj.get("type", "")
                            print(f"[EVENT] {etype} {obj}")
                            if etype == "CALL_INCOMING":
                                print("[EVENT] Incoming call — pulsing white")
                                in_call = False
                                start_pulse(COLOR_RING)
                            elif etype == "CALL_ESTABLISHED":
                                print("[EVENT] Call established — solid green")
                                in_call = True
                                set_color(COLOR_CALL)
                            elif etype == "CALL_CLOSED":
                                print("[EVENT] Call ended — amber idle")
                                in_call = False
                                set_idle()
                        elif obj.get("response"):
                            print(f"[baresip] response: ok={obj.get('ok')} "
                                  f"data={obj.get('data')!r} token={obj.get('token')}")
                        else:
                            print(f"[baresip] unrecognized: {obj}")
            except Exception as e:
                print(f"[baresip] socket error: {e}")
            finally:
                with _bs_lock:
                    _bs_socket = None
                _bs_connected.clear()
                try:
                    sock.close()
                except Exception:
                    pass
                print(f"[baresip] reconnecting in {backoff}s...")
                time.sleep(backoff)

    threading.Thread(target=_listen, daemon=True, name="baresip-listener").start()


# ── Button actions ─────────────────────────────────────────────────────────

def action_dial():
    global in_call
    if not _bs_connected.is_set():
        print("[DIAL] baresip not connected — attempting to start...")
        set_color(COLOR_ERROR)
        if not start_baresip(wait_secs=8):
            print("[DIAL] could not start baresip — giving up")
            flash_error()
            return
        if not _bs_connected.wait(timeout=6):
            print("[DIAL] socket not ready after start — giving up")
            flash_error()
            return
        print("[DIAL] baresip ready")

    if not in_call:
        target = f"sip:{REMOTE_USER}@{REMOTE_IP}"
        print(f"[DIAL] Calling {target} ...")
        ok = baresip_cmd("dial", target)
        if ok:
            in_call = True
            set_color(COLOR_CALL)
        else:
            print("[DIAL] Failed to send dial command")
            flash_error()
    else:
        print("[DIAL] Hanging up...")
        ok = baresip_cmd("hangup")
        if not ok:
            print("[DIAL] Hangup command failed — resetting state anyway")
        in_call = False
        set_idle()


def action_record_toggle():
    """Single press: start recording. Press again: stop recording."""
    global is_recording, record_proc

    if in_call:
        print("[RECORD] In call — ignoring")
        return

    if not is_recording:
        # ── Start recording ────────────────────────────────────────────────
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
                stderr=subprocess.PIPE,
            )
            is_recording = True
            set_color(COLOR_RECORD)   # solid red
        except FileNotFoundError:
            print("[RECORD] ERROR: arecord not found — install alsa-utils")
            flash_error()
        except Exception as e:
            print(f"[RECORD] ERROR starting arecord: {e}")
            flash_error()

    else:
        # ── Stop recording ─────────────────────────────────────────────────
        print("[RECORD] Stopping recording...")
        if record_proc is not None:
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
                record_proc  = None
                is_recording = False

        if os.path.exists(MESSAGE_PATH) and os.path.getsize(MESSAGE_PATH) > 44:
            print(f"[RECORD] Saved {os.path.getsize(MESSAGE_PATH)} bytes to {MESSAGE_PATH}")
        else:
            print(f"[RECORD] WARNING: message file missing or empty at {MESSAGE_PATH}")

        if _color_index >= 0:
            _post_led("solid", COLOR_CYCLE[_color_index])
        else:
            set_idle()


def action_play():
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

    if play_proc and play_proc.poll() is None:
        try:
            play_proc.terminate()
            play_proc.wait(timeout=2)
        except Exception:
            play_proc.kill()
            play_proc.wait()
        time.sleep(0.2)

    print(f"[PLAY] Playing {MESSAGE_PATH} via {SPEAKER_DEVICE} ...")
    try:
        play_proc = subprocess.Popen(
            ["aplay", "-D", SPEAKER_DEVICE, MESSAGE_PATH],
            stderr=subprocess.PIPE,
        )
        time.sleep(0.1)
        set_color(COLOR_PLAY)   # solid blue

        def _wait_for_end():
            try:
                _, stderr = play_proc.communicate(timeout=120)
                if play_proc.returncode != 0 and stderr:
                    print(f"[PLAY] aplay error: {stderr.decode(errors='ignore').strip()}")
            except subprocess.TimeoutExpired:
                print("[PLAY] Playback timed out — killing")
                play_proc.kill()
            except Exception as e:
                print(f"[PLAY] Error waiting for playback: {e}")
            finally:
                time.sleep(0.2)
                if _color_index >= 0:
                    _post_led("solid", COLOR_CYCLE[_color_index])
                else:
                    set_idle()

        threading.Thread(target=_wait_for_end, daemon=True, name="play-wait").start()
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
    for pin in (BTN_DIAL, BTN_RECORD, BTN_PLAY, BTN_COLOR):
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    print(f"[GPIO] Buttons ready on BCM {BTN_DIAL}, {BTN_RECORD}, {BTN_PLAY}, {BTN_COLOR}")


# ── Cleanup ────────────────────────────────────────────────────────────────

def cleanup():
    print("\n[EXIT] Cleaning up...")

    # Stop audio processes first
    if is_recording:
        action_record_toggle()
    if record_proc and record_proc.poll() is None:
        record_proc.kill()
        record_proc.wait()
    if play_proc and play_proc.poll() is None:
        try:
            play_proc.terminate()
            play_proc.wait(timeout=3)
        except Exception:
            play_proc.kill()
            play_proc.wait()

    # Hang up if in call
    if in_call:
        baresip_cmd("hangup")

    # Stop the LED worker cleanly BEFORE touching pixels
    _led_stop.set()      # signal worker to exit its loop
    _led_event.set()     # wake it immediately so it sees the flag
    _led_thread.join(timeout=1.0)   # wait for it to actually exit

    # Now we are the only one touching pixels — safe to deinit
    try:
        pixels.fill((0, 0, 0))
        pixels.show()
        print("[LED] LEDs off")
    except Exception as e:
        print(f"[LED] final fill error: {e}")
    try:
        pixels.deinit()
        print("[LED] deinit OK")
    except Exception as e:
        print(f"[LED] deinit error: {e}")

    try:
        GPIO.cleanup()
    except Exception as e:
        print(f"[EXIT] GPIO cleanup error: {e}")

    if _baresip_proc and _baresip_proc.poll() is None:
        print("[EXIT] Stopping baresip...")
        try:
            _baresip_proc.terminate()
            _baresip_proc.wait(timeout=3)
        except Exception:
            _baresip_proc.kill()

    print("[EXIT] Done.")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    # Kill any leftover aplay/arecord from a previous run
    reset_audio()

    # Force strip to known black state before anything else
    try:
        pixels.fill((0, 0, 0))
        pixels.show()
        time.sleep(0.1)
    except Exception as e:
        print(f"[LED] startup reset error: {e}")

    setup_buttons()
    set_idle()   # amber
    start_event_listener()

    def handle_exit(sig, frame):
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT,  handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    print("[baresip] checking / starting baresip...")
    if start_baresip(wait_secs=8):
        print("[baresip] process up — waiting for socket...")
        if _bs_connected.wait(timeout=8):
            print("[baresip] ready at startup")
        else:
            print("[WARN] baresip started but socket not ready yet — will connect soon")
    else:
        print("[WARN] baresip not ready — will retry when DIAL is pressed")

    print("\n══════════════════════════════════════")
    print("  Lamp intercom controller ready")
    print("══════════════════════════════════════")
    print(f"  DIAL   (BCM17 / Pin 11) — call / hang up")
    print(f"  RECORD (BCM24 / Pin 18) — press to start, press again to stop")
    print(f"  PLAY   (BCM27 / Pin 13) — play last message")
    print(f"  LEDs   (BCM18 / Pin 12) → 2× strips (30 LEDs)")
    print(f"    Amber        = idle")
    print(f"    Green        = in call")
    print(f"    White pulse  = incoming ring")
    print(f"    Red          = recording")
    print(f"    Blue         = playing")
    print(f"    Magenta flash = error")
    print(f"  Speaker via GF1002 on 3.5mm aux")
    print(f"  Mic    via USB ({MIC_DEVICE})")
    print(f"  Remote  sip:{REMOTE_USER}@{REMOTE_IP}")
    print("  Ctrl+C to quit\n")

    last_state = {
        BTN_DIAL:   GPIO.HIGH,
        BTN_RECORD: GPIO.HIGH,
        BTN_PLAY:   GPIO.HIGH,
        BTN_COLOR:  GPIO.HIGH,
    }

    try:
        while True:
            for pin in (BTN_DIAL, BTN_RECORD, BTN_PLAY, BTN_COLOR):
                try:
                    current = GPIO.input(pin)
                except Exception as e:
                    print(f"[GPIO] Error reading pin {pin}: {e}")
                    continue

                prev = last_state[pin]

                # Pressed (active LOW) — single read, no double-confirm
                if current == GPIO.LOW and prev == GPIO.HIGH:
                    time.sleep(0.02)   # debounce delay
                    if pin == BTN_DIAL:
                        action_dial()
                    elif pin == BTN_RECORD:
                        action_record_toggle()
                    elif pin == BTN_PLAY:
                        action_play()
                    elif pin == BTN_COLOR:
                        action_color_cycle()

                last_state[pin] = current

            time.sleep(0.01)

    except Exception as e:
        print(f"[MAIN] Unexpected error in main loop: {e}")
        cleanup()
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL] {e}")
    finally:
        cleanup()