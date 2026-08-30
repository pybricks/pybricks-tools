"""Cycle the hub's status light through a rainbow.

A program for checking that downloading and running works, on any hub and
with nothing plugged into it. Every hub has a colour status light, and
``ThisHub`` is whichever hub the program happens to be running on, so this
needs no editing to move between a Move hub and a Prime hub.

It prints as it goes, which also exercises the path from the hub's stdout
back to your terminal.

    pb run examples/rainbow.py
"""

from pybricks.hubs import ThisHub
from pybricks.parameters import Color
from pybricks.tools import wait

CYCLES = 2
SECONDS_PER_RAINBOW = 6
"""How long one pass through the colours takes. Turn it up to watch it."""

STEP = 2
"""Degrees of hue per step, out of 360. Smaller is smoother."""

PAUSE = SECONDS_PER_RAINBOW * 1000 * STEP // 360

hub = ThisHub()

for cycle in range(CYCLES):
    print("rainbow", cycle + 1, "of", CYCLES)
    for hue in range(0, 360, STEP):
        # Saturation and brightness default to full, so one number is a
        # colour: 0 is red, 120 green, 240 blue.
        hub.light.on(Color(hue))
        wait(PAUSE)

hub.light.off()
# Long enough to see that it went out: the hub turns its own light back on
# as soon as the program ends.
wait(1000)
print("done")
