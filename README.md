# GoodWe EMS Koordinátor — Home Assistant add-on

Koordinuje dva GoodWe hybridní střídače na jedné síti se **sdíleným (kaskádovým) měřením**
a spolupracuje s Delta Green / Proteus. Dashboard běží přímo v HA přes Ingress.

Instalace: **Settings → Add-ons → Add-on Store → ⋮ → Repositories** →
`https://github.com/roskodav/hassio-goodwe-ems` → Install → Configuration → Start.

---

## 1. Instalace (co tam je)

| | GW20K-ET | GW10K-ET |
|---|---|---|
| IP / Modbus | 10.0.1.76 / addr 1 | 10.0.1.10 / addr 2 |
| Sériové číslo | 9020KETT244L0071 | 9010KETU222W1303 |
| Baterie | ~24,7 kWh (7 modulů) | ~18 kWh (10 modulů) |
| Role | **hlavní** — řídí Delta Green | **sekundární** — řídí tento add-on |

Oba mají **vlastní chytrý elektroměr, ale oba měří tentýž bod sítě**
(prokázáno na 218 tis. vzorcích: korelace 0,996, medián rozdílu 34 W,
rozdíl neroste s výkonem GW10). Důsledek: údaje „spotřeba domu“ z jednotlivých
střídačů jsou **zkreslené** — skutečnou spotřebu proto počítáme z bilance.

Další zařízení v síti: Delta Link 10.0.1.44, Home Assistant 10.0.1.61,
Tigo 10.0.1.103, Shelly Pro 1 10.0.1.59.

## 2. Problém, který to řeší

Oba střídače jely v režimu AUTO/self-use a každý se snažil vynulovat **stejný**
měřený bod. Výsledek: jeden vybíjel baterii a druhý tu energii rovnou nabíjel
do své — energie kroužila přes AC se ztrátou ~25 % (a zbytečně cyklovala baterie).
Naměřeno 120 konfliktních period, nejdelší 16,4 h, se střídáním směru (ping-pong).

## 3. Jak to funguje

Každých 5 s se čtou oba střídače. **Zapisuje se výhradně do GW10, nikdy do GW20.**
Jediné, co se mění, je `ems_mode` GW10 (registr 47511 — totéž nastavení jako
v aplikaci SolarGo, tedy bezpečné a vratné).

| Situace | Reakce |
|---|---|
| **Přelévání** baterie↔baterie (6/6 vzorků, oba směry) | GW10 → `BATTERY_STANDBY` |
| GW20 nabíjí a hrozí opakování | GW10 se **nevrací** do AUTO (anti-oscilace, max 2 h) |
| Klid 12 vzorků + min. 15 min | GW10 → `AUTO` |
| **GW10 hltá PV přebytek**, GW20 hladoví (o 15+ % níž, pod 60 %) | GW10 pauznut, přebytek nabíjí GW20 |
| GW20 nízko a vybíjí na doraz, GW10 plnější, dům dobírá ze sítě | GW10 vypomůže vybíjením (`DISCHARGE_PV`) |
| Výpadek komunikace / nejasný stav | **nezapisuje se nic** |

Pojistky: ověření po každém zápisu, 5 min cooldown, SOC limity,
okamžitý safety-stop u asistence.

## 4. Delta Green / Proteus

Proteus **je aktivní a řídí GW20** (ověřeno v portálu i v registrech):
spotová optimalizace i obchodování flexibility zapnuté, hodinový plán
(nabít přes poledne, prodat ve večerní špičce).

Pozná se podle **`ems_power_limit` ≠ 0** na GW20 — `ems_mode` nechává na AUTO.
Portálové nastavení sedí na registry: přetoky 28 kW = `grid_export_limit 28000`,
min. nabití 20 % = `battery_discharge_depth 20`.

**Konflikt nehrozí**: Proteus píše jen do GW20, add-on jen do GW10. Naopak si
pomáhají — když GW10 přestane krást PV přebytky, Proteus má čím nabít GW20.

Portál: `moje.deltagreen.cz` → Proteus. Konfigurovaná baterie je jen 24,68 kWh
(= GW20); o baterii GW10 Proteus neví.

## 5. Spotové ceny a arbitráž

Ceny se stahují z `spotovaelektrina.cz` (15 min). Plánovač vybere
4 nejlevnější hodiny (nabíjet) a 4 nejdražší (prodávat), hlídá SOC 30–95 %.

Běží v režimu **`sim`** — nic nezapisuje, jen počítá, kolik by vydělal.
Ostrý režim se zapne až podle naměřených výsledků (`EMS_SPOT=live` v budoucí verzi).

## 6. Co dashboard ukazuje

Skutečná spotřeba domu (z bilance), soběstačnost, spotová cena + 24h graf
s okny plánovače, bilance sítě, „Koordinátor vám ušetřil“, dnešní energetická
bilance, rozložení po fázích, tok energie, historie 24 h, log zásahů.

## 7. Volby add-onu

| volba | výchozí | popis |
|---|---|---|
| `gw10_ip` | 10.0.1.10 | IP sekundárního střídače |
| `gw20_ip` | 10.0.1.76 | IP hlavního střídače |
| `apply` | true | true = reálně zapisuje, false = jen sleduje |
| `interval` | 5 | perioda čtení [s] |
| `assist` | true | výpomoc při vybíjení |

Doladění přes proměnné prostředí: `EMS_CHARGE_BALANCE`, `EMS_SPOT`,
`EMS_STARVE_*`, `EMS_ASSIST_*`, `EMS_CF_RATIO`, `PRICE_BUY_ADDER`.

## 8. Data a integrace do HA

- Do HA se posílají senzory `sensor.goodwe_*` a `binary_sensor.goodwe_ems_conflict`,
  `binary_sensor.goodwe_dg_dispatch` (+ upozornění na konflikt a nízké SOC).
- `/data/samples.csv` — vzorky vč. SOC, PV, ceny a reálné spotřeby (rotace po 150 tis. řádcích)
- `/data/actions.csv` — každý zásah do střídače
- `/data/stats.json` — statistiky přežívající restart
- API: `/api/state`, `/api/history`, `/api/actions`, `/api/log`

Testy: `python3 -m unittest test_logic` (75+ testů čisté logiky).
