# config_store.py
# Runtime-tunable settings (calibration), persisted to flash and settable over
# MQTT from the web UI. Only whitelisted keys are accepted, and every value is
# clamped to a safe range - so a bad remote value can't break the readings, and
# credentials / broker / device name are never remotely changeable.

import json
import config

OVERRIDES_FILE = 'overrides.json'

# key -> (default_from_config, min, max, type)
_SPEC = {
    'thermocouple_calibration': (float(getattr(config, 'THERMOCOUPLE_CALIBRATION', 0.0)), -20.0, 20.0, float),
    'pulses_per_revolution':    (int(getattr(config, 'PULSES_PER_REVOLUTION', 1)),         1,    1000, int),
}

_values = {}


def _clamp(spec, v):
    default, lo, hi, typ = spec
    try:
        v = typ(v)
    except Exception:
        return None
    if v < lo:
        v = lo
    elif v > hi:
        v = hi
    return v


def load():
    """Load defaults from config.py, then overlay any saved overrides."""
    global _values
    _values = {k: spec[0] for k, spec in _SPEC.items()}
    try:
        with open(OVERRIDES_FILE) as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k in _SPEC:
                cv = _clamp(_SPEC[k], v)
                if cv is not None:
                    _values[k] = cv
    except Exception:
        pass   # no overrides file yet, or unreadable - defaults stand
    return dict(_values)


def get(key):
    return _values.get(key)


def active():
    return dict(_values)


def apply(updates):
    """Validate + apply a dict of updates (e.g. from MQTT). Persists if anything
    changed. Returns the dict of keys that actually changed."""
    changed = {}
    if not isinstance(updates, dict):
        return changed
    for k, v in updates.items():
        if k in _SPEC:
            cv = _clamp(_SPEC[k], v)
            if cv is not None and cv != _values.get(k):
                _values[k] = cv
                changed[k] = cv
    if changed:
        _save()
    return changed


def _save():
    try:
        with open(OVERRIDES_FILE, 'w') as f:
            json.dump(_values, f)
    except Exception as e:
        print("config_store: save failed:", e)
