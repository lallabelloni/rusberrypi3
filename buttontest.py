#!/usr/bin/env python3
"""
LED strip color changer — Raspberry Pi 3 Model B (v1.2)
--------------------------------------------------------
Hardware (BCM GPIO numbering used throughout):

  LED strip 1  DIN → BCM GPIO2  (physical pin 3,  WiringPi 8)
  LED strip 2  DIN → BCM GPIO3  (physical pin 5,  WiringPi 9)
  Button 1         → BCM GPIO17 (physical pin 11, WiringPi 0)
  Button 2         → BCM GPIO18 (physical pin 12, WiringPi 1)
  Button 3         → BCM GPIO27 (physical pin 13, WiringPi 2)
  Speaker          → 3.5 mm audio jack (handled by OS/aplay)
  Mic              → USB

Requirements:
  sudo pip3 install rpi_ws281x RPi.GPIO --break-system-packages

Run with sudo (required for DMA/PWM access by rpi_ws281x):
  sudo python3 led_buttons.py
"""

import time
import signal
import sys
import RPi.GPIO as GPIO
from rpi_ws281x import PixelStrip, Color

# ── LED strip configuration ────────────────────────────────────────────────
LED_COUNT      = 30       # Number of LEDs per strip — adjust to your strip length
LED_FREQ_HZ    = 800000   # Signal frequency (800kHz for WS2812B)
LED_DMA        = 10       # DMA channel (10 is safe for Pi 3)
LED_BRIGHTNESS = 128      # 0 (off) to 255 (full brightness)
LED_INVERT     = False    # True if using an inverting level shifter
LED_CHANNEL    = 0        # PWM channel (0 for GPIO18 primary, but we use GPIO2/3 via DMA)

# GPIO pins for each strip's data line (BCM numbering)
STRIP1_PIN = 2   # Physical pin 3  / WiringPi 8
STRIP2_PIN = 3   # Physical pin 5  / WiringPi 9

# GPIO pins for buttons (BCM numbering)
BTN1_PIN = 17    # Physical pin 11 / WiringPi 0
BTN2_PIN = 18    # Physical pin 12 / WiringPi 1
BTN3_PIN = 27    # Physical pin 13 / WiringPi 2

# ── Color palette — one color per button, cycling on repeated press ────────
# Add or change colors freely: (R, G, B)
COLORS = [
    ("Red",     Color(255,   0,   0)),
    ("Green",   Color(  0, 255,   0)),
    ("Blue",    Color(  0,   0, 255)),
    ("Yellow",  Color(255, 255,   0)),
    ("Cyan",    Color(  0, 255, 255)),
    ("Magenta", Color(255,   0, 255)),
    ("White",   Color(255, 255, 255)),
    ("Orange",  Color(255, 100,   0)),
    ("Off",     Color(  0,   0,   0)),
]

# Each button cycles through its own position in COLORS independently
btn_color_index = {BTN1_PIN: 0, BTN2_PIN: 0, BTN3_PIN: 0}

# Which strip(s) each button controls
# Button 1 → strip 1 only
# Button 2 → strip 2 only
# Button 3 → both strips
BTN_STRIP_MAP = {
    BTN1_PIN: ["strip1"],
    BTN2_PIN: ["strip2"],
    BTN3_PIN: ["strip1", "strip2"],
}

# ── Helpers ────────────────────────────────────────────────────────────────
def fill_strip(strip, color):
    """Paint every pixel on a strip with one color."""
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, color)
    strip.show()

def setup_strips():
    strip1 = PixelStrip(LED_COUNT, STRIP1_PIN, LED_FREQ_HZ, LED_DMA,
                        LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL)
    # Strip 2 uses a separate PixelStrip instance on a different GPIO.
    # rpi_ws281x supports multiple strips via different DMA channels.
    strip2 = PixelStrip(LED_COUNT, STRIP2_PIN, LED_FREQ_HZ, LED_DMA + 1,
                        LED_INVERT, LED_BRIGHTNESS, 1)
    strip1.begin()
    strip2.begin()
    fill_strip(strip1, Color(0, 0, 0))
    fill_strip(strip2, Color(0, 0, 0))
    return strip1, strip2

def setup_buttons():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in (BTN1_PIN, BTN2_PIN, BTN3_PIN):
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def button_pressed(pin, strip1, strip2):
    """Called when a button falls to LOW (pressed)."""
    strips_map = {"strip1": strip1, "strip2": strip2}

    # Advance this button's color index
    btn_color_index[pin] = (btn_color_index[pin] + 1) % len(COLORS)
    name, color = COLORS[btn_color_index[pin]]

    targets = BTN_STRIP_MAP[pin]
    print(f"Button BCM{pin} → {name} on {', '.join(targets)}")

    for target in targets:
        fill_strip(strips_map[target], color)

def cleanup(strip1, strip2):
    """Turn off LEDs and release GPIO on exit."""
    print("\nShutting down…")
    fill_strip(strip1, Color(0, 0, 0))
    fill_strip(strip2, Color(0, 0, 0))
    GPIO.cleanup()

# ── Main loop ──────────────────────────────────────────────────────────────
def main():
    strip1, strip2 = setup_strips()
    setup_buttons()

    # Startup blink so you can confirm both strips are wired correctly
    print("Startup test — flashing strips…")
    for color in (Color(255, 0, 0), Color(0, 255, 0), Color(0, 0, 255), Color(0, 0, 0)):
        fill_strip(strip1, color)
        fill_strip(strip2, color)
        time.sleep(0.3)

    print("Ready. Press buttons to cycle LED colors.")
    print("  Button 1 (BCM17 / pin 11) → strip 1")
    print("  Button 2 (BCM18 / pin 12) → strip 2")
    print("  Button 3 (BCM27 / pin 13) → both strips")
    print("Ctrl+C to quit.\n")

    # Graceful Ctrl+C handler
    def handle_exit(sig, frame):
        cleanup(strip1, strip2)
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # Track last state for simple debounce (no interrupts needed)
    last_state = {BTN1_PIN: GPIO.HIGH, BTN2_PIN: GPIO.HIGH, BTN3_PIN: GPIO.HIGH}

    while True:
        for pin in (BTN1_PIN, BTN2_PIN, BTN3_PIN):
            current = GPIO.input(pin)
            if current == GPIO.LOW and last_state[pin] == GPIO.HIGH:
                # Falling edge detected — button just pressed
                time.sleep(0.02)  # 20 ms debounce delay
                if GPIO.input(pin) == GPIO.LOW:
                    button_pressed(pin, strip1, strip2)
            last_state[pin] = current
        time.sleep(0.01)  # Poll at ~100 Hz

if __name__ == "__main__":
    main()