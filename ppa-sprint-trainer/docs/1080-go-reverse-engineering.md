# 1080 Go — Reverse-Engineering Notes

**Date:** 2026-07-07
**Source:** static decompile of the **1080 Go** Android app (`com.motion1080.app`, v1.0.0 build 30, released 2026-07-01).
**Method:** pulled base + split APKs from a BlueStacks Android-11 instance → Expo `app.config` + `app.manifest` read directly → Hermes bytecode bundle (`index.android.bundle`, HBC v96) decompiled with `hermes-dec` (`hbc-decompiler`) → traced protobuf schemas, BLE UUID map, screen routes, and the control workflow.

> Purpose: understand how the market leader structures machine control/telemetry to inform the PPA Sprint Trainer. Note: 1080 Go talks **protobuf-over-BLE** to genuine 1080 machines; our rig is **Modbus RTU / RS485**. The protocol isn't drop-in, but the *control model and data schema* are directly instructive.

---

## 1. Tech stack (from `app.config`)

- **Expo SDK 55 / React Native**, Hermes, expo-router (file-based routing, typed routes), Reanimated, Sentry.
- **BLE:** `react-native-ble-plx` in **central** mode, background enabled, `neverForLocation:true`.
- Permissions: `BLUETOOTH`, `BLUETOOTH_ADMIN`, `BLUETOOTH_SCAN`, `BLUETOOTH_CONNECT`. Blocks `RECORD_AUDIO`.
- **`minSdkVersion: 29`** (Android 10) — installs fail on Android 9.
- **`usesFeature android.hardware.telephony required:true`** + `supportsTablet:false` → Play marks it "not available on PC/tablet". This is why it won't install on emulators/tablets.
- ARM-only RN native libs → will not run past splash on x86 emulators (needs a real ARM device).
- OTA updates: `u.expo.dev/444c24be-7f6f-4a74-a300-10cd21418917`. Sentry org `1080motion-ab`, project `companion-mobile-app`.
- Internal codename: owner `temotion`, URL scheme `tem-go`, iOS bundle `com.1080Motion.app`.
- Machine images bundled: **sprint2, cable, squat** (the three 1080 products the app drives).

---

## 2. BLE protocol map

All UUIDs use the branded pattern `1080XXXX-YYYY-4000-800Z-000000001080`, where `Z` selects the service.

### Service `…-8000` — NUS (Nordic UART Service) — raw transport
- RX char `10800002-0000-4000-8000-000000001080`
- TX char `10800003-0000-4000-8000-000000001080`

### Service `…-8001` — `CONTROL_APP_SERVER` (app → machine commands)
| Characteristic | Key | Protobuf msg |
|---|---|---|
| `10800001-…-8001` | load | `Load` |
| `10800002-…-8001` | speedLimit | `SpeedLimit` |
| `10800003-…-8001` | systemMode | `SystemMode` |
| `10800004-…-8001` | machineMode | `SetMachineMode` |
| `10800005-…-8001` | variableLoad | `VariableLoad` |
| `10800006-…-8001` | estop | `EStop` |
| `10800007-…-8001` | errors | `Errors` |
| `10800008-…-8001` | batteryInfo | `BatteryInfo` |
| `10800009-…-8001` | limits | `MachineLimits` |
| `10800010-…-8001` | controllerMode | `ControllerMode` |
| `10800011-…-8001` | gear | `Gear` |

### Service `…-8002` — `MACHINE_APP` (machine → app telemetry/state)
| Characteristic | Key | Protobuf msg |
|---|---|---|
| `10800001-…-8002` | machineAppState | `MachineState` |
| `10800002-…-8002` | exercise | `Exercise` |
| `10800003-…-8002` | client | `Client` |
| `10800004-…-8002` | session | `Session` |
| `10800005-…-8002` | bestResult | `Result` |
| `10800005-0001-…-8002` | currentResult | `Result` |
| `10800005-0002-…-8002` | recentResult | `Result` |
| `10800006-…-8002` | machineInfo | `MachineInfo` |
| `10800007-…-8002` | machineEvent | `Event` |
| `10800008-…-8002` | exerciseSettings | `ExerciseSettingsChar` |
| `10800009-…-8002` | displayMetrics | `DisplayMetrics` |
| `1080000a-…-8002` | autoApplyLoadDialog | `AutoApplyLoadDialog` |
| `1080000c-…-8002` | unitSettings | `UnitSettingsChar` |

Clean split: **`-8001` characteristics are setters, `-8002` are subscriptions.**

---

## 3. Protobuf message schemas (field lists from `createBase*`)

### Commands (app → machine)
```
TrySetAllMachineLoadValuesRequest {   // the master control command
  mode, conLoad, eccLoad,
  conSpeedLimit, eccSpeedLimit,
  variableLoadEnabled, variableLoadStartLoad, variableLoadTargetLoad, variableLoadTargetSpeed,
  reason, requestOptions
}
TrySetValuesRequestOptions { bypassSafetyChecks, refuseIfOutsideLimits }   // safety envelope
SetVariableLoadRequest { startLoad, targetLoad, targetSpeed, reason }
SetGearRequest        { gear, reason }
SetModeRequest        { mode, reason }
SetSystemModeRequest  { systemMode, reason }
WatchLineStatusResponse { isAtZeroPosition, lineMovementSpeed }   // zeroing / walk-to-mark primitive
```

### Telemetry / state (machine → app)
```
MachineState { application, userId, orgId, isDevMode, hasNetwork }
Exercise     { guid, name, archType, icon }
Client       { uuid, blurHash, name }
Session      { guid }
MachineInfo  { type, name, version, serialNumber }
Event        { resultDeleted }
UnitSettingsChar { speedUnit, distanceUnit }

Result { linearResult | codResult | repResult, aborted }     // oneof by drill type
  LinearResultMessage { isAssisted, values[LinearMetricAndValue{metric,value}] }
  CodResultMessage    { values[CodMetricAndValue{metric,value}], phaseCount }
    CodPhaseStats     { topSpeed, time, distance, zeroToFiveTime, isAssisted }
  RepResultMessage    { reps[RepSessionStats], repCount, repCountRight, repCountLeft, work }
    RepSessionStats   { phase, repId, values[], side }

DisplayMetrics {   // drives the configurable metric tiles per mode
  primaryRepMetric, primaryLinearMetric, primaryCodMetric,
  secondaryRepMetric, secondaryLinearMetric, secondaryCodMetric, displayPhase
}
AutoApplyLoadDialog { isOpen, autoApplyLoadId, mode, bodyWeightPercentage,
                      velocityDecrementPercentage, reason, load }

ExerciseSettingsChar { linearConfig, codConfig, repsConfig }
  LinearRunConfig { startDirection, finishCondition{type,value}, side }
  CodConfig       { minimumNumberOfTurns, flyingStartDistance, endPosition, startDirection }
  RepsConfig      { side }
```

### Key enums
- **`Application`** (workout mode, drives which dashboard renders): `APPLICATION_REPS`, `APPLICATION_ISOKINETIC`, `APPLICATION_OPEN_TRAINING`, `APPLICATION_RESISTED_SPRINT` (+ `UNSPECIFIED`/`UNRECOGNIZED`).
- **Direction:** `DIRECTION_SETTING_RESISTED` / `DIRECTION_SETTING_ASSISTED` (assisted = overspeed).
- **AutoApply modes:** `OFF`, `BODY_WEIGHT`, `VELOCITY_DECREMENT`, `PREVIOUS_TRACE`; safety reason `SPEED_EXCEEDS_LIMIT`.
- **Controller:** `CONTROLLER_MODE_SINGLE` / `MULTIPLEXED`.
- COD metrics: 3-phase model (`PHASE1/2/3` deceleration distance/time, max accel/decel, top speed, total distance/time).

---

## 4. App architecture & control workflow

**Machine-context-driven, one mode-branching control screen.**

Core hooks in `MachineControlScreen`:
- `useMachineContext(routeId)` → `{ connectionState, controlState, device.id, state.machineState.application, state.exercise.guid }`
- `useControlUi()` → `{ setActiveMachineId, setIsEstopActive, pushResult, clearResults, dropInProgressResult }` (results buffer w/ in-progress handling)
- `useBluetooth()`, `useMachineLinkStatus()` → `{ status, showReconnect }`, `useErrors()` → `{ estop }`

The screen branches its entire UI on `state.machineState.application` (REPS / ISOKINETIC / OPEN_TRAINING / RESISTED_SPRINT).

**`useMachine` command surface (the workflow state machine):**
```
DISCOVER   getConnectedDevices → stopScan
OWN        requestControl ⇄ releaseControl      // acquire exclusive control (= "Tap Accept on the machine")
LIVE       startLiveSubscription                // subscribe to -8002 telemetry chars
CONFIGURE  setExercise · setMachineMode · changeExerciseSettings
LOAD       setLoad · incrementLoad · setVariableLoad · respondToAutoApplyLoad
RUN        setSession · subscribeToSession
REVIEW     getResultCurve · getSessions · sessionResultCount
```

Coach flow: scan → `requestControl` → set exercise/mode → set load (or accept an auto-apply proposal) → run session → results stream via subscription → `releaseControl`.

---

## 5. Screen / route map (expo-router)

`(control)` group:
- `[id].tsx` — machine control **dashboard** (per-machine, the main screen)
- `onboarding.tsx`
- `add-machine.tsx` — add/pair a machine
- `awaiting-pairing.tsx` — waiting for on-machine "Accept" (+ `pairing-help.tsx`)
- `client-select.tsx` — athlete/client picker (the squad grid)
- `exercise-select.tsx` — exercise picker
- `results.tsx` — session results list → `result.tsx` — single result detail

`settings` group:
- `settings/about.tsx`
- `settings/recording.tsx` — `.mcr` recording/replay feature (bundled demo: `sprint2-simple.mcr`)

Nav flow: onboarding → add-machine → awaiting-pairing → `[id]` dashboard; from dashboard → client-select / exercise-select / results → result.

---

## 6. What to borrow for PPA Sprint Trainer

1. **`requestControl` / `releaseControl` ownership model** — an explicit "own the rig" step around ARM, pairs with our CN4/HMI mutual-exclusion safety.
2. **Per-command safety envelope** — every setter carries `reason` + `requestOptions{bypassSafetyChecks, refuseIfOutsideLimits}`. Add `refuseIfOutsideLimits` + a `reason` string to our Modbus write path (we already hard-clamp ±10 kg).
3. **One mode-branching control screen** keyed on drill type (sprint/COD/reps/isokinetic) instead of separate pages.
4. **`autoApplyLoad` propose-then-confirm** — machine proposes a load from bodyweight-%, velocity-decrement-%, or previous trace; coach confirms via `respondToAutoApplyLoad`. Speed + safety over manual setpoint.
5. **Variable-load as a primitive** — `variableLoadEnabled/StartLoad/TargetLoad/TargetSpeed` (a velocity-keyed resistance ramp) maps onto our resistance-curve presets.
6. **Con/ecc load AND con/ecc speed limits** as first-class fields — matches the website control panel; worth mirroring in our setup UI.
7. **Results as a `oneof`** (linear / cod / rep) with `aborted` flag + `dropInProgressResult` — clean handling of the invalid/aborted-rep case (our rep-validity flag).
8. **`CodPhaseStats`** (`topSpeed, time, distance, zeroToFiveTime, isAssisted`) + 3-phase COD decel model — directly comparable to `insights.py`; a concrete target for our COD analytics.
9. **`WatchLineStatus{isAtZeroPosition, lineMovementSpeed}`** — their zeroing/walk-to-mark primitive (one of our open rig-dependent items).

## 7. Artifacts on disk (this session)
- Pulled APKs: `%TEMP%\claude\…\scratchpad\1080apk\` (base.apk + splits)
- `index.android.bundle` (Hermes v96), `decompiled.js` (49 MB), `bundle_strings.txt`
- Decompiler: `hermes-dec` 0.1.5 (`pip install --user hermes-dec`) → `hbc-decompiler`
