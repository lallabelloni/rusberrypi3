import board
import neopixel

# --- Config ---
PIN      = board.D19
LEDS     = 15          # LEDs per strip
STRIPS   = 2           # number of strips
TOTAL    = LEDS * STRIPS
BRIGHTNESS = 0.3

# All strips on one data line = one long virtual strip
pixels = neopixel.NeoPixel(PIN, TOTAL, brightness=BRIGHTNESS, auto_write=False)

def wheel(pos):
    if pos < 85:
        return (pos * 3, 255 - pos * 3, 0)
    elif pos < 170:
        pos -= 85
        return (255 - pos * 3, 0, pos * 3)
    else:
        pos -= 170
        return (0, pos * 3, 255 - pos * 3)

try:
    while True:
        for j in range(255):
            for i in range(TOTAL):
                pixel_index = (i * 256 // TOTAL) + j
                pixels[i] = wheel(pixel_index & 255)
            pixels.show()

except KeyboardInterrupt:
    pixels.fill((0, 0, 0))
    pixels.show()
    print("Done")