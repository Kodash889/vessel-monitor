# status_led.py
# Visual-debugging status indicator for the Automation 2040 W Mini.
#
# The board has no RGB LED, so device state is encoded as blink *rhythm* on the
# dedicated connectivity LED (board.conn_led, top-right of the board), with
# brightness as a second cue: calm + dim = healthy, bright + fast = attention.
#
# Drive it from anywhere with:   status.set(status_led.ONLINE)
# A single async task (status.run()) owns the LED and renders the current state.
#
# See VISUAL_DEBUGGING.md for the field reference table.

import uasyncio

# --- State names ------------------------------------------------------------
BOOT     = 'boot'       # powered on, firmware starting
WIFI     = 'wifi'       # joining Wi-Fi
MQTT     = 'mqtt'       # Wi-Fi up, connecting to the MQTT broker
ONLINE   = 'online'     # connected and publishing - all good
LINKDOWN = 'linkdown'   # lost Wi-Fi or MQTT, trying to recover
OTA      = 'ota'        # downloading / applying a firmware update
ERROR    = 'error'      # fatal error, about to reboot

# --- Patterns: each is a list of (brightness 0-100, duration_ms) steps -------
# The task loops the current state's pattern until the state changes.
PATTERNS = {
    BOOT:     [(100, 80), (0, 80)],                          # rapid flicker
    WIFI:     [(100, 120), (0, 120)],                        # fast even blink
    MQTT:     [(100, 120), (0, 120), (100, 120), (0, 640)],  # double-blink + pause
    ONLINE:   [(15, 60), (0, 2940)],                         # calm dim heartbeat ~ every 3s
    LINKDOWN: [(100, 70), (0, 70)],                          # urgent fast flutter, full bright
    OTA:      [(60, 500), (0, 500)],                         # slow deliberate pulse
    ERROR:    [(100, 1000), (0, 200)],                       # long full-bright on
}

_CHUNK_MS = 40  # how often we re-check for a state change / yield to the loop


class StatusLED:
    def __init__(self, board, initial=BOOT):
        self.board = board
        self.state = initial

    def set(self, state):
        """Switch the indicator to a new state (takes effect within ~40 ms)."""
        if state in PATTERNS:
            self.state = state

    def _led(self, level):
        try:
            self.board.conn_led(level)
        except Exception:
            pass

    async def run(self):
        """Async task: render the current state's pattern forever."""
        while True:
            current = self.state
            for level, ms in PATTERNS.get(current, PATTERNS[BOOT]):
                self._led(level)
                slept = 0
                while slept < ms:
                    if self.state != current:        # new state - restart immediately
                        break
                    await uasyncio.sleep_ms(min(_CHUNK_MS, ms - slept))
                    slept += _CHUNK_MS
                if self.state != current:
                    break
