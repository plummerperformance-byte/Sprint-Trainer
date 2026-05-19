# Flywheel mode + loop-rate upgrade — design plan

**Status:** DRAFT for review. No code changes yet. Phase 4 (P09.01) is gated on
explicit sign-off. Written 2026-05-19.

## 1. Goal

Two linked asks:

- **Flywheel mode** — resistance proportional to acceleration (virtual inertia),
  `F = M·a`.
- **Loop rate** — the current 10 Hz control loop is too slow for a flywheel to
  *feel* like a flywheel; raise it.

The flywheel *maths* is ~30 lines. The *feel* depends entirely on loop rate.
This doc plans the loop-rate work; flywheel mode itself is section 6.

## 2. Current state (from code, 2026-05-19)

- **Comms:** HCFA SV-X3E, `pymodbus` `ModbusSerialClient`, **9600 8N1**, slave
  id 1, over CN3 → STM32 virtual COM port on COM6 (`ppa_drive.py:19`).
- **Control loop:** `poll_loop` (`ppa_service.py:648`), target **10 Hz** (3 Hz
  idle). Target only — `asyncio.sleep` clamps to 0 if a cycle overruns, so the
  true rate when connected is whatever the Modbus traffic allows.
- **Per cycle, `_read_snapshot` issues 9 separate Modbus transactions**
  (`ppa_service.py:387`): status, speed, torque, position (2-reg), bus voltage,
  temperature, control mode, fault code, torque-limit block (5-reg). During
  Phase C control add 2 writes (P03.11, P03.12).
- **Heartbeat:** separate thread, S-ON keepalive every 1.5 s
  (`HEARTBEAT_INTERVAL`), shares `modbus_lock`.

## 3. The bottleneck stack — corrected

An earlier feasibility note assumed 115200 baud and one batched 8-register
read. Both are wrong for this rig. Corrected, in priority order:

### 3.1 — Transaction count (the free win, do first)

9 read transactions per cycle. Every Modbus transaction carries fixed overhead
independent of payload: 3.5-char inter-frame silence either side, drive
turnaround, USB/driver latency. Fewer transactions is strictly faster at *any*
baud.

Most of those 9 don't need to run every control cycle:

- **status, speed, torque, bus voltage, position** — P21.00–P21.08 are
  contiguous. One 9-register block read (0x1500–0x1508) replaces five
  transactions; discard the unused intervening registers.
- **temperature (P21.31), fault (P21.41)** — slow-changing. Poll at 1–2 Hz, not
  control rate.
- **control mode (P00.01)** — static during a session. Read at arm, then
  occasionally.
- **torque limits (P03.08–12)** — these are what *we write*. No need to read
  them back every cycle.
- **the write** — P03.11 + P03.12 are adjacent; one `write_registers` (FC16)
  sets both, replacing two FC06 writes.

Result: the control cycle drops from **9 reads + 2 writes → 1 block read + 1
block write**. No parameter writes, no P09.01. This alone is expected to be the
single largest gain.

### 3.2 — Is the CN3 link even baud-limited? (must measure)

**Open question that gates the whole P09.01 decision.** The current link is USB
(mini-USB → CN3 → STM32 VCP). On a USB CDC virtual COM port the "9600" passed to
pyserial is often nominal — USB CDC carries bytes at USB speed; whether a real
9600-baud UART sits behind the STM32 bridge is unknown.

Also: **P09.01 is the RS485 baud.** RS485 is a *different physical port* on the
SV-X3E than CN3/USB. Bumping P09.01 may do nothing for the CN3 path.

So before anyone touches P09.01, Phase 0 must answer: does the CN3 link behave
like a 9600-baud serial line, or like a USB pipe? Measure per-transaction time
directly.

### 3.3 — RS485 baud (P09.01) — only if 3.2 says it matters, and only with sign-off

If — and only if — measurement shows the link is genuinely UART-baud-bound,
raising the drive's baud is the next lever. This means writing **P09.01**, which
is on the CLAUDE.md hard-do-not-modify list (rule #3). See section 5.

### 3.4 — Python timing on Windows

`time.sleep` / `asyncio.sleep` granularity on Windows is ~15 ms. For loops above
~50 Hz, switch the control loop's pacing to a `time.perf_counter` busy-wait,
optionally with `timeBeginPeriod(1)`. Cheap, no hardware risk.

### 3.5 — Windows scheduler jitter

Windows is not real-time; expect 1–5 ms jitter. Fine for a 100 Hz control loop
(10 ms period). A deal-breaker for 250 Hz *control* (4 ms period). True 250 Hz
control would need an MCU offload or PREEMPT_RT Linux — out of scope unless
testing proves 100 Hz insufficient.

## 4. Phased plan

Each phase is independently testable and shippable. Stop at any phase if the
feel is good enough — "one milestone at a time" (CLAUDE.md).

| Phase | Work | Touches P09.01? | Risk |
|---|---|---|---|
| 0 | Instrument: log real per-transaction and per-cycle time. Answer §3.2. | No | None (read-only) |
| 1 | Batch reads: P21 block read, defer slow regs, FC16 block write. | No | Low — pure refactor of `_read_snapshot` |
| 2 | `perf_counter` loop pacing; re-measure achieved rate. | No | Low |
| 3 | Flywheel mode PoC against the new rate (section 6). | No | Medium — first acceleration-driven write path |
| 4 | **GATED** — RS485 baud bump, if §3.2 says it helps. | **Yes** | See section 5 |
| 5 | Split loops: control thread + telemetry thread, two cadences. | No | Medium — threading + lock discipline |
| 6 | *(Optional)* MCU / EtherCAT offload for true 250 Hz control. | n/a | High — new hardware/firmware |

Realistic expectation: Phases 0–2 buy a large rate gain for free. Phase 3 gives
a testable flywheel. Whether Phase 4+ is needed is a *measured* decision, not an
assumed one.

## 5. The P09.01 decision — needs your sign-off

**What it is:** changing the drive's RS485 baud register to a higher rate.

**Why it's gated:** CLAUDE.md hard safety rule #3 lists P09.01 as never-modify.
Reason: a mistimed or mismatched baud change can orphan the comms link — the PC
and drive end up at different rates and can't talk until the rate is found again
or the drive is factory-reset.

**Preconditions before it is even on the table:**

1. Phase 0 measurement shows the CN3 link is genuinely UART-baud-bound (§3.2).
   If it is USB-speed, P09.01 is irrelevant — skip Phase 4 entirely.
2. Confirm from the SV-X3E manual which baud codes P09.01 accepts, and whether
   the change applies live or on next power cycle.
3. Confirm the STM32 VCP bridge will follow the new rate (its drive-side UART
   may be fixed in firmware).

**Recovery plan if it goes wrong:** a documented procedure to step the PC side
through candidate baud rates, plus the factory-reset path — ready *before* the
write, executed in a maintenance window (CN3 connected, no athlete, HMI session
inactive).

**Decision required:** do you authorise lifting rule #3 for P09.01 specifically,
under the preconditions above? Nothing in Phase 4 happens until that is a
written yes.

## 6. Flywheel mode (the easy part)

Once the loop is fast enough, the mode itself:

- New `mode` value `flywheel` (alongside resisted / assisted / cod / gym).
- Config: `virtual_mass_kg` (the inertia dial), `eccentric_overload_pct` (reuse
  the existing field), `viscous_damping` (small, for stability — see below).
- Per control cycle:
  1. Filtered acceleration `a` from speed — low-pass; raw v→a is jittery.
  2. `F = virtual_mass_kg * a / 9.81` → kg-equivalent.
  3. Add a viscous term `+ c·v` to damp the v→a→F positive-feedback ringing
     (small `c`; trades a little pure flywheel feel for stability).
  4. Slew-limit, clip to `kg_limit`, convert via the locked 5.64 %/kg, write
     P03.11; mirror to P03.12 with the eccentric-overload factor.
- Sign flip at top of rep — reuse the existing rep-phase detection.
- The Adjust-panel pipeline-summary line extends naturally:
  `Load = flywheel · virtual mass 20 kg`.

## 7. Risks & safety

- **CN3 vs HMI exclusivity** (CLAUDE.md): all of this is Modbus over CN3 →
  maintenance-window / Phase-C work, never during an HMI-mastered athlete
  session.
- **Watchdog:** the S-ON heartbeat auto-disables the drive ~4.6 s after the
  heartbeat stops. A faster control loop must keep the heartbeat thread alive —
  verify it still acquires `modbus_lock` promptly under heavier traffic.
- **Flywheel stability:** the v→a→F loop can self-oscillate. The viscous term
  and slew limit are not optional.
- **No new parameter writes** beyond P03.11/P03.12 (already in use) without
  individual sign-off (rule #1). P09.01 is the one exception under discussion.

## 8. Open questions — answer in Phase 0

1. Real per-transaction time on the CN3 link — is it baud-bound or USB-bound?
   (§3.2)
2. Does P09.01 affect the CN3/USB path at all, or only the RS485 port?
3. Which baud rates does P09.01 accept; live or power-cycle?
4. Does the STM32 VCP bridge follow a drive-side baud change?
5. Achieved control rate after Phase 1 batching — is it already enough for
   flywheel feel, making Phase 4 unnecessary?
