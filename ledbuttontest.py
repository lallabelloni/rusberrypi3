#!/usr/bin/env python3
"""
LED + Button diagnostic test — Raspberry Pi 3 Model B
======================================================

Tests all three buttons and both LED strips WITHOUT the SN74LS125AN buffer.
Wire GPIO 18 (Physical Pin 12) directly to both strip DATA lines for this test.

MINIMAL TEST WIRING (bypass the buffer IC):
─────────────────────────────────────────────────────────────────────────────

  Pi Physical Pin 9  (GND)  ─── External 5V supply GND   ← SHARED GROUND

  Pi Physical Pin 11 (GPIO 17) → Button DIAL   other leg → GND (Pin 14)
  Pi Physical Pin 12 (GPIO 18) → LED Strip 1 Data (Green wire)  ← direct!
                                  LED Strip 2 Data (Green wire)  ← direct!
  Pi Physical Pin 13 (GPIO 27) → Button PLAY   other leg → GND (Pin 14)
  Pi Physical Pin 14 (GND)     → all button GND legs (common)
  Pi Physical Pin 18 (GPIO 24) → Button RECORD other leg → GND (Pin 14)

  External 5V PSU +5V  → LED Strip 1 Red (VCC)
  External 5V PSU +5V  → LED Strip 2 Red (VCC)
  External 5V PSU GND  → LED Strip 1 Black (GND)
  External 5V PSU GND  → LED Strip 2 Black (GND)
  External 5V PSU GND  → Pi Physical Pin 9 (GND)  ← essential shared GND!

─────────────────────────────────────────────────────────────────────────────
ABOUT THE SN74LS125AN:
  Not needed for this test. WS2812B data threshold is ~0.7×VCC = ~3.5V.
  Pi GPIO outputs 3.3V — borderline but usually works in practice.
  The buffer upgrades to 5V logic for reliability in a final build.
  Skip it now; add it back if strips behave erratically.
─────────────────────────────────────────────────────────────────────────────

INSTALL:
  sudo apt update
  sudo apt install python3-rpi.gpio
  sudo pip3 install rpi_ws281x adafruit-circuitpython-neopixel adafruit-blinka --break-system-packages

RUN:
  sudo python3 led_button_test.py
"""

import time
import signal
import sys
import board
import neopixel
import RPi.GPIO as GPIO

# ── Pin definitions (BCM) ──────────────────────────────────────────────────

BTN_DIAL   = 17   # Physical pin 11
BTN_RECORD = 24   # Physical pin 18
BTN_PLAY   = 27   # Physical pin 13

LED_PIN    = board.D18   # Physical pin 12 → direct to both strip data lines

LEDS_PER_STRIP = 15
STRIP_COUNT    = 2
LED_TOTAL      = LEDS_PER_STRIP * STRIP_COUNT   # 30 LEDs

# ── Colors (R, G, B) ───────────────────────────────────────────────────────

RED    = (255,   0,   0)
GREEN  = (  0, 255,   0)
BLUE   = (  0,   0, 255)
AMBER  = (255, 147,  41)
WHITE  = (255, 255, 255)
OFF    = (  0,   0,   0)

# ── Setup ──────────────────────────────────────────────────────────────────

print()
print("══════════════════════════════════════════════")
print("  LED + Button diagnostic test")
print("══════════════════════════════════════════════")
print()
print("  GPIO pin map (BCM / Physical):")
print("  ─────────────────────────────────────────")
print(f"  LED data  → BCM 18  / Physical Pin 12")
print(f"  BTN DIAL  → BCM 17  / Physical Pin 11  (pull-up, active LOW)")
print(f"  BTN RECORD→ BCM 24  / Physical Pin 18  (pull-up, active LOW)")
print(f"  BTN PLAY  → BCM 27  / Physical Pin 13  (pull-up, active LOW)")
print(f"  GND       → Physical Pins 6, 9, 14, 25 (any GND)")
print(f"  LED strip GND must share GND with Pi!")
print()
print("  LED strip: 30 LEDs total (2 × 15), WS2812B / GRB order")
print("  Buffer IC (SN74LS125AN): BYPASSED for this test")
print()

pixels = neopixel.NeoPixel(
    LED_PIN,
    LED_TOTAL,
    brightness=0.2,
    auto_write=False,
    pixel_order=neopixel.GRB
)

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
for pin in (BTN_DIAL, BTN_RECORD, BTN_PLAY):
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def fill(color):
    pixels.fill(color)
    pixels.show()

def cleanup(sig=None, frame=None):
    print("\n[EXIT] Turning off LEDs and cleaning up GPIO...")
    fill(OFF)
    GPIO.cleanup()
    print("[EXIT] Done.")
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

# ── Startup LED test ───────────────────────────────────────────────────────

print("── Startup LED test ──────────────────────────────────────")
print("  Cycling R → G → B → WHITE → AMBER to verify all 30 LEDs")
print()

for label, color in [("RED", RED), ("GREEN", GREEN), ("BLUE", BLUE), ("WHITE", WHITE), ("AMBER", AMBER)]:
    print(f"  [{label}]")
    fill(color)
    time.sleep(0.8)

fill(OFF)
time.sleep(0.3)

# Pixel-by-pixel chase so you can spot dead LEDs
print("  Pixel chase (spot any dead LEDs)...")
for i in range(LED_TOTAL):
    pixels[i] = GREEN
    pixels.show()
    time.sleep(0.05)
    pixels[i] = OFF
    pixels.show()

print()
print("── Button test ───────────────────────────────────────────")
print("  DIAL   (BCM17 / Pin 11) → lights strip AMBER")
print("  RECORD (BCM24 / Pin 18) → lights strip RED  (hold = on, release = off)")
print("  PLAY   (BCM27 / Pin 13) → lights strip BLUE")
print("  Press Ctrl+C to exit")
print()

# ── Main button test loop ─────────────────────────────────────────────────

last = {BTN_DIAL: GPIO.HIGH, BTN_RECORD: GPIO.HIGH, BTN_PLAY: GPIO.HIGH}

BUTTON_NAMES = {
    BTN_DIAL:   ("DIAL  ", "BCM17 / Pin 11", AMBER),
    BTN_RECORD: ("RECORD", "BCM24 / Pin 18", RED),
    BTN_PLAY:   ("PLAY  ", "BCM27 / Pin 13", BLUE),
}

while True:
    for pin in (BTN_DIAL, BTN_RECORD, BTN_PLAY):
        now  = GPIO.input(pin)
        prev = last[pin]
        name, loc, color = BUTTON_NAMES[pin]

        if now == GPIO.LOW and prev == GPIO.HIGH:
            time.sleep(0.02)                        # debounce
            if GPIO.input(pin) == GPIO.LOW:
                print(f"  ▼ PRESSED  {name} ({loc})  → LEDs {color}")
                fill(color)

        if now == GPIO.HIGH and prev == GPIO.LOW:
            print(f"  ▲ RELEASED {name} ({loc})  → LEDs OFF")
            fill(OFF)

        last[pin] = now

    time.sleep(0.01)