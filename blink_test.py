# sudo apt update
# sudo apt install python3-pip
# sudo pip3 install rpi_ws281x adafruit-circuitpython-neopixel

import time
import board
import neopixel

NUM_LEDS = 30
PIN = board.D18

pixels = neopixel.NeoPixel(PIN, NUM_LEDS, brightness=0.2, auto_write=True)

while True:
    pixels.fill((255, 0, 0))
    time.sleep(1)

    pixels.fill((0, 0, 0))
    time.sleep(1)