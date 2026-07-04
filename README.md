# GoodWe EMS Koordinátor — Home Assistant add-on

Řídí dva GoodWe hybridní měniče (GW20 master / GW10 sekundární) s kaskádovým
měřením. Když GW10 v noci vybíjí a GW20 zároveň nabíjí (přelévání baterie do
baterie přes AC), přepne GW10 do `BATTERY_STANDBY`; po stabilizaci vrátí `AUTO`.
Zapisuje jen EMS mód GW10 (registr 47511) — nic jiného. Dashboard je dostupný
přímo v Home Assistantu přes **Ingress** (postranní panel „GoodWe EMS").

## Instalace

1. **Settings → Add-ons → Add-on Store → ⋮ (vpravo nahoře) → Repositories**
2. Vlož URL: `https://github.com/roskodav/hassio-goodwe-ems` → **Add**
3. V obchodě se objeví **GoodWe EMS Koordinátor** → otevři → **Install**
4. Záložka **Configuration**: zkontroluj `gw10_ip`, `gw20_ip`, `apply` → **Save**
5. **Start**, zapni **Start on boot** a **Show in sidebar**

Add-on posílá do HA i senzory (`sensor.goodwe_*`, `binary_sensor.goodwe_ems_conflict`)
přes Supervisor proxy — žádný token není potřeba nastavovat.

## Volby
| volba | výchozí | popis |
|---|---|---|
| `gw10_ip` | 10.0.1.10 | IP sekundárního měniče GW10K‑ET |
| `gw20_ip` | 10.0.1.76 | IP hlavního měniče GW20K‑ET |
| `apply` | true | true = reálně zapisuje; false = jen simulace |
| `interval` | 5 | perioda čtení [s] |
