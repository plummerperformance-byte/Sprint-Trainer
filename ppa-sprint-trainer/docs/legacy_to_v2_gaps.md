# Legacy → v2 storage migration — field gap report

How the legacy PPA `reps` / `sessions` tables map onto the v2
`MotionStorage` / `SessionStorage` models (`ppa/adapters/legacy_to_v2.py`),
and every v2 field with no legacy source.

`derived` = computed from another legacy column; `NONE` = no source at all.
The `NONE` rows are the work-list for the separate RS485 brief.

## MotionStorage ← legacy `reps`

| v2 field | Legacy source | Conversion / action |
|---|---|---|
| `guid` | derived | `uuid5(PPA_LEGACY_NS, "rep:{id}")` |
| `motion_group_guid` | derived | `uuid5(…, "motiongroup:{id}")` |
| `machine_guid` | derived from `sessions.rig_id` | `uuid5(…, "machine:{rig_id}")` |
| `is_eccentric` | `reps.is_eccentric` | int → bool |
| `node_no` | derived | constant `1` (single-cable rig) |
| `con_mass` | `sessions.hmi_load_kg` | direct (kg) — best available |
| `peak_speed` / `top_speed` | `reps.peak_speed_mps` (or `peak_speed_rpm × MPS_PER_RPM`) | m/s |
| `avg_speed` | `reps.avg_speed_mps` | direct |
| `peak_force` / `avg_force` | `reps.peak_force_n` / `avg_force_n` | direct (N) |
| `peak_power` / `avg_power` | `reps.peak_power_w` / `avg_power_w` | direct (W) |
| `peak_acceleration` / `avg_acceleration` | `reps.peak/avg_acceleration_mps2` | direct (m/s²) |
| `work` | `reps.work_j` | direct (J) |
| `distance` / `stop_position` | `reps.max_extension_m` (or `total_distance_counts ÷ COUNTS_PER_METRE`) | m |
| `duration` | `reps.ended_t_offset_ms − started_t_offset_ms` | ms → s |
| `data` | `reps.samples_json` | JSON samples re-encoded to the 1080 5-float wire blob |

### MotionStorage — NONE (no legacy source)

| v2 field | Legacy source | Action |
|---|---|---|
| `trim_type`, `start_index`, `stop_index` | NONE | defaults — sample trimming was never stored; deferred |
| `ecc_mass` | NONE | needs RS485 register (per-rep resistance snapshot) |
| `mode` | NONE | needs RS485 register — defaulted to `NORMAL` |
| `gear` | NONE | needs RS485 register — defaulted to `1` |
| `con_speed`, `ecc_speed` | NONE | needs RS485 register (speed limits used) |
| `is_boost_active`, `boost_load` | NONE | needs RS485 register (eccentric boost) |
| `mass_at_v0`, `mass_at_v1`, `speed_v1` | NONE | needs RS485 register (variable-load profile) |
| `start_position` | NONE | not recorded by the legacy rig loop; deferred to Phase 4 |
| `total_distance`, `total_duration` | NONE | cumulative session totals never stored per rep; deferred |
| `ppa_environment` | NONE | needs UI capture |
| `ppa_wind_mps` | NONE | needs UI capture (or weather API) |
| `ppa_surface_temp_c` | NONE | needs UI capture |
| `ppa_video_url` | NONE | needs UI capture |
| `ppa_rpe` | NONE | needs UI capture (post-rep prompt) |

## SessionStorage ← legacy `sessions`

| v2 field | Legacy source | Conversion / action |
|---|---|---|
| `guid` | derived | `uuid5(…, "session:{id}")` |
| `client_guid` | derived from `sessions.athlete_id` | `uuid5(…, "client:{athlete_id}")` |
| `title` | `sessions.notes` | direct |
| `is_test` | NONE | defaulted `False` — no legacy test flag |
| `group_session_guid` | NONE | defaulted `None` — PPA has no group sessions |

## Sync envelope (all entities)

| v2 field | Legacy source | Conversion / action |
|---|---|---|
| `created_utc` | `*.started_at` | ISO string → datetime |
| `edited_utc` | `*.ended_at` | ISO string → datetime |
| `created_offset_minutes` / `edited_offset_minutes` | NONE | defaulted `0` — legacy stores no UTC offset; needs capture going forward |
| `row_version`, `local_db_row_version` | NONE | new rows start at `0` |
| `is_locally_edited`, `is_locally_deleted` | NONE | new rows start `False` |
