# main.py  -  Immutable bootstrap. DO NOT list this file in OTA_FILES.
#
# Responsibilities (kept deliberately tiny so it never needs updating):
#   1. Start the hardware watchdog immediately, so even a hang here recovers.
#   2. Run the OTA rollback guard (auto-restores a bad update after N bad boots).
#   3. Check GitHub for a newer version and apply it (safe-commit).
#   4. Launch app.py, which contains all the real logic and is what gets updated.

import time
import machine
from machine import WDT

WDT_TIMEOUT_MS = 8000   # RP2040 hardware max is ~8388 ms


def _boot():
    print("Boot. reset_cause =", machine.reset_cause())

    # Watchdog on from the very start. feed() is passed into the OTA download
    # loop so a slow fetch is fed between files; a single fetch that hangs >8s
    # reboots us - harmless, because OTA only swaps files after ALL succeed.
    wdt = WDT(timeout=WDT_TIMEOUT_MS)

    # OTA is optional. If the module or its deps (e.g. urequests) are missing,
    # log it and carry on running the app rather than reboot-looping.
    try:
        import ota_update
    except Exception as e:
        print("OTA unavailable (continuing without it):", e)
        ota_update = None

    if ota_update:
        # 1) Auto-rollback a previously-applied update that won't boot cleanly.
        try:
            state = ota_update.guard()
            print("OTA guard:", state)
            if state == "rolledback":
                time.sleep(1)
                machine.reset()
        except Exception as e:
            print("OTA guard error (continuing):", e)

        # 2) Check for and apply a newer version.
        try:
            if ota_update.check_and_update(feed=wdt.feed):
                print("OTA applied; rebooting into the new version.")
                time.sleep(1)
                machine.reset()
        except Exception as e:
            print("OTA check error (continuing on current code):", e)

    # 3) Run the application. Normally never returns.
    import app
    app.main(wdt)


try:
    _boot()
except Exception as e:
    # app.py raised or failed to import. If this is unconfirmed new code, the
    # guard() boot-counter will roll it back after a few attempts. Reboot to retry.
    print("FATAL in boot/app:", e)
    time.sleep(2)
    machine.reset()
