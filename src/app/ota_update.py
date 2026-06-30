# ota_update.py
# Robust multi-file OTA updater for MicroPython (Raspberry Pi Pico W).
#
#   * Version-gated: compares the repo's version.json to the on-device copy.
#   * Safe-commit: every file is downloaded to <file>.new first; the live files
#     are only swapped in once ALL downloads succeed, so a dropout mid-update
#     never bricks the board - it just retries on the next boot.
#   * Keeps <file>.bak of every replaced file.
#   * Auto-rollback: a new update is "pending" until the app confirms it ran
#     stably (confirm_healthy()). If the new code keeps crashing before it
#     confirms, the previous version is restored automatically after a few boots.
#   * Public repos via raw.githubusercontent.com (no secret on the device) or
#     private repos via the GitHub contents API with a token.
#
# main.py must NOT be listed in OTA_FILES - it is the immutable bootstrap.
# config.py must NOT be listed either - it holds your secrets and stays local.

try:
    import urequests as requests
except ImportError:
    import requests

import os
import json
import time
import gc
import network
import config

# --- configuration (safe getattr defaults so an old config.py still imports) ---
REPO_OWNER     = getattr(config, "OTA_REPO_OWNER", "")
REPO_NAME      = getattr(config, "OTA_REPO_NAME", "")
BRANCH         = getattr(config, "OTA_BRANCH", "main")
REMOTE_DIR     = getattr(config, "OTA_REMOTE_DIR", "src/app")  # path inside the repo
TOKEN          = getattr(config, "OTA_TOKEN", None)            # None => public repo
FILES          = getattr(config, "OTA_FILES", ["app.py"])      # local paths to manage
MAX_BOOT_TRIES = getattr(config, "OTA_MAX_BOOT_TRIES", 3)

VERSION_FILE = "version.json"
PENDING_FLAG = "ota_pending.json"
MANAGED = list(FILES) + [VERSION_FILE]   # version.json is rolled back too


# ---------------------------------------------------------------- helpers ----
def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


def _local_version():
    return float(_read_json(VERSION_FILE, {"version": 0}).get("version", 0))


def _makedirs(path):
    """Ensure parent dirs exist for a path like 'umqtt/simple.py'."""
    parts = path.split("/")[:-1]
    cur = ""
    for p in parts:
        cur = cur + "/" + p if cur else p
        if not _exists(cur):
            try:
                os.mkdir(cur)
            except OSError:
                pass


def _url(remote_path):
    if TOKEN:
        return "https://api.github.com/repos/{}/{}/contents/{}?ref={}".format(
            REPO_OWNER, REPO_NAME, remote_path, BRANCH)
    return "https://raw.githubusercontent.com/{}/{}/{}/{}".format(
        REPO_OWNER, REPO_NAME, BRANCH, remote_path)


def _headers():
    h = {"User-Agent": "pico-ota"}
    if TOKEN:
        h["Authorization"] = "token " + TOKEN
        h["Accept"] = "application/vnd.github.raw"   # raw bytes, not JSON metadata
    return h


def _fetch_text(remote_path, feed=None):
    if feed:
        feed()
    gc.collect()
    # Cache-bust: raw.githubusercontent is CDN-cached (~5 min). A unique query
    # param forces a fresh fetch so a just-pushed version is seen immediately.
    url = _url(remote_path)
    url += ('&' if '?' in url else '?') + 'nocache=' + str(time.ticks_ms())
    r = requests.get(url, headers=_headers())
    try:
        if r.status_code != 200:
            raise OSError("HTTP %d for %s" % (r.status_code, remote_path))
        return r.text
    finally:
        r.close()


def connect_wifi(feed=None, timeout=30):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    try:
        wlan.config(pm=0xa11140)   # disable power-save (dropout mitigation)
    except Exception:
        pass
    if not wlan.isconnected():
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        deadline = time.time() + timeout
        while not wlan.isconnected():
            if time.time() > deadline:
                return False
            if feed:
                feed()
            time.sleep_ms(300)
    return wlan.isconnected()


# ----------------------------------------------------------- public API ----
def update_pending():
    return _exists(PENDING_FLAG)


def guard():
    """Call at boot BEFORE running the app. Auto-rolls-back a bad update.
    Returns 'ok', 'trial', or 'rolledback'."""
    if not _exists(PENDING_FLAG):
        return "ok"
    info = _read_json(PENDING_FLAG, {"tries": 0})
    tries = info.get("tries", 0) + 1
    if tries >= MAX_BOOT_TRIES:
        _rollback()
        _safe_remove(PENDING_FLAG)
        return "rolledback"
    info["tries"] = tries
    _write_json(PENDING_FLAG, info)
    print("OTA: trial boot %d/%d of pending update" % (tries, MAX_BOOT_TRIES))
    return "trial"


def confirm_healthy():
    """Call from the app once it has run stably. Commits the pending update."""
    if not _exists(PENDING_FLAG):
        return
    for f in MANAGED:
        _safe_remove(f + ".bak")
    _safe_remove(PENDING_FLAG)
    print("OTA: update confirmed healthy and committed.")


def check_and_update(feed=None):
    """Connect, compare versions, apply update if newer.
    Returns True if an update was applied (caller should machine.reset())."""
    if update_pending():
        print("OTA: previous update awaiting confirmation; skipping check.")
        return False
    if not REPO_OWNER or not REPO_NAME:
        print("OTA: repo not configured; skipping.")
        return False
    if not connect_wifi(feed):
        print("OTA: no Wi-Fi; skipping update check.")
        return False

    try:
        remote_txt = _fetch_text(REMOTE_DIR + "/" + VERSION_FILE, feed)
        remote_v = float(json.loads(remote_txt).get("version", 0))
    except Exception as e:
        print("OTA: version check failed:", e)
        return False

    local_v = _local_version()
    print("OTA: local v%s, remote v%s" % (local_v, remote_v))
    if remote_v <= local_v:
        print("OTA: up to date.")
        return False

    # 1) download all code files to <file>.new (plus the new version.json)
    print("OTA: downloading v%s ..." % remote_v)
    staged = []
    try:
        for f in FILES:
            data = _fetch_text(REMOTE_DIR + "/" + f, feed)
            _makedirs(f)
            with open(f + ".new", "w") as out:
                out.write(data)
            staged.append(f)
            gc.collect()
            print("OTA: staged", f)
        with open(VERSION_FILE + ".new", "w") as out:
            json.dump({"version": remote_v}, out)
        staged.append(VERSION_FILE)
    except Exception as e:
        print("OTA: download failed (%s); aborting, device untouched." % e)
        for f in staged:
            _safe_remove(f + ".new")
        return False

    # 2) every file downloaded OK -> swap in, keeping a .bak of each
    for f in MANAGED:
        bak = f + ".bak"
        try:
            _safe_remove(bak)
            if _exists(f):
                os.rename(f, bak)
            os.rename(f + ".new", f)
        except OSError as e:
            print("OTA: swap failed for", f, e)

    # 3) mark pending until the app proves the new code boots cleanly
    _write_json(PENDING_FLAG, {"version": remote_v, "tries": 0})
    print("OTA: v%s applied; will confirm after a healthy boot." % remote_v)
    return True


# ------------------------------------------------------------- internals ----
def _rollback():
    print("OTA: rolling back to previous version...")
    restored = 0
    for f in MANAGED:
        bak = f + ".bak"
        if _exists(bak):
            try:
                _safe_remove(f)
                os.rename(bak, f)
                restored += 1
                print("OTA: restored", f)
            except OSError as e:
                print("OTA: rollback failed for", f, e)
    if restored == 0:
        print("OTA: nothing to roll back to (first update?).")


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
