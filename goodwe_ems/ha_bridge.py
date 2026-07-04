"""Optional Home Assistant bridge.

Pushes GoodWe / EMS coordinator state into Home Assistant as entities via the
REST API, using a long-lived token (or the Supervisor token when run as an
add-on). Entirely fault tolerant: if HA is unreachable it just retries next
cycle and never affects the control loop.

Enabled when HA_URL and HA_TOKEN are set. States are re-pushed every
HA_PUSH_INTERVAL seconds, so they repopulate within seconds even after an HA
restart (REST-created states are not restored by HA on their own).
"""
import json
import os
import threading
import time
import urllib.request
import urllib.error

PUSH_INTERVAL = float(os.environ.get("HA_PUSH_INTERVAL", "10"))


def _num(v):
    try:
        if v is None:
            return None
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _post_state(base, token, entity, state, attrs):
    url = f"{base}/api/states/{entity}"
    data = json.dumps({"state": state, "attributes": attrs}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=6) as resp:
        return resp.status


def _entities(snap):
    latest = snap.get("latest") or {}
    readings = latest.get("readings") or {}
    status = snap.get("status") or {}
    plan = snap.get("control_plan") or {}
    ctl = plan.get("controller") or {}
    analysis = snap.get("analysis") or {}
    states = analysis.get("states") or {}

    out = []
    for inv_id, label in (("gw10", "GW10K-ET"), ("gw20", "GW20K-ET")):
        r = readings.get(inv_id) or {}
        out += [
            (f"sensor.goodwe_{inv_id}_battery_power", _num(r.get("pbattery1")),
             {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement",
              "friendly_name": f"{label} baterie výkon", "icon": "mdi:battery-charging"}),
            (f"sensor.goodwe_{inv_id}_battery_soc", _num(r.get("battery_soc")),
             {"unit_of_measurement": "%", "device_class": "battery", "state_class": "measurement",
              "friendly_name": f"{label} SOC"}),
            (f"sensor.goodwe_{inv_id}_meter_power", _num(r.get("meter_active_power_total")),
             {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement",
              "friendly_name": f"{label} elektroměr", "icon": "mdi:gauge"}),
            (f"sensor.goodwe_{inv_id}_pv_power", _num(r.get("ppv") if r.get("ppv") is not None else r.get("ppv_total")),
             {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement",
              "friendly_name": f"{label} PV výkon", "icon": "mdi:solar-power"}),
            (f"sensor.goodwe_{inv_id}_battery_mode", str(r.get("battery_mode_label") or "unknown"),
             {"friendly_name": f"{label} režim baterie", "icon": "mdi:battery-sync"}),
        ]

    gw20 = readings.get("gw20") or {}
    out.append(("sensor.goodwe_house_load", _num(gw20.get("load_ptotal")),
                {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement",
                 "friendly_name": "Dům spotřeba", "icon": "mdi:home-lightning-bolt"}))

    prices = snap.get("prices") or {}
    if prices.get("now") is not None:
        out.append(("sensor.goodwe_spot_price", prices.get("now"),
                    {"unit_of_measurement": "CZK/kWh", "state_class": "measurement",
                     "friendly_name": "Spotová cena elektřiny", "icon": "mdi:cash",
                     "level": prices.get("level")}))

    conflict = "on" if states.get("gw10_to_gw20", 0) >= 1 else "off"
    ems = ctl.get("gw10_ems_mode")
    ems_label = "STANDBY" if ems == 8 else "AUTO" if ems == 1 else (str(ems) if ems is not None else "unknown")
    out += [
        ("binary_sensor.goodwe_ems_conflict", conflict,
         {"device_class": "problem", "friendly_name": "GoodWe EMS konflikt (přelévání baterií)"}),
        ("sensor.goodwe_ems_controller_state", str(ctl.get("state") or "monitoring"),
         {"friendly_name": "GoodWe EMS stav koordinátoru", "icon": "mdi:cog-sync"}),
        ("sensor.goodwe_ems_gw10_mode", ems_label,
         {"friendly_name": "GoodWe GW10 EMS mód", "icon": "mdi:transmission-tower"}),
        ("sensor.goodwe_ems_writes", status.get("write_count", 0),
         {"unit_of_measurement": "×", "state_class": "total_increasing",
          "friendly_name": "GoodWe EMS počet zásahů", "icon": "mdi:counter"}),
        ("sensor.goodwe_ems_mode", "apply" if status.get("apply_enabled") else "dry-run",
         {"friendly_name": "GoodWe EMS režim", "icon": "mdi:shield-check"}),
        ("sensor.goodwe_ems_samples", status.get("sample_count", 0),
         {"state_class": "total_increasing", "friendly_name": "GoodWe EMS počet vzorků", "icon": "mdi:database"}),
    ]
    return out


def _push_once(base, token, snap):
    ok = fail = 0
    for entity, state, attrs in _entities(snap):
        if state is None:
            continue
        try:
            _post_state(base, token, entity, state, attrs)
            ok += 1
        except Exception:
            fail += 1
    return ok, fail


_notify_state = {}


def _call_service(base, token, domain, service, data):
    url = f"{base}/api/services/{domain}/{service}"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=6) as resp:
        return resp.status


def _notify(base, token, nid, title, message, active):
    """Create a persistent notification on rising edge, dismiss on falling edge."""
    prev = _notify_state.get(nid, False)
    try:
        if active and not prev:
            _call_service(base, token, "persistent_notification", "create",
                          {"notification_id": nid, "title": title, "message": message})
        elif not active and prev:
            _call_service(base, token, "persistent_notification", "dismiss",
                          {"notification_id": nid})
    except Exception:
        pass
    _notify_state[nid] = active


def _maybe_notify(base, token, snap):
    states = (snap.get("analysis") or {}).get("states") or {}
    conflict = (states.get("gw10_to_gw20", 0) + states.get("gw20_to_gw10", 0)) >= 1
    _notify(base, token, "goodwe_ems_conflict", "GoodWe EMS: přelévání baterií",
            "GW10 a GW20 si přelévají energii mezi bateriemi — koordinátor zasahuje.", conflict)

    readings = (snap.get("latest") or {}).get("readings") or {}
    low = False
    for inv in ("gw10", "gw20"):
        soc = _num((readings.get(inv) or {}).get("battery_soc"))
        if soc is not None and soc < 15:
            low = True
    _notify(base, token, "goodwe_ems_low_soc", "GoodWe EMS: nízké SOC baterie",
            "Některá baterie klesla pod 15 %.", low)


def start(state, inverters=None):
    base = os.environ.get("HA_URL", "").rstrip("/")
    token = os.environ.get("HA_TOKEN", "")
    if not base or not token:
        return False

    def loop():
        while True:
            try:
                snap = state.snapshot()
                _push_once(base, token, snap)
                _maybe_notify(base, token, snap)
            except Exception:
                pass
            time.sleep(PUSH_INTERVAL)

    threading.Thread(target=loop, daemon=True, name="ha-bridge").start()
    return True
