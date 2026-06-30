# app.py  -  Robust vessel monitor for Pimoroni Automation 2040 W Mini
# Launched by main.py (the bootstrap). This file is the one OTA updates.
#
# Reads RPM (inductive sensor on IN1/GP26, IRQ-counted) and temperature
# (K-type via MAX6675, software SPI) and publishes JSON to MQTT over Wi-Fi.
#
# Design goals: survive Wi-Fi/MQTT dropouts indefinitely and self-recover.
#   * Single supervised async loop, never silently dies
#   * Hardware watchdog (reboots the board on any true hang)
#   * Active link liveness (keepalive + periodic ping/check_msg)
#   * Exponential backoff reconnect; full machine.reset() after repeated failure
#   * Store-and-forward outbox so brief outages don't leave gaps
#   * Health telemetry (uptime, RSSI, free mem, reconnects, reset cause)
#
# Same config.py keys as before. Optional new keys are read with getattr()
# and safe defaults, so your existing config.py keeps working unchanged.

import machine
import utime
import time
import network
import json
import uasyncio
import gc
from machine import Pin, WDT
from umqtt.simple import MQTTClient
from automation import Automation2040WMini
import config
try:
    import ota_update
except Exception:
    ota_update = None
import status_led
import config_store

# ----------------------------------------------------------------------------
# Tunables (override any of these in config.py if you like)
# ----------------------------------------------------------------------------
WIFI_CONNECT_TIMEOUT  = 30        # seconds to wait for an IP before giving up
MQTT_KEEPALIVE        = 60        # seconds; broker drops us if silent this long
LINK_CHECK_SECONDS    = 30        # how often to ping the broker / check Wi-Fi
SOCKET_TIMEOUT        = 5         # seconds; stops a dead socket blocking forever
OUTBOX_MAX            = 120       # max buffered readings during an outage
MAX_BACKOFF           = 60        # cap on reconnect backoff (seconds)
FATAL_RECONNECTS      = 15        # consecutive failures -> hard reboot
HEALTHY_AFTER_S       = 60        # run this long, then confirm an OTA update

UPDATE_FREQUENCY_SECONDS   = getattr(config, "UPDATE_FREQUENCY_SECONDS", 5)
HEARTBEAT_INTERVAL_SECONDS = getattr(config, "HEARTBEAT_INTERVAL_SECONDS", 30)
THERMOCOUPLE_CALIBRATION   = getattr(config, "THERMOCOUPLE_CALIBRATION", 0.0)
PULSES_PER_REVOLUTION      = getattr(config, "PULSES_PER_REVOLUTION", 1)

# Thermocouple wiring (unchanged)
THERMOCOUPLE_SCK_PIN = 0
THERMOCOUPLE_CS_PIN  = 1
THERMOCOUPLE_SO_PIN  = 2

# MQTT identity / topics (note: no micropython.const on runtime bytes)
MQTT_CLIENT_ID    = b"pico_w_" + config.MQTT_DEVICE_NAME.encode("utf-8")
MQTT_TOPIC_DATA   = config.MQTT_TOPIC_DATA.format(config.MQTT_DEVICE_NAME).encode("utf-8")
MQTT_TOPIC_STATUS = config.MQTT_TOPIC_STATUS.format(config.MQTT_DEVICE_NAME).encode("utf-8")
MQTT_TOPIC_CONFIG = getattr(config, "MQTT_TOPIC_CONFIG", "vessel/{}/config").format(config.MQTT_DEVICE_NAME).encode("utf-8")


# ----------------------------------------------------------------------------
# MAX6675 thermocouple driver (unchanged behaviour)
# ----------------------------------------------------------------------------
class MAX6675:
    def __init__(self, sck_pin, cs_pin, so_pin):
        self.sck = Pin(sck_pin, Pin.OUT)
        self.cs  = Pin(cs_pin, Pin.OUT, value=1)
        self.so  = Pin(so_pin, Pin.IN)
        self.sck.value(0)

    def read(self):
        self.cs.value(0)
        time.sleep_us(10)
        raw = 0
        for _ in range(16):
            self.sck.value(1)
            raw <<= 1
            if self.so.value():
                raw |= 1
            self.sck.value(0)
        self.cs.value(1)
        return raw

    def read_temp(self, celsius=True):
        raw = self.read()
        if raw & 0b100:               # D2 = open-circuit flag
            return "Error: Open Circuit"
        temp_c = (raw >> 3) * 0.25
        return temp_c if celsius else (temp_c * 9.0 / 5.0) + 32.0


# ----------------------------------------------------------------------------
# Globals / state
# ----------------------------------------------------------------------------
pulse_count = 0   # incremented in IRQ, read+reset in sensor loop


def _pulse_irq(pin):
    global pulse_count
    pulse_count += 1


board = Automation2040WMini()
status = status_led.StatusLED(board)   # visual-debugging indicator (conn LED)
config_store.load()                    # apply any saved calibration overrides
thermocouple = MAX6675(THERMOCOUPLE_SCK_PIN, THERMOCOUPLE_CS_PIN, THERMOCOUPLE_SO_PIN)

rpm_pin = Pin(26, Pin.IN, Pin.PULL_DOWN)
rpm_pin.irq(trigger=Pin.IRQ_RISING, handler=_pulse_irq)

wlan = network.WLAN(network.STA_IF)
wdt = None  # created once we've made it past first-boot setup


class State:
    boot_time   = utime.time()
    client      = None
    mqtt_ok     = False
    reconnects  = 0
    publishes   = 0
    drops       = 0
    last_error  = ""
    reset_cause = machine.reset_cause()


state = State()
outbox = []  # store-and-forward ring buffer of pending JSON payloads (bytes)


def feed_wdt():
    if wdt is not None:
        wdt.feed()


def _reset_cause_name(c):
    names = {
        getattr(machine, "PWRON_RESET", -1):     "power-on",
        getattr(machine, "WDT_RESET", -2):       "watchdog",
        getattr(machine, "HARD_RESET", -3):      "hard",
        getattr(machine, "SOFT_RESET", -4):      "soft",
        getattr(machine, "DEEPSLEEP_RESET", -5): "deepsleep",
    }
    return names.get(c, "unknown(%s)" % c)


# ----------------------------------------------------------------------------
# Wi-Fi
# ----------------------------------------------------------------------------
async def connect_wifi():
    """Bring Wi-Fi up, feeding the WDT throughout. Returns True on success."""
    status.set(status_led.WIFI)
    if not wlan.active():
        wlan.active(True)
    # pm=0xa11140 == PM_NONE: disables CYW43 power-save, the #1 dropout fix
    try:
        wlan.config(pm=0xa11140)
    except Exception:
        pass

    if wlan.isconnected():
        return True

    print("Wi-Fi: connecting to", config.WIFI_SSID)
    try:
        wlan.disconnect()
    except Exception:
        pass
    wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)

    deadline = utime.time() + WIFI_CONNECT_TIMEOUT
    while not wlan.isconnected():
        if utime.time() > deadline:
            print("Wi-Fi: connect timed out")
            return False
        feed_wdt()
        await uasyncio.sleep_ms(500)

    print("Wi-Fi: up, IP", wlan.ifconfig()[0])
    return True


# ----------------------------------------------------------------------------
# MQTT
# ----------------------------------------------------------------------------
_pending_health = False   # set by the config callback so the loop echoes promptly


def _fw_version():
    try:
        with open("version.json") as f:
            return json.load(f).get("version")
    except Exception:
        return None


def _on_config_msg(topic, msg):
    """MQTT callback: apply whitelisted calibration updates from the web UI."""
    global _pending_health
    try:
        updates = json.loads(msg)
    except Exception:
        print("config: bad payload")
        return
    changed = config_store.apply(updates)
    if changed:
        print("config: applied", changed)
        _pending_health = True   # echo the new active config on the next loop tick


def _new_client():
    lwt = json.dumps({"status": "disconnected"}).encode("utf-8")
    c = MQTTClient(
        client_id=MQTT_CLIENT_ID,
        server=config.MQTT_BROKER,
        keepalive=MQTT_KEEPALIVE,
    )
    c.set_last_will(MQTT_TOPIC_STATUS, lwt, retain=True)
    c.set_callback(_on_config_msg)
    return c


def connect_mqtt():
    """Blocking connect (guarded by the WDT). Returns client or None."""
    try:
        c = _new_client()
        c.connect()
        try:
            c.sock.settimeout(SOCKET_TIMEOUT)   # never block forever on a read
        except Exception:
            pass
        c.publish(MQTT_TOPIC_STATUS,
                  json.dumps({"status": "connected"}).encode("utf-8"),
                  retain=True)
        c.subscribe(MQTT_TOPIC_CONFIG, qos=1)   # retained config delivered on subscribe
        print("MQTT: connected")
        board.switch_led(1, True)
        return c
    except Exception as e:
        state.last_error = "mqtt-connect: %s" % e
        print("MQTT: connect failed:", e)
        board.switch_led(1, False)
        return None


# ----------------------------------------------------------------------------
# Sensors
# ----------------------------------------------------------------------------
def read_sensors(interval_s):
    """Return (rpm, temp_or_error). Resets the pulse counter atomically."""
    global pulse_count
    irq_state = machine.disable_irq()
    pulses = pulse_count
    pulse_count = 0
    machine.enable_irq(irq_state)

    ppr = config_store.get('pulses_per_revolution') or 1
    rpm = (pulses / ppr) / (interval_s / 60) if interval_s > 0 else 0.0

    temp = thermocouple.read_temp()
    if isinstance(temp, (int, float)):
        temp += config_store.get('thermocouple_calibration') or 0.0
    return round(rpm, 2), temp


def queue_reading(rpm, temp):
    if isinstance(temp, (int, float)):
        payload = {"device": config.MQTT_DEVICE_NAME,
                   "rpm": rpm,
                   "temperature_c": round(temp, 2)}
    else:
        payload = {"device": config.MQTT_DEVICE_NAME, "rpm": rpm, "error": temp}
    outbox.append(json.dumps(payload).encode("utf-8"))
    # Drop the oldest if we overflow (keep newest data, bounded RAM)
    if len(outbox) > OUTBOX_MAX:
        outbox.pop(0)
        state.drops += 1


def drain_outbox():
    """Publish everything queued. Raises OSError on link failure."""
    while outbox:
        state.client.publish(MQTT_TOPIC_DATA, outbox[0])
        outbox.pop(0)
        state.publishes += 1


def publish_health():
    rssi = None
    try:
        rssi = wlan.status("rssi")
    except Exception:
        pass
    health = {
        "device": config.MQTT_DEVICE_NAME,
        "status": "connected",
        "uptime_s": utime.time() - state.boot_time,
        "free_mem": gc.mem_free(),
        "rssi": rssi,
        "reconnects": state.reconnects,
        "publishes": state.publishes,
        "drops": state.drops,
        "queued": len(outbox),
        "reset_cause": _reset_cause_name(state.reset_cause),
        "last_error": state.last_error,
        "fw_version": _fw_version(),
        "thermocouple_calibration": config_store.get('thermocouple_calibration'),
        "pulses_per_revolution": config_store.get('pulses_per_revolution'),
    }
    state.client.publish(MQTT_TOPIC_STATUS, json.dumps(health).encode("utf-8"))


# ----------------------------------------------------------------------------
# Supervisor: owns the connection, never gives up without rebooting
# ----------------------------------------------------------------------------
async def ensure_connected():
    """Block (feeding WDT) until Wi-Fi + MQTT are up. Reboots after persistent failure."""
    backoff = 1
    while True:
        feed_wdt()
        if not wlan.isconnected():
            state.mqtt_ok = False
            if not await connect_wifi():
                state.reconnects += 1
                _backoff_or_die(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue

        if not state.mqtt_ok or state.client is None:
            status.set(status_led.MQTT)
            c = connect_mqtt()
            if c is None:
                state.reconnects += 1
                _backoff_or_die(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            state.client = c
            state.mqtt_ok = True

        status.set(status_led.ONLINE)
        return  # both up


def _backoff_or_die(seconds):
    status.set(status_led.LINKDOWN)
    print("Reconnect attempt", state.reconnects, "- backing off", seconds, "s")
    if state.reconnects >= FATAL_RECONNECTS:
        print("Too many failures; rebooting.")
        status.set(status_led.ERROR)
        utime.sleep(1)
        machine.reset()
    # chunked sleep so the WDT keeps getting fed
    end = utime.time() + seconds
    while utime.time() < end:
        feed_wdt()
        utime.sleep_ms(200)


def mark_link_down(where, e):
    state.mqtt_ok = False
    state.last_error = "%s: %s" % (where, e)
    print("Link down (%s): %s" % (where, e))
    status.set(status_led.LINKDOWN)
    board.switch_led(1, False)   # secondary indicator: MQTT link
    try:
        state.client.sock.close()
    except Exception:
        pass
    state.client = None


async def run():
    uasyncio.create_task(status.run())   # visual-debugging LED task
    global _pending_health
    await ensure_connected()
    confirmed = False
    healthy_deadline = utime.time() + HEALTHY_AFTER_S

    last_sensor = utime.time()
    last_health = utime.time()
    last_check  = utime.time()

    print("Monitor running. data=%s status=%s" % (
        MQTT_TOPIC_DATA.decode(), MQTT_TOPIC_STATUS.decode()))

    while True:
        feed_wdt()
        now = utime.time()

        # 0) Once we've run cleanly for a while, confirm any pending OTA update
        if not confirmed and now >= healthy_deadline:
            if ota_update:
                try:
                    ota_update.confirm_healthy()
                except Exception as e:
                    print("OTA confirm error:", e)
            confirmed = True

        # 1) Sample sensors on schedule and queue the reading
        if now - last_sensor >= UPDATE_FREQUENCY_SECONDS:
            rpm, temp = read_sensors(now - last_sensor)
            queue_reading(rpm, temp)
            last_sensor = now
            print("rpm=%s temp=%s queued=%d" % (rpm, temp, len(outbox)))

        # 2) Periodic health heartbeat (or an immediate echo after a config change)
        if _pending_health or now - last_health >= HEARTBEAT_INTERVAL_SECONDS:
            try:
                publish_health()
                last_health = now
                _pending_health = False
            except OSError as e:
                mark_link_down("health", e)

        # 2b) Process inbound messages (retained config + UI calibration) promptly
        if state.mqtt_ok and state.client is not None:
            try:
                state.client.sock.setblocking(False)
                state.client.check_msg()
            except OSError as e:
                mark_link_down("recv", e)

        # 3) Active liveness probe: ping (keepalive)
        if state.mqtt_ok and now - last_check >= LINK_CHECK_SECONDS:
            last_check = now
            try:
                state.client.ping()
            except OSError as e:
                mark_link_down("ping", e)

        # 4) Flush queued readings
        if state.mqtt_ok and outbox:
            try:
                drain_outbox()
            except OSError as e:
                mark_link_down("publish", e)

        # 5) If the link dropped anywhere above, rebuild it (then resume)
        if not state.mqtt_ok:
            await ensure_connected()
            state.reconnects = state.reconnects  # keep count; reset backoff handled inside

        await uasyncio.sleep_ms(100)


def main(external_wdt=None):
    global wdt
    wdt = external_wdt if external_wdt is not None else WDT(timeout=8000)
    print("App start. reset_cause =", _reset_cause_name(state.reset_cause))
    # Exceptions propagate to the bootstrap (main.py), which handles rollback.
    uasyncio.run(run())


if __name__ == "__main__":
    main()
