# Trainitest reference sessions

Drop captured Trainitest reference data here (brief Phase 5.1–5.3) — it is then
diff-tested against every PPA export by `tests/test_against_trainitest.py`.

## Capture procedure (local Windows — not runnable in CI)

1. Enable simulated machines:
   ```
   reg add "HKCU\Software\1080Motion\Trainitest\Trainitest" /v EnableSimulatedMachines /t REG_DWORD /d 1 /f
   ```
2. In Trainitest, connect a Simulated Sprint 2 and run + save: a resisted
   sprint, an assisted sprint, a variable-load sprint, a reps (gym) session,
   and a COD (5-0-5) session.
3. Copy the resulting `LocalDb.sqlite` into this folder as
   `reference_localdb.sqlite`.

When `reference_localdb.sqlite` is present the harness imports each session via
`ppa.adapters.trainitest_to_v2` and checks the import is deterministic;
otherwise it falls back to a synthetic Trainitest DB built from
`../LocalDb_schema.sql`.

## Phase 5.4 — BLOB format

`Motion.Data` is assumed to be the same 5-float wire format that
`DataSampleExtractor.cs` decodes from the public API (see
`ppa/codecs/sample_bytes.py`). Confirm with `decomp.exe` against a real
captured row; update the codec if it differs.
