# PPA SQLite → 1080 proto schema mapping

How the existing PPA `ppa.db` columns map onto the canonical 1080 Motion proto
schema (`vendor/1080-api/Protos/`). Implemented in
`ppa/adapters/sqlite_to_ppa.py`.

Hierarchy: a PPA **session** → 1080 `Session`; a PPA **rep** → 1080 `Motion`
(structurally a single-machine `MotionGroup` wrapping one `Motion`).

## `sessions` → `Session`

| PPA SQLite column | 1080 proto field | Conversion |
|---|---|---|
| `sessions.id` | `Session.common.Guid` | `str(id)` |
| `sessions.started_at` | `Session.common.Created` | ISO string → `datetime` |
| `sessions.ended_at` | `Session.common.Edited` | ISO string → `datetime` |
| `sessions.athlete_id` | `Session.client_guid` | `str(athlete_id)` |

## `reps` → `Motion` + `Motion.values` (`AggregatedValues`)

| PPA SQLite column | 1080 proto field | Conversion |
|---|---|---|
| `reps.id` | `Motion.common.Guid`, `Motion.motion_group_guid` | `str(id)` |
| `reps.started_at` | `Motion.common.Created` | ISO → `datetime` |
| `reps.ended_at` | `Motion.common.Edited` | ISO → `datetime` |
| `reps.is_eccentric` | `Motion.is_eccentric` | int → bool |
| `reps.comment` | `Motion.comment` | direct |
| `reps.peak_speed_mps` | `AggregatedValues.peak_speed` | direct (m/s) |
| `reps.peak_speed_rpm` *(legacy fallback)* | `AggregatedValues.peak_speed` | `rpm × analytics.MPS_PER_RPM` (~0.00576) |
| `reps.avg_speed_mps` | `AggregatedValues.avg_speed` | direct |
| `reps.peak_force_n` | `AggregatedValues.peak_force` | direct (N) |
| `reps.avg_force_n` | `AggregatedValues.avg_force` | direct |
| `reps.peak_power_w` | `AggregatedValues.peak_power` | direct (W) |
| `reps.avg_power_w` | `AggregatedValues.avg_power` | direct |
| `reps.peak_acceleration_mps2` | `AggregatedValues.peak_acceleration` | direct (m/s²) |
| `reps.avg_acceleration_mps2` | `AggregatedValues.avg_acceleration` | direct |
| `reps.work_j` | `AggregatedValues.work` | direct (J) |
| `reps.max_extension_m` | `AggregatedValues.stop_position`, `…distance` | direct (m) |
| `reps.total_distance_counts` *(fallback for distance)* | `AggregatedValues.distance` | `counts ÷ analytics.COUNTS_PER_METRE` |
| `reps.ended_t_offset_ms − started_t_offset_ms` | `AggregatedValues.duration` | `ms ÷ 1000` (s) |
| `node_no` | `Motion.node_no` | constant `1` — single-cable rig |

## `reps.samples_json` → `Motion.samples` (`DataSamples` / `DataSample`)

`samples_json` is a JSON list of `{t_ms, v_mps, F_N, P_W, pos_m, a_mps2}`.

| samples_json key | 1080 `DataSample` field | Conversion |
|---|---|---|
| `pos_m` | `position` | direct (m) |
| `t_ms` | `time` | `ms ÷ 1000` (s) |
| `v_mps` | `speed` | direct (m/s) |
| `a_mps2` | `acceleration` | direct (m/s²) |
| `F_N` | `force` | direct (N) |
| `P_W` | `power` | direct (W) |

## `sessions.hmi_load_kg` → `Resistance`

| PPA SQLite column | 1080 proto field | Conversion |
|---|---|---|
| `sessions.hmi_load_kg` | `Resistance.con_mass` | direct (kg) — best available |

## `athletes` → `Client` (used in Phase 3 export)

| PPA SQLite column | 1080 proto field | Conversion |
|---|---|---|
| `athletes.id` | `Client.common.Guid` | `str(id)` |
| `athletes.name` | `Client.display_name` | direct |
| `athletes.body_mass_kg` | `Client.weight` | direct (kg) |
| `athletes.external_id` | `Client.external_id` | direct |
| `athletes.squad_group` | `Client.group_name` | direct |
| `athletes.tags` | `Client.tags` | comma-split → list |

---

## Gaps — 1080 fields with no PPA source (RS485 / future-work register hunt)

| 1080 proto field | Notes |
|---|---|
| `AggregatedValues.start_position` | PPA does not record a movement start position — set to `0.0`. |
| `Resistance.ecc_mass` | PPA reps do not snapshot per-rep eccentric load. |
| `Resistance.mode` | No PPA equivalent of the 1080 `0 Normal / 1 NFW / 2 Isotonic` mode. Needs a mapping rule from PPA's resisted/assisted/cod/gym. |
| `Resistance.gear` | PPA stores gear in live config, not per rep. |
| `Resistance.con_speed_limit` / `ecc_speed_limit` | Not snapshotted per rep. |
| `Client.gender`, `Client.length` (height) | PPA athlete record has no gender or height. |
| `ExerciseType`, `Exercise`, `ArchType` | PPA has no exercise-library layer; reps carry a flat `drill` string only. |
| `MotionGroup.side`, `Motion.node_no = 2` | Single-cable rig — no left/right side tracking. |
| `SetViewSetting` | PPA has no per-set view-preference persistence. |

These gaps are candidates for new `ppa_`-prefixed fields or for an RS485
register hunt to capture the missing machine state.
