#!/usr/bin/env python3
import argparse
import asyncio
import csv
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import goodwe

import logic

try:
    import ha_bridge
except Exception:
    ha_bridge = None

try:
    import price
except Exception:
    price = None


def _load_addon_options():
    """When running as a Home Assistant add-on, seed env from /data/options.json
    and wire HA push through the Supervisor proxy. No-op when run standalone."""
    opt_path = "/data/options.json"
    if os.path.exists(opt_path):
        try:
            with open(opt_path, encoding="utf-8") as fh:
                opts = json.load(fh)
        except Exception:
            opts = {}
        mapping = {"gw10_ip": "GW10_IP", "gw20_ip": "GW20_IP", "apply": "EMS_APPLY",
                   "interval": "EMS_INTERVAL", "assist": "EMS_ASSIST"}
        for key, env in mapping.items():
            if opts.get(key) is not None and not os.environ.get(env):
                os.environ[env] = str(opts[key])
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if supervisor_token:
        os.environ.setdefault("HA_URL", "http://supervisor/core")
        os.environ.setdefault("HA_TOKEN", supervisor_token)


_load_addon_options()


INVERTERS = [
    {"id": "gw10", "label": "GW10K-ET", "ip": os.environ.get("GW10_IP", "10.0.1.10"), "role": "secondary"},
    {"id": "gw20", "label": "GW20K-ET", "ip": os.environ.get("GW20_IP", "10.0.1.76"), "role": "master"},
]

FIELDS = [
    "timestamp",
    "work_mode_label",
    "grid_mode_label",
    "grid_in_out_label",
    "meter_active_power_total",
    "meter_active_power1",
    "meter_active_power2",
    "meter_active_power3",
    "active_power_total",
    "pbattery1",
    "battery_mode_label",
    "battery_soc",
    "load_ptotal",
    "house_consumption",
    "backup_ptotal",
    "ppv",
    "ppv_total",
]

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = DATA_DIR / "samples.csv"
ACTION_LOG_PATH = DATA_DIR / "actions.csv"
VALID_WINDOW = 24
CONFLICT_APPLY_SAMPLES = 6
CLEAR_RESTORE_SAMPLES = 12
MIN_STANDBY_SECONDS = 15 * 60
MAX_STANDBY_SECONDS = 2 * 60 * 60  # release GW10 after this even if GW20 keeps charging
WRITE_COOLDOWN_SECONDS = 5 * 60
GW10_AUTO = 1
GW10_DISCHARGE = 3  # EMSMode.DISCHARGE_PV — force GW10 to discharge (load-share assist)
GW10_BATTERY_STANDBY = 8


def _envf(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


# --- SOC-balance assist: GW10 helps when GW20 is overworked ---
ASSIST_ENABLED = str(os.environ.get("EMS_ASSIST", "true")).strip().lower() in {"1", "true", "yes", "on"}
ASSIST_GW20_SOC_MAX = _envf("EMS_ASSIST_GW20_SOC", 40)          # GW20 SOC at/below this
ASSIST_GW10_SOC_FLOOR = _envf("EMS_ASSIST_GW10_FLOOR", 40)      # GW10 must stay above this
ASSIST_SOC_GAP = _envf("EMS_ASSIST_SOC_GAP", 20)               # GW10 this much fuller than GW20
ASSIST_GW20_DISCHARGE_W = _envf("EMS_ASSIST_GW20_DISCHARGE_W", 8000)  # GW20 discharging >= this
ASSIST_IMPORT_MIN = _envf("EMS_ASSIST_IMPORT_MIN", 400)        # site importing >= this to start
ASSIST_IMPORT_STOP = _envf("EMS_ASSIST_IMPORT_STOP", 150)      # safety-stop below this import
ASSIST_GW20_SOC_RECOVER = _envf("EMS_ASSIST_GW20_RECOVER", 45)  # stop once GW20 recovers to this

SHUTTLE_LOSS_FACTOR = 0.25          # AC round-trip loss of battery-to-battery transfer
BATTERY_WEAR_CZK_PER_KWH = 1.5      # LFP cycle wear cost per shuttled kWh
PREVENTED_ASSUMED_FACTOR = 0.6      # conservative share of trigger power assumed avoided

# Daily counters (reset at midnight); totals live in persistent stats.
EMPTY_DAY = {
    "date": None,
    "grid_cost_today_czk": 0.0,
    "shuttle_kwh_today": 0.0,
    "shuttle_loss_czk_today": 0.0,
    "pv_kwh_today": 0.0,
    "load_kwh_today": 0.0,
    "import_kwh_today": 0.0,
    "export_kwh_today": 0.0,
    "import_cost_today_czk": 0.0,
    "export_earn_today_czk": 0.0,
    "solar_saved_today_czk": 0.0,
    "coordinator_prevented_kwh_today": 0.0,
    "coordinator_saved_today_czk": 0.0,
    "spot_sim_kwh_today": 0.0,
    "spot_sim_earn_today_czk": 0.0,
}

# Historical seed (offline analysis of 2026-07-04..2026-08-03 CSV): the coordinator
# prevented ~20.5 kWh of shuttling in 127 episodes ≈ 43 CZK incl. battery wear
# (conservative: standby windows only). Full counterfactual — chained conflict
# periods (66x measured direction ping-pong, no capacity cap): ~77 kWh transfer,
# ~19 kWh lost ≈ 163 CZK/month incl. wear. Upper accrues at the measured ratio.
COUNTERFACTUAL_RATIO = _envf("EMS_CF_RATIO", 5.7)  # full-counterfactual / conservative (163/43 from data)

# --- Spot arbitrage planner (GW10 only; GW20 stays untouched) ---
# "sim" computes the plan + simulated earnings without writing anything;
# "off" disables. Live mode intentionally not implemented until sim proves out.
SPOT_MODE = str(os.environ.get("EMS_SPOT", "sim")).strip().lower()
SPOT_CHEAP_HOURS = int(_envf("EMS_SPOT_CHEAP_HOURS", 4))
SPOT_EXP_HOURS = int(_envf("EMS_SPOT_EXP_HOURS", 4))
SPOT_SOC_FLOOR = _envf("EMS_SPOT_SOC_FLOOR", 30)
SPOT_SOC_CEILING = _envf("EMS_SPOT_SOC_CEILING", 95)
SPOT_SIM_POWER_W = _envf("EMS_SPOT_SIM_POWER_W", 3000)  # assumed arbitrage power in sim
STATS_SEED = {
    "coordinator_prevented_kwh_total": 20.5,
    "coordinator_saved_total_czk": 43.0,
    "coordinator_saved_upper_total_czk": 246.0,
    "import_cost_total_czk": 0.0,
    "export_earn_total_czk": 0.0,
    "solar_saved_total_czk": 0.0,
    "spot_sim_earn_total_czk": 0.0,
}


def spot_plan_snapshot(prices, gw10_soc):
    """Current spot-arbitrage plan (pure computation, no writes)."""
    if SPOT_MODE == "off" or not prices:
        return {"mode": "off"}
    today = prices.get("today") or []
    cheap, expensive = logic.spot_windows(today, SPOT_CHEAP_HOURS, SPOT_EXP_HOURS)
    hour = prices.get("hour")
    hour = hour if hour is not None else datetime.now().hour
    act = logic.spot_action(hour, cheap, expensive, gw10_soc, SPOT_SOC_FLOOR, SPOT_SOC_CEILING)
    cheap_prices = [h.get("czk") for h in today if h.get("hour") in cheap and h.get("czk") is not None]
    basis = sum(cheap_prices) / len(cheap_prices) if cheap_prices else None
    return {
        "mode": SPOT_MODE,
        "charge_hours": cheap,
        "discharge_hours": expensive,
        "cheap_basis_czk": round(basis, 2) if basis is not None else None,
        "now": act,
        "target": "GW10K-ET (18 kWh)",
    }


class MonitorState:
    def __init__(self, max_samples=720):
        self.lock = threading.Lock()
        self.samples = deque(maxlen=max_samples)
        self.status = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "dry_run": True,
            "last_error": None,
            "sample_count": 0,
            "action_count": 0,
            "last_action": None,
            "connections": {},
            "apply_enabled": False,
            "auto_restore": True,
            "write_count": 0,
            "controller": {
                "state": "monitoring",
                "gw10_ems_mode": None,
                "standby_since": None,
                "last_write_at": None,
                "last_write_result": None,
            },
            "savings": dict(EMPTY_DAY),
            "stats": {"restarts": 0, "actions_total": 0, "first_boot": None, "grid_cost_total_czk": 0.0},
        }

    def snapshot(self):
        with self.lock:
            status = dict(self.status)
            samples = list(self.samples)
            plan = build_control_plan(samples)
            plan["mode"] = "apply" if status.get("apply_enabled") else "dry-run"
            plan["controller"] = dict(status.get("controller", {}))
            prices = price.snapshot() if price else {}
            latest = samples[-1] if samples else None
            extras = compute_extras(latest, prices)
            return {
                "status": status,
                "inverters": [
                    {key: spec[key] for key in ("id", "label", "ip", "role")}
                    for spec in INVERTERS
                ],
                "samples": samples,
                "latest": latest,
                "analysis": analyze_window(samples),
                "control_plan": plan,
                "prices": prices,
                "advice": extras["advice"],
                "self_sufficiency": extras["self_sufficiency"],
                "phases": extras["phases"],
                "real_load": extras["real_load"],
                "spot_plan": spot_plan_snapshot(prices, as_number(((latest or {}).get("readings") or {}).get("gw10", {}).get("battery_soc"))),
                "savings": status.get("savings", {}),
                "stats": status.get("stats", {}),
            }

    def add_sample(self, sample):
        with self.lock:
            self.samples.append(sample)
            self.status["sample_count"] += 1
            decision = sample.get("decision", {})
            if decision.get("severity") in {"warn", "critical"}:
                self.status["action_count"] += 1
                self.status["last_action"] = decision

    def set_error(self, error):
        with self.lock:
            self.status["last_error"] = error

    def set_connection(self, inverter_id, ok, detail=None):
        with self.lock:
            current = self.status["connections"].get(inverter_id, {})
            if ok:
                current["ok"] = True
                current["last_ok"] = datetime.now().isoformat(timespec="seconds")
                current["last_error"] = None
            else:
                current["ok"] = False
                current["last_error"] = detail
                current["last_error_at"] = datetime.now().isoformat(timespec="seconds")
            self.status["connections"][inverter_id] = current

    def set_apply_config(self, apply_enabled, auto_restore):
        with self.lock:
            self.status["dry_run"] = not apply_enabled
            self.status["apply_enabled"] = apply_enabled
            self.status["auto_restore"] = auto_restore

    def recent_samples(self):
        with self.lock:
            return list(self.samples)

    def update_controller(self, **changes):
        with self.lock:
            controller = dict(self.status["controller"])
            controller.update(changes)
            self.status["controller"] = controller

    def controller_snapshot(self):
        with self.lock:
            return dict(self.status["controller"])

    def accumulate(self, sample, interval_s, prices):
        """Full energy accounting for one sample: grid cost, PV savings, shuttle
        and coordinator-prevented shuttle (today + persistent totals)."""
        prices = prices or {}
        price_now = as_number(prices.get("now"))
        price_buy = as_number(prices.get("buy")) or price_now
        price_sell = as_number(prices.get("sell")) or price_now
        readings = sample.get("readings") or {}
        gw20 = readings.get("gw20", {})
        with self.lock:
            today = datetime.now().date().isoformat()
            sv = self.status.get("savings") or {}
            if sv.get("date") != today:
                sv = dict(EMPTY_DAY)
                sv["date"] = today
            st = self.status.get("stats") or {}

            def bump(day_key, delta, total_key=None):
                sv[day_key] = round(sv.get(day_key, 0.0) + delta, 4)
                if total_key:
                    st[total_key] = round(st.get(total_key, 0.0) + delta, 4)

            meter = as_number(gw20.get("meter_active_power_total"))
            if meter is None:
                meter = as_number(readings.get("gw10", {}).get("meter_active_power_total"))
            if meter is not None:
                import_w = max(0.0, -meter)
                export_w = max(0.0, meter)
                imp_kwh = logic.interval_energy_kwh(import_w, interval_s)
                exp_kwh = logic.interval_energy_kwh(export_w, interval_s)
                bump("import_kwh_today", imp_kwh)
                bump("export_kwh_today", exp_kwh)
                if price_buy is not None:
                    bump("import_cost_today_czk", imp_kwh * price_buy, "import_cost_total_czk")
                if price_sell is not None:
                    bump("export_earn_today_czk", exp_kwh * price_sell, "export_earn_total_czk")
                if price_now is not None:
                    net = imp_kwh * (price_buy or 0) - exp_kwh * (price_sell or 0)
                    bump("grid_cost_today_czk", net, "grid_cost_total_czk")

            pv_w = (as_number(readings.get("gw10", {}).get("ppv")) or 0.0) + (as_number(gw20.get("ppv")) or 0.0)
            pv_kwh = logic.interval_energy_kwh(pv_w, interval_s)
            bump("pv_kwh_today", pv_kwh)
            load = logic.real_house_load(readings)
            if load is not None:
                bump("load_kwh_today", logic.interval_energy_kwh(load, interval_s))
            if meter is not None and price_buy is not None:
                # PV self-consumed replaces bought energy; exported PV earns sell price.
                exp_kwh_now = logic.interval_energy_kwh(max(0.0, meter), interval_s)
                self_kwh = max(0.0, pv_kwh - exp_kwh_now)
                bump("solar_saved_today_czk", self_kwh * price_buy + exp_kwh_now * (price_sell or 0.0), "solar_saved_total_czk")

            skwh = logic.interval_energy_kwh(logic.shuttle_power_w(sample), interval_s)
            if skwh:
                bump("shuttle_kwh_today", skwh)
                bump("shuttle_loss_czk_today", skwh * SHUTTLE_LOSS_FACTOR * (price_now or 0.0))

            # coordinator counterfactual: while GW10 is held in STANDBY, the shuttle
            # that triggered the hold is assumed avoided (conservative factor).
            ctl = self.status.get("controller") or {}
            trig = as_number(ctl.get("standby_trigger_w"))
            if ctl.get("state") == "gw10_standby" and trig:
                prev_kwh = logic.interval_energy_kwh(trig * PREVENTED_ASSUMED_FACTOR, interval_s)
                saved = prev_kwh * (SHUTTLE_LOSS_FACTOR * (price_now or 2.5) + BATTERY_WEAR_CZK_PER_KWH)
                bump("coordinator_prevented_kwh_today", prev_kwh, "coordinator_prevented_kwh_total")
                bump("coordinator_saved_today_czk", saved, "coordinator_saved_total_czk")
                st["coordinator_saved_upper_total_czk"] = round(
                    st.get("coordinator_saved_upper_total_czk", 0.0) + saved * COUNTERFACTUAL_RATIO, 4)

            # spot arbitrage simulation: what the planner would have earned now
            if SPOT_MODE == "sim":
                g10 = readings.get("gw10", {})
                soc10 = as_number(g10.get("battery_soc"))
                plan = spot_plan_snapshot(prices, soc10)
                act = (plan.get("now") or {}).get("action")
                if act in ("charge", "discharge"):
                    gain = logic.spot_sim_gain_czk(act, price_now, plan.get("cheap_basis_czk"), SPOT_SIM_POWER_W, interval_s)
                    if gain:
                        bump("spot_sim_earn_today_czk", gain, "spot_sim_earn_total_czk")
                        bump("spot_sim_kwh_today", logic.interval_energy_kwh(SPOT_SIM_POWER_W, interval_s))

            self.status["savings"] = sv
            self.status["stats"] = st

    def record_write(self, result):
        with self.lock:
            self.status["write_count"] += 1
            st = self.status.get("stats") or {}
            st["actions_total"] = st.get("actions_total", 0) + 1
            self.status["stats"] = st
            controller = dict(self.status["controller"])
            controller["last_write_at"] = datetime.now().isoformat(timespec="seconds")
            controller["last_write_result"] = result
            self.status["controller"] = controller


STATE = MonitorState()


def as_number(value):
    try:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def clean_value(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def decide(sample):
    gw10 = sample["readings"].get("gw10", {})
    gw20 = sample["readings"].get("gw20", {})
    gw10_bat = as_number(gw10.get("pbattery1"))
    gw20_bat = as_number(gw20.get("pbattery1"))
    gw10_meter = as_number(gw10.get("meter_active_power_total"))
    gw20_meter = as_number(gw20.get("meter_active_power_total"))
    gw20_load = as_number(gw20.get("load_ptotal"))
    gw10_house = as_number(gw10.get("house_consumption"))

    notes = []
    recommended = "hold"
    severity = "ok"

    if gw10.get("error") or gw20.get("error") or gw10_bat is None or gw20_bat is None:
        return {
            "severity": "unknown",
            "recommended": "wait",
            "title": "Waiting for complete battery data",
            "notes": ["At least one inverter did not return battery power."],
            "would_write": [],
            "exact_dry_run_writes": [],
        }

    # goodwe library reports battery power with firmware-dependent signs in some models.
    # Labels are therefore used as supporting evidence and thresholds stay conservative.
    gw10_mode = str(gw10.get("battery_mode_label") or "").lower()
    gw20_mode = str(gw20.get("battery_mode_label") or "").lower()
    gw10_discharge = gw10_mode == "discharge" and abs(gw10_bat) > 300
    gw10_charge = gw10_mode == "charge" and abs(gw10_bat) > 300
    gw20_discharge = gw20_mode == "discharge" and abs(gw20_bat) > 300
    gw20_charge = gw20_mode == "charge" and abs(gw20_bat) > 300

    if gw10_discharge and gw20_charge:
        severity = "critical"
        recommended = "limit_gw10_discharge"
        notes.append("GW10 is discharging while GW20 is charging. This looks like battery-to-battery transfer through AC.")
    elif gw20_discharge and gw10_charge:
        severity = "critical"
        recommended = "limit_gw10_charge"
        notes.append("GW20 is discharging while GW10 charges — battery-to-battery shuttling. GW10 should defer to GW20 (Delta Green master).")
    elif gw20_discharge and abs(gw10_bat) < 150:
        severity = "ok"
        recommended = "keep_gw20_master"
        notes.append("GW20 is acting as the main system and GW10 is nearly idle.")
    elif abs(gw10_bat) > 1000 and abs(gw20_bat) > 1000:
        severity = "warn"
        recommended = "observe"
        notes.append("Both batteries are active at significant power. This may be valid only if their measured branches are independent.")

    if gw10_house is not None and gw10_house < -200:
        severity = "warn" if severity == "ok" else severity
        notes.append("GW10 reports negative house consumption, which suggests cascaded or reversed/overlapping measurement.")

    if gw10_meter is not None and gw20_meter is not None and abs(gw10_meter) < 80 and abs(gw20_meter) < 80 and gw20_load and gw20_load > 300:
        notes.append("Both meters are near zero while GW20 sees real load. That supports GW20 as the master reference.")

    would_write = []
    if recommended == "limit_gw10_discharge":
        would_write = [
            {
                "target": "GW10K-ET",
                "type": "dry-run",
                "operation": "restrict secondary battery discharge",
                "candidate_methods": [
                    "set GW10 EMS mode to BATTERY_STANDBY",
                    "restore GW10 EMS mode to AUTO after GW20 stops charging",
                ],
            }
        ]
        would_write[0]["exact_dry_run_writes"] = [
            {"ip": "10.0.1.10", "setting": "ems_mode", "current": "AUTO (1)", "would_set": "BATTERY_STANDBY (8)"}
        ]
    elif recommended == "limit_gw10_charge":
        would_write = [
            {
                "target": "GW10K-ET",
                "type": "dry-run",
                "operation": "avoid secondary charging from master-side discharge",
                "candidate_methods": [
                    "disable GW10 charge window",
                    "set GW10 EMS power limit to 0 W while GW20 is discharging",
                ],
            }
        ]

    if not notes:
        notes.append("No conflict detected in this sample.")

    return {
        "severity": severity,
        "recommended": recommended,
        "title": recommended.replace("_", " ").title(),
        "notes": notes,
        "would_write": would_write,
    }


def state_counts(samples, limit):
    recent = valid_samples(samples)[-limit:]
    counts = {}
    for sample in recent:
        state = classify_energy_state(sample)
        counts[state] = counts.get(state, 0) + 1
    return recent, counts


def iso_now():
    return datetime.now().isoformat(timespec="seconds")


def append_action_csv(action):
    exists = ACTION_LOG_PATH.exists()
    with ACTION_LOG_PATH.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if not exists:
            writer.writerow([
                "time",
                "operation",
                "target",
                "from_mode",
                "to_mode",
                "result",
                "reason",
            ])
        writer.writerow([
            action.get("time"),
            action.get("operation"),
            action.get("target"),
            action.get("from_mode"),
            action.get("to_mode"),
            action.get("result"),
            action.get("reason"),
        ])


def seconds_since_iso(value):
    if not value:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(value)).total_seconds()
    except ValueError:
        return None


async def read_ems_mode(client):
    value = await client.read_setting("ems_mode")
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


async def write_ems_mode(client, mode):
    await client.set_ems_mode(goodwe.EMSMode(mode))


def make_control_result(mode, action, result, reason, from_mode=None, to_mode=None):
    return {
        "time": iso_now(),
        "mode": mode,
        "action": action,
        "result": result,
        "reason": reason,
        "from_mode": from_mode,
        "to_mode": to_mode,
    }


async def maybe_apply_control(samples, clients, apply_enabled, auto_restore):
    recent_conflict, conflict_counts = state_counts(samples, CONFLICT_APPLY_SAMPLES)
    recent_clear, clear_counts = state_counts(samples, CLEAR_RESTORE_SAMPLES)
    controller = STATE.controller_snapshot()
    gw10_client = clients.get("gw10")

    if not gw10_client:
        return make_control_result("apply" if apply_enabled else "dry-run", "none", "blocked", "GW10 client is not connected.")

    try:
        current_mode = await read_ems_mode(gw10_client)
        STATE.update_controller(gw10_ems_mode=current_mode)
    except Exception as exc:
        return make_control_result(
            "apply" if apply_enabled else "dry-run",
            "none",
            "blocked",
            f"Could not read GW10 ems_mode: {type(exc).__name__}: {exc}",
        )

    cooldown = seconds_since_iso(controller.get("last_write_at"))
    in_cooldown = cooldown is not None and cooldown < WRITE_COOLDOWN_SECONDS
    shuttle_count = conflict_counts.get("gw10_to_gw20", 0) + conflict_counts.get("gw20_to_gw10", 0)
    stable_conflict = len(recent_conflict) >= CONFLICT_APPLY_SAMPLES and shuttle_count == CONFLICT_APPLY_SAMPLES
    stable_clear = (
        len(recent_clear) >= CLEAR_RESTORE_SAMPLES
        and clear_counts.get("gw10_to_gw20", 0) == 0
        and clear_counts.get("gw20_to_gw10", 0) == 0
    )
    # GW20 charging is the root condition that makes GW10 re-shuttle; only restore
    # GW10 to AUTO once GW20 is no longer charging (prevents STANDBY<->AUTO oscillation).
    gw20_calm = bool(recent_clear) and not any(logic.gw20_charging(s) for s in recent_clear)

    valids = valid_samples(samples)
    latest_valid = valids[-1] if valids else None
    assist_window = valids[-CONFLICT_APPLY_SAMPLES:]
    stable_assist = (
        ASSIST_ENABLED
        and len(assist_window) >= CONFLICT_APPLY_SAMPLES
        and all(classify_assist(s) is True for s in assist_window)
    )
    assist_clear_window = valids[-CLEAR_RESTORE_SAMPLES:]
    assist_cleared = (
        len(assist_clear_window) >= CLEAR_RESTORE_SAMPLES
        and all(classify_assist(s) is False for s in assist_clear_window)
    )

    if current_mode == GW10_BATTERY_STANDBY and not controller.get("standby_since"):
        STATE.update_controller(state="gw10_standby", standby_since=iso_now())

    if stable_conflict:
        reason = f"{CONFLICT_APPLY_SAMPLES}/{CONFLICT_APPLY_SAMPLES} latest valid samples show battery-to-battery shuttling between GW10 and GW20 (either direction). GW10 defers to GW20 (Delta Green master)."
        if current_mode == GW10_BATTERY_STANDBY:
            STATE.update_controller(state="gw10_standby")
            return make_control_result("apply" if apply_enabled else "dry-run", "standby", "already_applied", reason, current_mode, GW10_BATTERY_STANDBY)
        if current_mode != GW10_AUTO:
            return make_control_result("apply" if apply_enabled else "dry-run", "standby", "blocked", f"{reason} Current GW10 ems_mode is {current_mode}, expected AUTO (1).", current_mode, GW10_BATTERY_STANDBY)
        if in_cooldown:
            return make_control_result("apply" if apply_enabled else "dry-run", "standby", "blocked", f"{reason} Write cooldown is still active.", current_mode, GW10_BATTERY_STANDBY)
        if not apply_enabled:
            return make_control_result("dry-run", "standby", "would_apply", reason, current_mode, GW10_BATTERY_STANDBY)

        trigger_powers = [logic.shuttle_power_w(s) for s in recent_conflict]
        trigger_w = sorted(trigger_powers)[len(trigger_powers) // 2] if trigger_powers else None
        await write_ems_mode(gw10_client, GW10_BATTERY_STANDBY)
        verified = await read_ems_mode(gw10_client)
        result = "applied" if verified == GW10_BATTERY_STANDBY else f"verify_failed:{verified}"
        action = {
            "time": iso_now(),
            "operation": "set_gw10_ems_mode",
            "target": "10.0.1.10",
            "from_mode": current_mode,
            "to_mode": GW10_BATTERY_STANDBY,
            "result": result,
            "reason": reason,
        }
        append_action_csv(action)
        STATE.record_write(action)
        STATE.update_controller(state="gw10_standby", gw10_ems_mode=verified, standby_since=iso_now(), standby_trigger_w=trigger_w)
        return make_control_result("apply", "standby", result, reason, current_mode, GW10_BATTERY_STANDBY)

    # Assist safety-stop: if GW10 is force-discharging (assist) and it is no longer
    # justified (import gone / GW10 SOC floor / GW20 recovered), return to AUTO now,
    # bypassing the cooldown so we never keep exporting.
    if current_mode == GW10_DISCHARGE and (not ASSIST_ENABLED or latest_valid is None or assist_should_stop(latest_valid)):
        reason = "Assist safety-stop: GW10 discharge no longer justified (import gone / SOC floor / GW20 recovered) -> AUTO."
        if not apply_enabled:
            return make_control_result("dry-run", "restore_auto", "would_apply", reason, current_mode, GW10_AUTO)
        await write_ems_mode(gw10_client, GW10_AUTO)
        verified = await read_ems_mode(gw10_client)
        result = "applied" if verified == GW10_AUTO else f"verify_failed:{verified}"
        action = {"time": iso_now(), "operation": "assist_stop_gw10_ems_mode", "target": "10.0.1.10",
                  "from_mode": current_mode, "to_mode": GW10_AUTO, "result": result, "reason": reason}
        append_action_csv(action)
        STATE.record_write(action)
        STATE.update_controller(state="monitoring", gw10_ems_mode=verified, standby_since=None)
        return make_control_result("apply", "restore_auto", result, reason, current_mode, GW10_AUTO)

    # Assist: GW20 low + discharging near max while GW10 is much fuller and the site
    # imports -> GW10 shares the load (DISCHARGE_PV). Shuttle (handled above) wins.
    if stable_assist and current_mode != GW10_BATTERY_STANDBY:
        reason = "GW20 low SOC and discharging near max while GW10 is much fuller and the site imports -> GW10 assists (DISCHARGE_PV)."
        if current_mode == GW10_DISCHARGE:
            STATE.update_controller(state="gw10_assist")
            return make_control_result("apply" if apply_enabled else "dry-run", "assist", "already_applied", reason, current_mode, GW10_DISCHARGE)
        if current_mode != GW10_AUTO:
            return make_control_result("apply" if apply_enabled else "dry-run", "assist", "blocked", f"{reason} Current GW10 ems_mode is {current_mode}, expected AUTO (1).", current_mode, GW10_DISCHARGE)
        if in_cooldown:
            return make_control_result("apply" if apply_enabled else "dry-run", "assist", "blocked", f"{reason} Write cooldown is still active.", current_mode, GW10_DISCHARGE)
        if not apply_enabled:
            return make_control_result("dry-run", "assist", "would_apply", reason, current_mode, GW10_DISCHARGE)
        await write_ems_mode(gw10_client, GW10_DISCHARGE)
        verified = await read_ems_mode(gw10_client)
        result = "applied" if verified == GW10_DISCHARGE else f"verify_failed:{verified}"
        action = {"time": iso_now(), "operation": "assist_gw10_ems_mode", "target": "10.0.1.10",
                  "from_mode": current_mode, "to_mode": GW10_DISCHARGE, "result": result, "reason": reason}
        append_action_csv(action)
        STATE.record_write(action)
        STATE.update_controller(state="gw10_assist", gw10_ems_mode=verified, standby_since=None)
        return make_control_result("apply", "assist", result, reason, current_mode, GW10_DISCHARGE)

    if current_mode == GW10_BATTERY_STANDBY and auto_restore:
        standby_age = seconds_since_iso(controller.get("standby_since"))
        reason = f"{CLEAR_RESTORE_SAMPLES} latest valid samples are clear of transfer conflicts."
        if standby_age is None or standby_age < MIN_STANDBY_SECONDS:
            return make_control_result("apply" if apply_enabled else "dry-run", "restore_auto", "blocked", f"{reason} Minimum standby hold time has not elapsed.", current_mode, GW10_AUTO)
        if not stable_clear:
            return make_control_result("apply" if apply_enabled else "dry-run", "restore_auto", "blocked", "Recent samples are not stable enough to restore AUTO.", current_mode, GW10_AUTO)
        force_release = standby_age is not None and standby_age >= MAX_STANDBY_SECONDS
        if not gw20_calm and not force_release:
            return make_control_result("apply" if apply_enabled else "dry-run", "restore_auto", "blocked", "GW20 is still charging — keeping GW10 in STANDBY to prevent immediate re-shuttle (anti-oscillation).", current_mode, GW10_AUTO)
        if in_cooldown:
            return make_control_result("apply" if apply_enabled else "dry-run", "restore_auto", "blocked", "Write cooldown is still active.", current_mode, GW10_AUTO)
        if not apply_enabled:
            return make_control_result("dry-run", "restore_auto", "would_apply", reason, current_mode, GW10_AUTO)

        await write_ems_mode(gw10_client, GW10_AUTO)
        verified = await read_ems_mode(gw10_client)
        result = "applied" if verified == GW10_AUTO else f"verify_failed:{verified}"
        action = {
            "time": iso_now(),
            "operation": "restore_gw10_ems_mode",
            "target": "10.0.1.10",
            "from_mode": current_mode,
            "to_mode": GW10_AUTO,
            "result": result,
            "reason": reason,
        }
        append_action_csv(action)
        STATE.record_write(action)
        STATE.update_controller(state="monitoring", gw10_ems_mode=verified, standby_since=None)
        return make_control_result("apply", "restore_auto", result, reason, current_mode, GW10_AUTO)

    if current_mode == GW10_DISCHARGE and assist_cleared:
        reason = f"{CLEAR_RESTORE_SAMPLES} latest valid samples no longer need GW10 assist -> AUTO."
        if in_cooldown:
            return make_control_result("apply" if apply_enabled else "dry-run", "restore_auto", "blocked", "Write cooldown is still active.", current_mode, GW10_AUTO)
        if not apply_enabled:
            return make_control_result("dry-run", "restore_auto", "would_apply", reason, current_mode, GW10_AUTO)
        await write_ems_mode(gw10_client, GW10_AUTO)
        verified = await read_ems_mode(gw10_client)
        result = "applied" if verified == GW10_AUTO else f"verify_failed:{verified}"
        action = {"time": iso_now(), "operation": "assist_restore_gw10_ems_mode", "target": "10.0.1.10",
                  "from_mode": current_mode, "to_mode": GW10_AUTO, "result": result, "reason": reason}
        append_action_csv(action)
        STATE.record_write(action)
        STATE.update_controller(state="monitoring", gw10_ems_mode=verified, standby_since=None)
        return make_control_result("apply", "restore_auto", result, reason, current_mode, GW10_AUTO)

    if current_mode == GW10_AUTO:
        STATE.update_controller(state="monitoring", standby_since=None)
    return make_control_result("apply" if apply_enabled else "dry-run", "none", "no_action", "No stable corrective condition is active.", current_mode, None)


def valid_samples(samples):
    valid = []
    for sample in samples:
        gw10 = sample.get("readings", {}).get("gw10", {})
        gw20 = sample.get("readings", {}).get("gw20", {})
        if gw10.get("error") or gw20.get("error"):
            continue
        if as_number(gw10.get("pbattery1")) is None or as_number(gw20.get("pbattery1")) is None:
            continue
        valid.append(sample)
    return valid


def classify_energy_state(sample):
    # Single source of truth lives in logic.py (unit tested).
    return logic.classify_energy_state(sample)


def _assist_cfg():
    return {
        "gw20_soc_max": ASSIST_GW20_SOC_MAX,
        "gw10_floor": ASSIST_GW10_SOC_FLOOR,
        "soc_gap": ASSIST_SOC_GAP,
        "gw20_discharge_w": ASSIST_GW20_DISCHARGE_W,
        "import_min": ASSIST_IMPORT_MIN,
    }


def classify_assist(sample):
    """True when GW20 is overworked while GW10 is much fuller and the site imports."""
    return logic.assist_needed(sample, _assist_cfg())


def assist_should_stop(sample):
    """Immediate safety-stop conditions while GW10 is assisting (force-discharging)."""
    g10 = sample.get("readings", {}).get("gw10", {})
    g20 = sample.get("readings", {}).get("gw20", {})
    s10 = as_number(g10.get("battery_soc"))
    s20 = as_number(g20.get("battery_soc"))
    meter20 = as_number(g20.get("meter_active_power_total"))
    if s10 is not None and s10 <= ASSIST_GW10_SOC_FLOOR:
        return True
    if meter20 is not None and (-meter20) < ASSIST_IMPORT_STOP:
        return True
    if s20 is not None and s20 >= ASSIST_GW20_SOC_RECOVER:
        return True
    return False


def analyze_window(samples):
    if not samples:
        return {"summary": "No samples yet.", "conflicts": 0, "recommendation": "wait"}
    conflicts = 0
    warnings = 0
    valid = valid_samples(samples)
    recent = valid[-VALID_WINDOW:]
    states = {}
    gw10_bats = []
    gw20_bats = []
    gw20_loads = []
    for sample in recent:
        state = classify_energy_state(sample)
        states[state] = states.get(state, 0) + 1
        decision = sample.get("decision", {})
        if state in {"gw10_to_gw20", "gw20_to_gw10"}:
            conflicts += 1
        if decision.get("severity") == "warn" or state in {"both_discharge", "both_charge"}:
            warnings += 1
        gw10_bats.append(as_number(sample.get("readings", {}).get("gw10", {}).get("pbattery1")))
        gw20_bats.append(as_number(sample.get("readings", {}).get("gw20", {}).get("pbattery1")))
        gw20_loads.append(as_number(sample.get("readings", {}).get("gw20", {}).get("load_ptotal")))

    def avg(values):
        nums = [v for v in values if v is not None]
        return round(sum(nums) / len(nums), 1) if nums else None

    recommendation = "keep_observing"
    if states.get("gw10_to_gw20", 0) >= 2:
        recommendation = "dry_run_limit_gw10_discharge"
    elif states.get("gw20_to_gw10", 0) >= 2:
        recommendation = "dry_run_limit_gw10_charge"
    elif warnings:
        recommendation = "inspect_secondary_logic"

    return {
        "summary": f"{len(samples)} samples, {len(valid)} valid, last {len(recent)} valid: {conflicts} transfer conflicts, {warnings} coordination warnings.",
        "conflicts": conflicts,
        "warnings": warnings,
        "states": states,
        "recommendation": recommendation,
        "averages": {
            "gw10_battery_w": avg(gw10_bats),
            "gw20_battery_w": avg(gw20_bats),
            "gw20_load_w": avg(gw20_loads),
        },
    }


def build_control_plan(samples):
    valid = valid_samples(samples)
    recent = valid[-VALID_WINDOW:]
    analysis = analyze_window(samples)
    states = analysis.get("states", {})
    plan = {
        "mode": "dry-run",
        "safe_to_apply": False,
        "target": "GW10K-ET",
        "master": "GW20K-ET",
        "recommendation": analysis.get("recommendation", "wait"),
        "reason": [],
        "would_do": [],
        "exact_dry_run_writes": [],
        "blocked_by": [],
    }

    if len(recent) < 6:
        plan["blocked_by"].append("Need at least 6 valid samples after communication recovered.")
        return plan

    if states.get("gw10_to_gw20", 0) + states.get("gw20_to_gw10", 0) >= 2:
        plan["reason"].append("Recent samples show battery-to-battery shuttling between GW10 and GW20 (either direction).")
        plan["would_do"].append("Keep GW20 as master — Delta Green controls it; never write GW20.")
        plan["would_do"].append("Pause GW10 (BATTERY_STANDBY) so it defers to GW20 and stops shuttling.")
        plan["would_do"].append("Restore GW10 to AUTO once the batteries are no longer opposed and readings are stable.")
        plan["exact_dry_run_writes"].append(
            {"ip": "10.0.1.10", "setting": "ems_mode", "current": "AUTO (1)", "would_set": "BATTERY_STANDBY (8)"}
        )
        plan["safe_to_apply"] = False
        return plan

    if states.get("both_discharge", 0) >= max(4, len(recent) // 2):
        plan["reason"].append("Both inverters are discharging while their meters stay near zero. This may be intended cascaded branch balancing.")
        plan["would_do"].append("Do not force a change yet.")
        plan["would_do"].append("Keep alerting if one inverter starts charging from the other.")
        return plan

    plan["reason"].append("No stable corrective action is proven from the latest valid window.")
    plan["would_do"].append("Continue observing.")
    return plan


async def read_inverter(inv):
    data = await inv["client"].read_runtime_data()
    return {field: clean_value(data.get(field)) for field in FIELDS}


async def connect_inverter(spec):
    # family="ET" skips protocol probing (kills the noisy es.py discovery
    # tracebacks in the log and reconnects faster).
    return await goodwe.connect(spec["ip"], family="ET", timeout=6, retries=3)


async def monitor_loop(interval, apply_enabled=False, auto_restore=True):
    clients = {}
    STATE.set_apply_config(apply_enabled, auto_restore)
    while True:
        try:
            sample = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "readings": {},
            }
            for spec in INVERTERS:
                try:
                    if spec["id"] not in clients:
                        clients[spec["id"]] = await connect_inverter(spec)
                    spec["client"] = clients[spec["id"]]
                    sample["readings"][spec["id"]] = await read_inverter(spec)
                    STATE.set_connection(spec["id"], True)
                except Exception as exc:
                    clients.pop(spec["id"], None)
                    detail = f"{type(exc).__name__}: {exc}"
                    STATE.set_connection(spec["id"], False, detail)
                    sample["readings"][spec["id"]] = {"error": detail}

            sample["decision"] = decide(sample)
            sample["control"] = await maybe_apply_control(
                STATE.recent_samples() + [sample],
                clients,
                apply_enabled,
                auto_restore,
            )
            STATE.add_sample(sample)
            append_csv(sample)
            STATE.accumulate(sample, interval, price.snapshot() if price else {})
            _cycle[0] += 1
            if _cycle[0] % 60 == 0:
                save_persisted_stats(STATE.status.get("stats"))
            STATE.set_error(None)
        except Exception as exc:
            STATE.set_error(f"{type(exc).__name__}: {exc}")
        await asyncio.sleep(interval)


CSV_HEADER = [
    "time",
    "gw10_meter_w", "gw10_battery_w", "gw10_battery_mode", "gw10_soc", "gw10_pv_w",
    "gw20_meter_w", "gw20_battery_w", "gw20_battery_mode", "gw20_soc", "gw20_pv_w",
    "real_load_w", "price_czk", "severity", "recommended",
]
CSV_MAX_ROWS = 150_000  # ~8-9 days at 5 s; on overflow rotate to samples-archive.csv
_csv_row_count = [None]


def _rotate_csv_if_needed():
    """Start a fresh file when the header changed (v2 migration) or when the file
    grows past CSV_MAX_ROWS. One archive generation is kept."""
    if not LOG_PATH.exists():
        _csv_row_count[0] = 0
        return
    try:
        with LOG_PATH.open(encoding="utf-8", errors="replace") as fh:
            first = fh.readline().replace("\0", "").strip()
            if _csv_row_count[0] is None:
                _csv_row_count[0] = sum(1 for _ in fh)
        if first != ",".join(CSV_HEADER) or _csv_row_count[0] >= CSV_MAX_ROWS:
            LOG_PATH.replace(DATA_DIR / "samples-archive.csv")
            _csv_row_count[0] = 0
    except OSError:
        pass


def append_csv(sample):
    _rotate_csv_if_needed()
    exists = LOG_PATH.exists()
    readings = sample.get("readings") or {}
    gw10 = readings.get("gw10", {})
    gw20 = readings.get("gw20", {})
    decision = sample.get("decision", {})
    price_now = (price.snapshot() or {}).get("now") if price else None
    with LOG_PATH.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if not exists:
            writer.writerow(CSV_HEADER)
        writer.writerow([
            sample["time"],
            gw10.get("meter_active_power_total"), gw10.get("pbattery1"), gw10.get("battery_mode_label"),
            gw10.get("battery_soc"), gw10.get("ppv"),
            gw20.get("meter_active_power_total"), gw20.get("pbattery1"), gw20.get("battery_mode_label"),
            gw20.get("battery_soc"), gw20.get("ppv"),
            logic.real_house_load(readings), price_now,
            decision.get("severity"), decision.get("recommended"),
        ])
        fh.flush()
    if _csv_row_count[0] is not None:
        _csv_row_count[0] += 1


STATS_PATH = DATA_DIR / "stats.json"
_cycle = [0]


def compute_extras(latest, prices):
    """Derive price advice, self-sufficiency and per-phase breakdown for the API."""
    advice = logic.price_advice((prices or {}).get("level"))
    self_suff = None
    phases = None
    if latest:
        readings = latest.get("readings") or {}
        phases = logic.phase_breakdown(readings)
        pv = 0.0
        disch = 0.0
        for inv in ("gw10", "gw20"):
            r = readings.get(inv) or {}
            pv += as_number(r.get("ppv")) or 0.0
            mode, mag = logic.battery_dir(r)
            if mode == "discharge" and mag:
                disch += mag
        # both meters may read the same grid point; use the master (GW20) for import.
        imp = logic.grid_import_w(readings.get("gw20") or {}) or 0.0
        self_suff = logic.self_sufficiency_pct(pv, disch, imp)
        real_load = logic.real_house_load(readings)
    else:
        real_load = None
    return {"advice": advice, "self_sufficiency": self_suff, "phases": phases, "real_load": real_load}


def load_persisted_stats():
    try:
        with STATS_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_persisted_stats(stats):
    try:
        with STATS_PATH.open("w", encoding="utf-8") as fh:
            json.dump(stats or {}, fh)
    except Exception:
        pass


def _read_csv_rows(path):
    """CSV reader tolerant to NUL bytes / truncated lines from unclean shutdowns."""
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            cleaned = (line.replace("\0", "") for line in fh)
            return [r for r in csv.DictReader(cleaned) if (r.get("time") or "").startswith("20")]
    except Exception:
        return []


def read_action_rows():
    if not ACTION_LOG_PATH.exists():
        return []
    try:
        with ACTION_LOG_PATH.open(encoding="utf-8", errors="replace") as fh:
            cleaned = (line.replace("\0", "") for line in fh)
            return list(csv.DictReader(cleaned))
    except Exception:
        return []


_history_cache = {"at": 0.0, "data": {}}


def read_history(max_rows=17280, buckets=288):
    """Downsampled series from samples.csv for the 24h charts (cached 60 s —
    the FE polls this and the file lives on the Pi's SD card)."""
    now = time.monotonic()
    if _history_cache["data"] and now - _history_cache["at"] < 60:
        return _history_cache["data"]
    rows = _read_csv_rows(LOG_PATH)
    rows = logic.downsample(rows[-max_rows:], buckets)

    def col(name):
        return [as_number(x.get(name)) for x in rows]

    data = {
        "time": [x.get("time") for x in rows],
        "gw10_battery": col("gw10_battery_w"),
        "gw20_battery": col("gw20_battery_w"),
        "gw10_meter": col("gw10_meter_w"),
        "gw20_meter": col("gw20_meter_w"),
        "gw10_soc": col("gw10_soc"),
        "gw20_soc": col("gw20_soc"),
        "real_load": col("real_load_w"),
        "price": col("price_czk"),
    }
    _history_cache["at"] = now
    _history_cache["data"] = data
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        if self.path == "/" or self.path.startswith("/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        if self.path in {"/api/state", "/api/log"}:
            self.send_response(200)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index.html"):
            body = (ROOT / "static" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/state":
            self.send_json(STATE.snapshot())
            return
        if self.path == "/api/history":
            self.send_json(read_history())
            return
        if self.path == "/api/actions":
            self.send_json(logic.action_log_summary(read_action_rows()))
            return
        if self.path == "/api/log":
            if not LOG_PATH.exists():
                self.send_response(404)
                self.end_headers()
                return
            body = LOG_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def run_server(host, port):
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"GoodWe EMS dry-run monitor: http://{host}:{port}")
    print(f"CSV log: {LOG_PATH}")
    server.serve_forever()


def env_bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def main():
    parser = argparse.ArgumentParser(description="GoodWe EMS monitor with conservative optional apply mode.")
    parser.add_argument("--host", default=os.environ.get("EMS_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("EMS_PORT", "8765")))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("EMS_INTERVAL", "5")))
    parser.add_argument(
        "--apply",
        action="store_true",
        default=env_bool("EMS_APPLY"),
        help="Enable writes. Only GW10 ems_mode AUTO (1) <-> BATTERY_STANDBY (8) is implemented.",
    )
    parser.add_argument(
        "--no-auto-restore",
        action="store_true",
        default=env_bool("EMS_NO_AUTO_RESTORE"),
        help="Keep GW10 in BATTERY_STANDBY after applying until manually changed.",
    )
    args = parser.parse_args()

    persisted = load_persisted_stats()
    stats = {
        "restarts": persisted.get("restarts", 0) + 1,
        "first_boot": persisted.get("first_boot") or iso_now(),
        "actions_total": len(read_action_rows()),
        "grid_cost_total_czk": persisted.get("grid_cost_total_czk", 0.0),
    }
    for key, seed in STATS_SEED.items():
        stats[key] = persisted.get(key, seed)
    STATE.status["stats"] = stats
    save_persisted_stats(STATE.status["stats"])

    if ha_bridge is not None and os.environ.get("HA_URL") and os.environ.get("HA_TOKEN"):
        if ha_bridge.start(STATE, INVERTERS):
            print(f"Home Assistant bridge enabled -> {os.environ.get('HA_URL')}")

    if price is not None and str(os.environ.get("PRICE_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}:
        price.start()
        print("Spot price fetcher enabled (spotovaelektrina.cz)")

    thread = threading.Thread(
        target=lambda: asyncio.run(
            monitor_loop(
                args.interval,
                apply_enabled=args.apply,
                auto_restore=not args.no_auto_restore,
            )
        ),
        daemon=True,
    )
    thread.start()
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
