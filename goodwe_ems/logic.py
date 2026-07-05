"""Pure, unit-testable helpers for the GoodWe EMS coordinator.

Everything here is side-effect free and deterministic so it can be unit tested
without touching inverters, the network, or the clock. monitor.py imports these.
"""


def as_number(value):
    """Best-effort float parse; returns None for empty/invalid."""
    try:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def battery_dir(reading):
    """Return (mode, magnitude_w). Direction from the reliable mode label, not the
    firmware-dependent sign of pbattery1."""
    mag = as_number((reading or {}).get("pbattery1"))
    mode = str((reading or {}).get("battery_mode_label") or "").lower()
    return (mode, abs(mag) if mag is not None else None)


def grid_import_w(reading):
    """Import power (W) from a meter reading. Meter negative == importing, so
    import = -meter. Returns 0 for export, None if unavailable."""
    meter = as_number((reading or {}).get("meter_active_power_total"))
    if meter is None:
        return None
    return max(0.0, -meter)


def classify_energy_state(sample, threshold_w=300):
    """Classify the joint battery state of the two inverters from one sample.

    Direction comes from the mode label; magnitude must exceed threshold_w.
    """
    g10 = (sample.get("readings") or {}).get("gw10", {})
    g20 = (sample.get("readings") or {}).get("gw20", {})
    b10 = as_number(g10.get("pbattery1"))
    b20 = as_number(g20.get("pbattery1"))
    m10 = str(g10.get("battery_mode_label") or "").lower()
    m20 = str(g20.get("battery_mode_label") or "").lower()
    if b10 is None or b20 is None:
        return "unknown"
    d10 = m10 == "discharge" and abs(b10) > threshold_w
    c10 = m10 == "charge" and abs(b10) > threshold_w
    d20 = m20 == "discharge" and abs(b20) > threshold_w
    c20 = m20 == "charge" and abs(b20) > threshold_w
    if d10 and c20:
        return "gw10_to_gw20"
    if d20 and c10:
        return "gw20_to_gw10"
    if d10 and d20:
        return "both_discharge"
    if c10 and c20:
        return "both_charge"
    return "normal"


def gw20_charging(sample, threshold_w=300):
    """True when GW20's battery is actively charging (the condition that causes the
    secondary to shuttle). Used to avoid restoring GW10 to AUTO too early."""
    g20 = (sample.get("readings") or {}).get("gw20", {})
    b20 = as_number(g20.get("pbattery1"))
    m20 = str(g20.get("battery_mode_label") or "").lower()
    if b20 is None:
        return False
    return m20 == "charge" and abs(b20) > threshold_w


def price_advice(level):
    """Human advice derived from the spot price level (low/medium/high)."""
    lv = str(level or "").lower()
    if lv == "high":
        return {"action": "discharge", "text": "Draho — kryj spotřebu z baterie, neber ze sítě."}
    if lv == "low":
        return {"action": "charge", "text": "Levno — vhodná chvíle nabíjet (i ze sítě)."}
    if lv == "medium":
        return {"action": "hold", "text": "Střední cena — normální self-use."}
    return {"action": "unknown", "text": "Čekám na ceník."}


def interval_energy_kwh(power_w, interval_s):
    """Energy (kWh) transferred at power_w over interval_s seconds."""
    p = as_number(power_w)
    dt = as_number(interval_s)
    if p is None or dt is None:
        return 0.0
    return (p / 1000.0) * (dt / 3600.0)


def interval_cost_czk(meter_power_w, price_czk_per_kwh, interval_s):
    """Grid cost for one sampling interval.

    Convention: meter negative == import (you pay), positive == export (you earn).
    Returns CZK: positive = paid to grid, negative = earned from grid.
    """
    meter = as_number(meter_power_w)
    price = as_number(price_czk_per_kwh)
    if meter is None or price is None:
        return 0.0
    import_w = -meter  # negative meter == importing
    return interval_energy_kwh(import_w, interval_s) * price


def self_sufficiency_pct(pv_w, battery_discharge_w, grid_import_w):
    """Share of load (%) covered by own sources (PV + battery discharge) vs grid.

    load ≈ pv + battery_discharge + grid_import. Returns None when idle.
    """
    pv = max(0.0, as_number(pv_w) or 0.0)
    bat = max(0.0, as_number(battery_discharge_w) or 0.0)
    grid = max(0.0, as_number(grid_import_w) or 0.0)
    total = pv + bat + grid
    if total <= 0:
        return None
    return round((pv + bat) / total * 100.0, 1)


def phase_breakdown(readings):
    """Per-phase meter power (W), summed across all inverters, plus imbalance.

    Returns None if no per-phase meter data is present.
    """
    phases = {"L1": 0.0, "L2": 0.0, "L3": 0.0}
    seen = False
    for inv in (readings or {}).values():
        for idx, key in enumerate(("meter_active_power1", "meter_active_power2", "meter_active_power3"), start=1):
            val = as_number((inv or {}).get(key))
            if val is not None:
                phases["L{}".format(idx)] += val
                seen = True
    if not seen:
        return None
    vals = [phases["L1"], phases["L2"], phases["L3"]]
    dominant = max(phases, key=lambda k: abs(phases[k]))
    return {
        "L1": round(phases["L1"]),
        "L2": round(phases["L2"]),
        "L3": round(phases["L3"]),
        "spread": round(max(vals) - min(vals)),
        "dominant": dominant,
    }


def assist_needed(sample, cfg):
    """True when GW20 is overworked while GW10 is much fuller and the site imports.

    cfg carries the thresholds so this stays pure/testable.
    """
    g10 = (sample.get("readings") or {}).get("gw10", {})
    g20 = (sample.get("readings") or {}).get("gw20", {})
    s10 = as_number(g10.get("battery_soc"))
    s20 = as_number(g20.get("battery_soc"))
    b20 = as_number(g20.get("pbattery1"))
    m20 = str(g20.get("battery_mode_label") or "").lower()
    meter20 = as_number(g20.get("meter_active_power_total"))
    if None in (s10, s20, b20, meter20):
        return None
    importing = -meter20
    gw20_hard = m20 == "discharge" and abs(b20) >= cfg["gw20_discharge_w"]
    return bool(
        s20 <= cfg["gw20_soc_max"]
        and s10 >= cfg["gw10_floor"]
        and (s10 - s20) >= cfg["soc_gap"]
        and gw20_hard
        and importing >= cfg["import_min"]
    )


def action_log_summary(rows, recent=20):
    """Summarize actions.csv rows (list of dicts) into counts + recent list."""
    rows = rows or []
    by_op = {}
    for row in rows:
        op = row.get("operation", "?")
        by_op[op] = by_op.get(op, 0) + 1
    return {"total": len(rows), "by_operation": by_op, "recent": rows[-recent:]}


def downsample(points, buckets):
    """Reduce a list to at most `buckets` items by striding (keeps last point)."""
    n = len(points)
    if buckets <= 0 or n <= buckets:
        return list(points)
    step = n / float(buckets)
    out = [points[int(i * step)] for i in range(buckets)]
    if out and out[-1] is not points[-1]:
        out[-1] = points[-1]
    return out
