# PPA Sprint Trainer

Reverse-engineered control + analytics platform for the **Land Fitness LDT-997-2.3** sprint trainer (1080 Sprint clone) with **HCFA SV-X3E** servo drive.

Owned by Plummer Performance Academy (Adam Plummer). Software is the rig's coaching platform: real-time control over Modbus RTU, athletic-mode state machine with no-load corridor, athlete history + progression, sprint-FV interpretation engine (Lahti / Cross / Morin / Samozino), session templates, PWA install for phone + tablet + iPad.

## Quick start

```bash
pip install -r requirements.txt
python ppa_service.py
```

Then open:

- `http://127.0.0.1:8765/coach` — laptop coach view
- `http://<lan-ip>:8765/athlete` — phone athlete view
- Or run `python ppa_app.py` for a native desktop window + system tray

## Architecture

| File | Role |
|---|---|
| `ppa_service.py` | Unified FastAPI service. Owns COM6, Modbus client, poll loop, athletic-mode state machine, all REST + WS endpoints, embeds the coach + athlete HTML |
| `ppa_app.py` | Desktop launcher — pywebview window + pystray tray icon |
| `persistence.py` | SQLite schema + CRUD helpers (athletes, sessions, reps, samples, templates, rigs) |
| `analytics.py` | Locked calibration constants (kg→torque %, counts→metres, RPM→m/s) |
| `insights.py` | Tier-1 sprint-FV interpretation engine (FV orientation, Pmax classification, Lopt, Tau, mechanical effectiveness, etc.) |
| `synthesis.py` | Coach insights → athlete-facing layered report (verdict + 3 prescriptions + 1 avoid + chase metric, with anti-jargon lint) |
| `prescriptions.py` | Library of dosed training prescriptions (athlete + coach copy) |
| `load_1080_xlsx.py` | Parse a 1080 Sprint xlsx export, compute step events + sprint metrics, POST to `/api/c/dev/load_rep` |
| `generate_icons.py` | PWA icon generator (192 + 512 px maskable) |
| `static/` | PWA manifest + service worker + icons + legacy Phase B index |
| `scripts/` | Calibration sweeps, Phase C bench-test helpers, drive recovery (`force_mode_7.py`), HMI probes |

## Hardware

- USB Mini-B → drive **CN3** → STM32 virtual COM port on **COM6**
- Modbus RTU at 9600 8N1, slave ID 1
- P-register addressing: `(group << 8) | idx` (e.g. P21.04 → `0x1504`)

### Calibrations (locked 2026-05-10)

| Quantity | Value |
|---|---|
| kg → torque % | **5.64 %/kg** (4-point fit, R² ≈ 1.0) |
| Encoder counts → metres | **379,288 c/m** (3-point sweep, variance < 0.03 %) |
| RPM → m/s | **× 0.00576** (derived) |

## Safety

**CN3 ↔ HMI mutual exclusion.** USB plugged into CN3 destabilises the HMI's EtherCAT comms. Operating rule:

- **Live training:** CN3 unplugged. All data via the HMI REST API at `192.168.88.222`.
- **Maintenance / our app:** CN3 plugged, HMI Motor Off.

The service auto-disarms on shutdown. Setpoint hard-clamped to ±10 kg in code (~56 % torque, well below the 300 % factory ceiling). Watchdog (P09.11 = 5 s) auto-disables the drive 4–5 s after the heartbeat stops.

See `CLAUDE.md` for full project context, decision history, and rules of engagement.

## Status (2026-05-10)

- Phase C closed (C.1 → C.6 all passed)
- Athletic-mode state machine working with no-load corridor + slew-rate-limited torque + verify-and-retry disarm
- Per-rep 1080-schema metrics: peak/avg force/velocity/power, work, impulse, TTPF, TTPS, splits, sprint-phase segmentation, F0/V0/Pmax import from xlsx
- Coach UI: live chart, recent reps, Run-detail tabs (Profile / Insights / Steps / Splits / F·V), History drawer with progression, Insights tab with Athlete/Coach toggle and synthesised verdict + chase metric
- All v1 Addendum rig-independent items shipped: pre-rep card, rep validity flag, E-stop, audio feedback, solo countdown, rest timer, tap-to-cycle tiles, session templates, units toggle, multi-rig naming, mode selector, COD sub-mode, gear state, ecc warning, BT HID input layer
- PWA installable on Android / iOS / iPad

## Open work

See `CLAUDE.md` Build Philosophy + the rig-dependent items in the v1 Addendum (walk-to-mark calibration, velocity/distance trigger control logic, auto rep detection, isokinetic auto-cal, drive thermal monitoring, NFW mode).
