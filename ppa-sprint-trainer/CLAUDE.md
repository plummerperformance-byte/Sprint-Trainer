# PPA 1080 Rig — Claude Code working notes

## What this project is
Reverse-engineered control plane for an HCFA SV-X3E servo drive in a Land Fitness LDT-997-2.3 sprint trainer (1080 Sprint clone). Working dir: `C:\Users\trigo\`. Rig dir (intentional trailing bracket): `C:\Users\trigo\1080-reverse-engineering)\`.

End goal: PPA Sprint Trainer MVP — local FastAPI service captures live drive telemetry over Modbus, persists athlete sessions, surfaces sprint metrics (splits, phases, decel KPIs, stride mechanics) via a phone-friendly web UI.

## Architecture (current, working)
- **Drive comms**: USB-RS485 adapter (DSD TECH SH-U14, FTDI) → **CN4** RS485 port (pins 3/4/5 = 485A / 485B / SG, 220 Ω termination at the drive). Modbus RTU, 9600 8N1, slave ID 1. CN4/RS485 talks straight to the servo, **bypassing the PLC/HMI**. The service auto-detects the FTDI adapter by VID `0x0403`, else falls back to COM6. *(Earlier notes said CN3 — that was a labelling error: CN3 is the drive's USB port, CN4 is RS485.)*
- **Addressing**: `P[group].[idx]` → register `(group << 8) | idx`. e.g. P21.01 → 0x1501.
- **pymodbus 3.13**: use `device_id=`, NOT `slave=`.
- **Service**: `ppa_service.py` — FastAPI on `127.0.0.1:8765`. Owns the drive serial port, polls ~10 Hz, exposes REST + WS.
- **Drive wrapper**: `ppa_drive.py` — read methods + write methods gated by `armed=False`.
- **UI**: `static/index.html` — vanilla JS dashboard, ARM gate.

## CRITICAL — CN3 vs HMI mutual exclusion (confirmed 2026-05-10)
> **Status update:** this concerns the **CN3 USB** port. The project has since moved to **CN4/RS485** — a separate physical bus that bypasses the PLC/HMI. The exclusivity below most likely does **not** apply to the current CN4 path, which means Modbus during a live HMI session may be safe. Confirm on the rig before relying on it. History below stands for the CN3/USB path.

**Plugging the mini-USB into CN3 destabilises the HMI's EtherCAT comms to the drive.** Symptom: HMI shows intermittent "Device No Response" pop-ups, reps fail to log to the datalog. With CN3 unplugged + drive restarted the HMI works perfectly. Confirmed by user: same rig worked fine the previous evening with CN3 unplugged, today's session was unstable until USB removed.

Operational rule:
- **Live training sessions** (athlete on the cable, HMI master): **CN3 unplugged.** No Modbus during reps. All telemetry comes from the HMI REST API at `192.168.88.222`.
- **Maintenance windows** (parameter snapshot, cap-test, scaling probes): **CN3 plugged in, HMI session inactive.** Brief Modbus reads/writes only. Re-unplug before resuming live use.
- The PPA service architecture needs to reflect this: live mode is HMI-API-only, Modbus is for offline maintenance.

## Confirmed register map
- P21.00 servo status (1=ready, 2=running)
- P21.01 motor speed RPM
- P21.04 commanded torque % (signed, 0.1%/count, negative=cable-retract)
- P21.06 DC bus voltage (0.1 V/count)
- P21.07+P21.08 cumulative encoder position (32-bit signed)
- P00.01 control mode (currently 7 = CANOpen/EtherCAT master)
- P03.08 torque limit source (0=internal, 1=external)
- P03.11/P03.12 forward/reverse external torque limit (0.1%/count, factory cap 3000 = 300%)
- P09.04 comm response delay (used as benign-write proof; current value 0)

## Empirical calibration (locked in 2026-05-10)
- **Torque scaling: 0.1% per count, confirmed.**
- **HMI kg → drive P21.04 torque% = -5.64 %/kg** (4-point static sweep 2/5/10/15 kg, R²≈1.0). Static sweep with cable slack, HMI in active session, "Current Target" + "Variable Force" both ON. Session 3's 2.8 %/kg figure was wrong (different HMI mode/toggle state). 6.1 %/kg theoretical estimate from KB was actually closer to truth.
- **Max usable HMI cable load = 300 / 5.64 ≈ 53 kg** before the 300% factory cap clips. Not 107 kg as previously stated.
- **Position scaling: counts_per_metre = 379,288** (3-point sweep at 10/20/30 m vs HMI distance, variance <0.03%). Equivalent: metres_per_count = 2.636e-6.
- **Implied drum circumference ≈ 345.6 mm** (110 mm dia), assuming 17-bit encoder (131,072 cpr) direct on drum shaft (no gearing). Fits clean integer math.
- **RPM → m/s ≈ RPM × 0.00576** (derived: drum_circ / 60). Not yet verified with live readings but consistent with the position fit.
- P21.04 reports the EtherCAT setpoint, NOT measured force. Real-time force needs a load cell or P21.05 phase-current calibration.
- HMI's `load` column in datalog channel 0 reports back what the HMI thinks the athlete's exerting (not the setpoint). For setpoint→force conversion, use the 5.64 %/kg ratio above.

## Hard safety rules — NEVER violate
1. Never write to a parameter that hasn't been individually justified to me first. Default mode is read-only.
2. Never raise torque limits above 3000 (300% factory ceiling).
3. Never modify P00.00 (motor direction), P00.04 (load inertia), P09.00 (slave ID), P09.01 (RS485 baud), or any P06.x encoder calibration.
4. Never use the S-ON-via-Modbus path (manual section 10.3, register 0x3607) without me physically at the rig and explicit confirmation in that exact session. It bypasses the HMI e-stop interlock.
5. If a Modbus exception comes back, look up the code against the table in `project_hcfa_modbus_protocol.md` before guessing. Codes 20/22/25/26 are non-trivial.
6. Default `armed=False` on every script. Writes require typed confirmation.
7. The rig is in a workshop. Athlete cable safety is my problem; your job is not to make it worse.

## Build philosophy for this project
- **One milestone at a time.** Don't pre-build for features that aren't briefed.
- **Empirical over theoretical.** If a register's behaviour was measured today, trust the measurement, not the manual.
- **Read-only first.** New features land as read-only against the data stream before any write surface is exposed.
- **Phase C unlocked 2026-05-10:** Direct-Modbus drive control proven end-to-end. Mode 7→2 works, S-ON via 0x3607 heartbeat works (motor enables in <1 s), watchdog auto-disable confirmed at ~4.6 s after heartbeat stops. The "HMI is master" architecture is no longer the only option — local Modbus can drive the rig. But Phase C still requires cable detached + operator at rig + cap test of risks. Don't run Phase C scripts during athlete sessions; that's still Phase B (limit-modulator).

## Where things live
- `C:\Users\trigo\` — service, scripts, working files
- `C:\Users\trigo\static\` — web UI
- `C:\Users\trigo\1080-reverse-engineering)\` — manuals, handover docs, CSVs from runs
- `C:\Users\trigo\1080-reverse-engineering)\1080-handover-session3.md` — most recent handover, ground truth
- `C:\Users\trigo\.claude\projects\C--Users-trigo\memory\` — auto-loaded project memories

## Communication style
Concise. Bottom line first. Don't praise the question. Push back if I'm proposing something stupid.
