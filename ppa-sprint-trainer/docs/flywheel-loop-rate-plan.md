# Flywheel mode + loop-rate upgrade — design plan

**Status:** DRAFT for review. No code changes yet. Phase 3 (P09.01 baud) is gated
on explicit sign-off. Written 2026-05-19, revised after the CN4/RS485 transport
was confirmed.

## 1. Goal

Two linked asks:

- **Flywheel mode** — resistance proportional to acceleration (virtual inertia),
  `F = M·a`.
- **Loop rate** — the current ~10 Hz control loop is too slow for a flywheel to
  *feel* like a flywheel. Target: **100 Hz control**, 250 Hz telemetry.

The flywheel *maths* is ~30 lines. The *feel* depends entirely on loop rate.
This doc plans the loop-rate work; flywheel mode itself is section 6.

## 2. Current state (from code + rig, 2026-05-19)

- **Transport: CN4 — the drive's RS485 port.** USB-RS485 adapter (DSD TECH
  SH-U14, FTDI chip) → CN4 (pins 3/4/5 = 485A / 485B / SG, 220 Ω terminated at
  the drive). This is a **real RS485 UART link**, and it bypasses the PLC/HMI
  entirely — Modbus straight to the servo.
- **Comms:** `pymodbus` `ModbusSerialClient`, **9600 8N1**, slave id 1
  (`ppa_drive.py:19`). The service auto-detects the FTDI adapter by VID `0x0403`
  (`ppa_service.py:76`), else falls back to COM6.
- **Control loop:** `poll_loop` (`ppa_service.py:648`), target ~10 Hz. Target
  only — `asyncio.sleep` clamps to 0 if a cycle overruns, so the true rate is
  whatever the Modbus traffic allows.
- **Per cycle, `_read_snapshot` issues 9 separate Modbus transactions**
  (`ppa_service.py:387`). During Phase C control, add 2 writes.
- **Heartbeat:** separate thread, S-ON keepalive every 1.5 s
  (`HEARTBEAT_INTERVAL`), shares `modbus_lock`.

## 3. The bottleneck stack

It is a real RS485 UART, so the link is genuinely **baud-bound**. 9600 baud is a
hard wall: a minimal 2-transaction cycle is ~37 wire bytes ≈ 38 ms, plus
inter-frame gaps and turnaround → realistic floor **~15–20 Hz**. 100 Hz at 9600
is arithmetically impossible. Getting to 100 Hz is therefore a *transport*
problem, addressed in priority order:

### 3.1 — FTDI latency timer (free, do first)

FTDI USB-serial chips default to a **16 ms latency timer** — the chip waits up
to 16 ms to fill a USB packet before sending. For request/response Modbus that
caps the transaction rate at **~30 Hz regardless of baud**. Fix: Windows →
Device Manager → the adapter's COM port → Port Settings → Advanced → Latency
Timer → **1 ms**. Free, 5 minutes, and nothing above ~30 Hz happens without it.

### 3.2 — Transaction count

9 read transactions per cycle. Every Modbus transaction carries fixed overhead
(3.5-char inter-frame silence either side, drive turnaround, USB latency).
Fewer transactions is strictly faster at any baud.

- **status, speed, torque, bus voltage, position** — P21.00–P21.08 are
  contiguous; one 9-register block read replaces five transactions (the
  `drive_alive_check.py` script already does this read).
- **temperature (P21.31), fault (P21.41)** — slow-changing; poll at 1–2 Hz.
- **control mode (P00.01)** — static during a session; read at arm.
- **torque limits (P03.08–12)** — these are what we *write*; don't read back
  every cycle.
- **the write** — P03.11 + P03.12 are adjacent; one `write_registers` (FC16)
  sets both. If the drive supports FC23 (read/write multiple), one transaction
  does the whole cycle.

Result: control cycle drops from **9 reads + 2 writes → 1 block read + 1 block
write** (or a single FC23). No parameter writes.

### 3.3 — RS485 baud (P09.01) — the 100 Hz lever, GATED

100 Hz needs ~10 ms/cycle; at 9600 the wire time alone blows that. P09.01 is the
RS485 baud register — and RS485/CN4 is the port in use, so raising it directly
speeds the live link. RS485 + FTDI runs 115200 / 230400 / 460800 comfortably; at
115200 a batched cycle is ~3 ms of wire, leaving ample room inside 10 ms.

P09.01 is on the CLAUDE.md hard-do-not-modify list (rule #3). See section 5.

### 3.4 — Python timing on Windows

`asyncio.sleep` / `time.sleep` granularity on Windows is ~15 ms. For loops above
~50 Hz, pace the control loop with a `time.perf_counter` busy-wait, optionally
`timeBeginPeriod(1)`. Cheap, no hardware risk.

### 3.5 — Windows scheduler jitter

Windows is not real-time; expect 1–5 ms jitter. Fine for a 100 Hz control loop
(10 ms period). A deal-breaker for 250 Hz *control* (4 ms period) — but 250 Hz
*telemetry* tolerates jitter (it may drop a sample). True 250 Hz control would
need an MCU offload or PREEMPT_RT Linux — out of scope unless 100 Hz proves
insufficient.

## 4. Phased plan

Each phase is independently testable and shippable. Stop at any phase if the
feel is good enough — "one milestone at a time" (CLAUDE.md).

| Phase | Work | Touches P09.01? | Risk |
|---|---|---|---|
| 0 | `scripts/loop_rate_probe.py`: fix FTDI latency timer, measure real per-transaction time, confirm drive baud. | No | None (read-only) |
| 1 | Batch reads: P21 block read, defer slow regs, FC16/FC23 write. | No | Low — refactor of `_read_snapshot` |
| 2 | `perf_counter` loop pacing; re-measure achieved rate. | No | Low |
| 3 | **GATED** — P09.01 baud bump to 115200+. | **Yes** | See section 5 |
| 4 | Flywheel mode against the new rate (section 6). | No | Medium — first acceleration-driven write path |
| 5 | Split loops: control thread @ 100 Hz + telemetry thread @ 250 Hz. | No | Medium — threading + lock discipline |
| 6 | *(Optional)* MCU / EtherCAT offload for true 250 Hz control. | n/a | High — new hardware/firmware |

Phase 0 + the FTDI fix alone roughly triples the ceiling for free. Phases 1–2
get close to the 9600 wall. Phase 3 is what actually unlocks 100 Hz.

## 5. The P09.01 decision — needs your sign-off

**What it is:** changing the drive's RS485 baud register to 115200 (or higher).

**Why it's gated:** CLAUDE.md hard safety rule #3 lists P09.01 as never-modify.
A mistimed or mismatched baud change can orphan the comms link — PC and drive
end up at different rates and can't talk until the rate is found again or the
drive is factory-reset.

**Preconditions before the write:**

1. Phase 0 done — FTDI latency fixed, batching in, current rate measured, so the
   baud bump is the *only* remaining lever.
2. Confirm from the SV-X3E manual which baud codes P09.01 accepts, and whether
   the change applies live or on next power cycle.
3. Recovery procedure written and to hand: step the PC side through candidate
   baud rates; factory-reset path. `loop_rate_probe.py`'s baud sweep is exactly
   this recovery tool.
4. Done in a maintenance window (cable detached, operator at the rig). Because
   CN4/RS485 bypasses the HMI, an HMI session need not be inactive — but
   confirm that on the rig first.

**Decision required:** authorise lifting rule #3 for P09.01 specifically, under
the preconditions above. Nothing in Phase 3 happens until that is a written yes.

## 6. Flywheel mode

Once the loop is fast enough:

- New `mode` value `flywheel` (alongside resisted / assisted / cod / gym).
- Config: `virtual_mass_kg` (the inertia dial), `eccentric_overload_pct` (reuse
  the existing field), `viscous_damping` (small, for stability).
- Per control cycle:
  1. Filtered acceleration `a` from speed — low-pass; raw v→a is jittery.
  2. `F = virtual_mass_kg * a / 9.81` → kg-equivalent.
  3. Add a viscous term `+ c·v` to damp the v→a→F positive-feedback ringing.
  4. Slew-limit, clip to `kg_limit`, convert via the locked 5.64 %/kg, write
     P03.11; mirror to P03.12 with the eccentric-overload factor.
- Sign flip at top of rep — reuse the existing rep-phase detection.

**Usable-today fallback (10 Hz):** a *virtual-flywheel* model — a software wheel
state, resistance from the velocity error against it — is far more stable at low
loop rates than raw `F = M·a`, because the virtual wheel's inertia is itself a
low-pass and velocity is read directly (no differentiation noise). If a flywheel
is wanted before the loop-rate work lands, build that variant.

## 7. Risks & safety

- **CN4/RS485 vs HMI:** RS485 is a separate bus from the CN3 USB port and from
  EtherCAT. The old "CN3 destabilises the HMI" rule is a CN3/USB issue and most
  likely does not apply to CN4 — verify on the rig, but this probably lifts the
  maintenance-window-only constraint.
- **Watchdog:** the S-ON heartbeat auto-disables the drive ~4.6 s after the
  heartbeat stops. A faster control loop must keep the heartbeat thread alive —
  verify it still acquires `modbus_lock` promptly under heavier traffic.
- **Flywheel stability:** the v→a→F loop can self-oscillate. The viscous term
  and slew limit are not optional.
- **No new parameter writes** beyond P03.11/P03.12 (already in use) without
  individual sign-off (rule #1). P09.01 is the one exception under discussion.

## 8. Open questions — answer in Phase 0

1. Real per-transaction time on the CN4 link, before and after the FTDI latency
   fix.
2. Achieved control rate after Phase 1 batching.
3. Which baud rates P09.01 accepts; live or power-cycle.
4. Confirm CN4/RS485 genuinely runs clean during a live HMI session.
