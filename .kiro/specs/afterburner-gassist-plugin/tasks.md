# Implementation Plan: NVIDIA G-Assist Plugin for MSI Afterburner

## Overview

This plan implements the plugin bottom-up and test-first so every layer is exercised through
the `AfterburnerInterface` mock boundary with a `FakeAfterburner`. **No real GPU or Afterburner
is required for unit tests.** Language/runtime is **Python + the official `gassist_sdk`**,
speaking G-Assist Plugin **Protocol V2** (JSON-RPC 2.0 over stdin/stdout, 4-byte big-endian
length prefix).

Build order: scaffolding → typed models & error taxonomy → `AfterburnerInterface` + `FakeAfterburner`
→ safety/validation layer → domain services → protocol/plugin wiring → real MAHM/MACM clients →
manifest, packaging, and docs. Each task builds on the previous ones and ends by wiring code into
the running plugin — no orphaned code and no big-bang integration at the end.

The 10 design Correctness Properties (P1–P10) are implemented as Hypothesis property-based tests,
placed next to the code they validate so regressions surface early.

Testing tools: `pytest` (unit) + `Hypothesis` (property-based). All domain tests run with
`FakeAfterburner`; the OS-touching modules — MAHM monitoring, MACM control, and the read-only
profile-file reader — are covered by dedicated tests against sandboxed fixtures, plus
**optional** integration tests where practical. The `ctypes` struct bindings for both
shared-memory interfaces are additionally checked against the committed SDK-layout reference
fixtures (`tests/fixtures/sdk_layouts/`, task 18.3; binding checks in tasks 18.4 and 20.3), which
`tools/generate_sdk_layout_fixtures.py` regenerates (`--generate`) or diffs (`--diff`) against
the installed `SDK\Include\*.h` headers.

---

## Tasks

- [x] 1. Scaffold the plugin package, dependencies, and test harness
  - Create the plugin package layout under the spec project folder: an `afterburner/` package
    (protocol, safety, services, integration subpackages), a top-level `plugin.py` entry point
    placeholder, a `tests/` directory, and a `libs/` folder placeholder for the vendored `gassist_sdk`.
  - Add `requirements.txt` (runtime) and `requirements-dev.txt` (dev) with minimal deps only:
    runtime = standard library + vendored `gassist_sdk` (document that Windows named-shared-memory
    access uses `ctypes` against `kernel32` — stdlib only; `mmap` cannot attach to another process's
    named section);
    dev = `pytest`, `hypothesis`. Avoid heavyweight/optional deps (pydantic stays optional/unused).
  - Add `pytest.ini`/`pyproject.toml` test config (test discovery, Hypothesis profile with a
    minimum of 100 examples per property test) and a placeholder `tests/conftest.py`.
  - _Requirements: 17.1, 18.1_

- [x] 2. Define strongly-typed domain models and the error taxonomy
  - [x] 2.1 Implement enums and value objects
    - Create `afterburner/models.py` with `InterfaceStatus`, `ControlFeature`, `RiskLevel`,
      `ErrorCode`, `DiagnosisCause`, and the `Range` value object (`clamp`, `contains`).
    - _Requirements: 2.1, 5.5, 6.1, 13.1_
  - [x] 2.2 Implement dataclass models
    - Add `GpuLimits` (with `fan_curve_max_points`), `GpuCapabilities` (with `supports()`),
      `GpuTelemetry` (with `sampled_at`, `is_stale`, `driver_version`), `TuningState`, `Profile`,
      `FanCurvePoint`, `FanCurve`, `ControlResult`, `Diagnosis`, and the `PluginError(Exception)`
      type (`code`, `user_message`, `detail`), plus the tuning-ownership types
      `AuthorityState`, `AfterburnerAuthoritySnapshot`, and `TuningOwnershipReport` (single
      shared GPU tuning state; external authorities always `unknown_not_observable`).
    - Add a `rangeFor(feature)` helper on `GpuLimits` used by the validator.
    - _Requirements: 3.1, 5.1, 6.5, 10.1, 12.2, 12.3, 13.1, 19.1, 19.3_
  - [x]* 2.3 Write unit tests for models and `Range`
    - Test `Range.clamp`/`contains` at/inside/outside bounds and inverted/degenerate ranges;
      test `GpuCapabilities.supports` and `GpuLimits.rangeFor`.
    - _Requirements: 5.5, 5.6, 6.2_

- [x] 3. Define the `AfterburnerInterface` Protocol and implement `FakeAfterburner`
  - [x] 3.1 Declare the adapter Protocol (the mock boundary)
    - Create `afterburner/integration/interface.py` with a `runtime_checkable` `AfterburnerInterface`
      exposing exactly: `detect`, `get_version`, `read_telemetry`, `read_all_telemetry`,
      `read_capabilities`, `read_tuning_state`, `list_profiles`, `load_profile`, `reset_tuning`,
      `apply_control`, `apply_fan_curve`. Deliberately include **no** generic `write(offset, value)`
      primitive.
    - _Requirements: 14.1, 17.1_
  - [x] 3.2 Implement `FakeAfterburner`
    - Create `afterburner/integration/fake.py` implementing `AfterburnerInterface` fully in memory:
      configurable detect status, telemetry (including missing/null fields and malformed records),
      capability sets/limits, profiles, and control results. Record every `apply_control`/
      `apply_fan_curve` call so tests can assert whether a write occurred.
    - Support simulating unavailability modes (not installed, not running, interface absent,
      disconnected mid-op) and a stalled MAHM update counter.
    - _Requirements: 17.2, 17.3, 7.4, 16.3_
  - [x]* 3.3 Write structural test that no generic memory-write primitive exists — **Property 6**
    - **Property 6: No arbitrary memory access — only named validated controls**
    - Assert the `AfterburnerInterface` public surface contains only the enumerated named
      operations and no `write`/`poke`/offset/address-taking method; assert `FakeAfterburner`
      conforms via `isinstance` runtime check.
    - **Validates: Requirements 14.1, 14.2, 14.3**

- [x] 4. Implement `HardwareCapabilityResolver` (capability detection & gating)
  - [x] 4.1 Implement capability resolution
    - Create `afterburner/safety/capabilities.py` implementing `resolveCapabilities(gpu_index)`:
      derive supported controls from Afterburner-reported flags + min/max only (no hardcoded
      per-GPU assumptions); require functional MACM for write-capable features; expose read-only +
      `PROFILE_LOAD`/`PROFILE_RESET` when control is unavailable; treat inverted/missing ranges as
      unavailable; time-box resolution to 5s → read-only + error indication.
    - _Requirements: 5.1, 5.2, 5.3, 5.6_
  - [x]* 4.2 Write unit tests for capability detection
    - Cover supported/unsupported/mixed capability sets, MACM-unavailable (read-only) degradation,
      inverted/missing ranges → unavailable, and resolution timeout → read-only.
    - _Requirements: 5.1, 5.2, 5.3, 5.6_
  - [x]* 4.3 Write property test for capability gating — **Property 2**
    - **Property 2: Capability gating — unsupported features are never actuated**
    - Over randomized capability sets, assert that for every feature not in the supported set no
      adapter write is invoked and a typed `UNSUPPORTED_FEATURE` error is returned.
    - **Validates: Requirements 5.3, 6.4, 7.1, 8.5**

- [x] 5. Implement `TuningValidator` (validation, clamping, fan-curve rules)
  - [x] 5.1 Implement value validation & clamping
    - Create `afterburner/safety/validator.py` implementing `validateAndClamp(gpu_index, feature,
      requested_value)`: reject unsupported features (`UNSUPPORTED_FEATURE`), reject missing limits
      (`INTERFACE_UNAVAILABLE`), reject NaN/±inf (`INVALID_VALUE`), otherwise clamp to reported
      `[min,max]` and return `(safe_value, clamped)`.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 14.4, 14.5_
  - [x] 5.2 Implement fan-curve validation
    - Implement `validateFanCurve(gpu_index, curve)`: require `FAN_CURVE` support; require 2 to
      `fan_curve_max_points` points (Afterburner-reported max, default 2, never exceeding 32);
      enforce non-decreasing temperatures; clamp each point's temp/fan into reported ranges;
      raise typed `INVALID_VALUE`/`UNSUPPORTED_FEATURE` and leave prior config unchanged on failure.
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_
  - [x]* 5.3 Write unit tests for tuning & safety boundaries
    - Clamp at/inside/outside min & max, reject NaN/inf, unsupported feature error, missing-range
      error, and report requested-vs-applied values.
    - _Requirements: 6.3, 6.4, 6.5, 6.6, 13.6, 13.7_
  - [x]* 5.4 Write property test for clamping invariant — **Property 1**
    - **Property 1: Clamping invariant — no raw LLM value reaches hardware**
    - Generate arbitrary requested values (far below min, far above max, boundary) against
      randomized reported ranges; assert the value handed to the adapter is always `∈ [min,max]`.
    - **Validates: Requirements 6.1, 6.2, 6.5, 5.4, 7.1, 13.5, 14.4**
  - [x]* 5.5 Write property test for fan-curve monotonicity & bounds — **Property 3**
    - **Property 3: Fan-curve monotonicity and bounds**
    - Generate random candidate curves; assert accepted curves are non-decreasing in temperature
      and every point is within reported bounds, and curves violating monotonicity/point-count are
      rejected.
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4**

- [x] 6. Implement `SafetyPolicy` (risk classification & confirm-token lifecycle)
  - [x] 6.1 Implement risk classification and confirm tokens
    - Create `afterburner/safety/policy.py`: classify each function as LOW/HIGH risk (reads,
      `reset_tuning`, `load_profile` = LOW; `set_*`, `optimize_*` = HIGH); issue single-use tokens
      with `CONFIRM_TOKEN_TTL_SECONDS = 300`; `validateToken` rejects missing/expired/reused tokens
      and marks valid tokens consumed.
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 11.6_
  - [x]* 6.2 Write unit tests for the confirmation flow
    - Issue → validate → consume; reject reused token; reject expired token; reject missing token;
      LOW-risk ops bypass confirmation.
    - _Requirements: 9.1, 9.2, 9.3, 9.4_
  - [x]* 6.3 Write property test for confirmation before high-risk ops — **Property 4**
    - **Property 4: Confirmation before high-risk operations**
    - Over randomized token lifecycles (issue/validate/expire/replay), assert a high-risk write
      occurs only with a currently-valid token and never on missing/expired/reused tokens.
    - **Validates: Requirements 9.1, 9.2, 9.3, 9.4, 11.4**

- [x] 7. Implement `AfterburnerClient` facade over the integration layer
  - Create `afterburner/integration/client.py`: hold the injected `AfterburnerInterface`, orchestrate
    detect/read/profile/control calls, and translate raw adapter failures into typed `PluginError`s
    (graceful degradation; never raise unhandled). Reject Afterburner-dependent operations when no
    interface is injected.
  - _Requirements: 7.4, 7.5, 13.3, 13.4, 17.1, 17.4_

- [x] 8. Implement `TelemetryService` (caching & staleness)
  - [x] 8.1 Implement cache + rate limiting + staleness
    - Create `afterburner/services/telemetry.py` with `TELEMETRY_TTL_SECONDS = 2`,
      `MIN_POLL_INTERVAL_SECONDS = 1`, `STALE_THRESHOLD_SECONDS = 5`; serve cache within TTL;
      enforce min poll interval; flag `is_stale` past threshold or when the MAHM update counter
      stalls; include
      telemetry age and ISO-8601 millisecond timestamps; parse well-formed and malformed MAHM
      records without fabricating values.
    - _Requirements: 3.1, 3.2, 3.4, 3.5, 3.6, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_
  - [x]* 8.2 Write unit tests for telemetry parsing, caching & staleness
    - Well-formed and malformed MAHM records, missing-field → explicit unavailable (no fabricated
      values), multi-GPU indexing, cache hit within TTL, rate-limit enforcement, stale flag past
      threshold, and stalled-counter detection.
    - _Requirements: 3.2, 3.5, 3.6, 4.4, 4.5, 4.6, 4.7_
  - [x]* 8.3 Write property test for caching & rate invariant — **Property 8**
    - **Property 8: Telemetry caching and rate invariant**
    - Issue bursts of reads with randomized timing; assert reads inside the TTL window are served
      from cache (bounded underlying poll count) and reads past the threshold are flagged
      `is_stale=True`.
    - **Validates: Requirements 4.1, 4.2, 4.3**

- [x] 9. Implement `ProfileManager` (list / active / load / reset)
  - [x] 9.1 Implement profile operations
    - Create the read-only profile-file reader (behind `AfterburnerInterface`) and
      `afterburner/services/profiles.py`, per the design document's profile storage layout:
      READ-ONLY scan of the detected installation's Profiles directory (Requirement 14.3); pick
      the per-GPU `VEN_…&FN_*.cfg` file whose name matches the target GPU MAHM `szGpuId`
      (documented in the shipped `MAHMSharedMemory.h` as the `VEN_%04X&…&FN_%d` encoding); treat
      empty slots in either observed form (absent `[ProfileN]` section or `Format=2`-only section);
      skip non-tuning `[Defaults]`/`[Settings]` sections; treat `[Startup]` as auto-apply
      corroboration only (populated = enabled, empty = disabled); never parse either
      `MSIAfterburner.cfg` copy; never write/create/delete in the directory. `get_profiles`
      (at most one active — none when control state is unavailable or ambiguous; empty list when
      none), `load_profile` (parse `[ProfileN]` settings, route every value through the same
      clamp/validation as LLM-supplied values, apply only named validated controls via the
      control interface — capability-gated; reject unknown id leaving active unchanged),
      `reset_profile`; profile apply/reset while the control interface is unavailable returns
      typed `INTERFACE_UNAVAILABLE`/best-effort — never a fabricated list or false success.
    - _Requirements: 10.1–10.9_
  - [x]* 9.2 Write unit tests for profile handling
    - List with one active, empty list, load valid/invalid id, reset, read-only enforcement (no
      writes to the Profiles directory), and best-effort/unavailable classification when the
      control interface is absent.
    - _Requirements: 10.1–10.8_

- [x]* 9.3 Write security acceptance tests for the read-only Profiles-directory guarantee — **Property 9**
    - Sandbox a copy of a Profiles directory taken from the task 9.7 fixtures (real 4.6.7
      layout, incl. the populated/empty `[Startup]` A/B pair and the empty-slot variant). Run Hypothesis-randomized adversarial function arguments (path/traversal/absolute-
      path-looking profile identifiers, arbitrary strings) and adversarial profile contents
      (malformed INI, path-like and traversal values in setting keys, oversized/duplicate
      sections, unexpected section names) through `list_profiles` / `load_profile` /
      `reset_profile` and capability resolution; assert after every operation that the directory
      tree (names, sizes, mtimes, content hashes) is unchanged and that no write-capable handle
      is ever requested on the directory or its files. These tests exercise the real read-only
      profile-file reader on fixture directories; no Afterburner installation is required.
    - _Requirements: 14.3, 14.6, 14.7, 14.8, 10.1_

- [x] 9.4 Implement `TuningOwnershipService` (single shared GPU tuning state)
    - Create `afterburner/services/ownership.py`: compose `AfterburnerClient` reads (`detect`,
      `read_tuning_state`) with `ProfileManager`'s active-profile match and populated `[Startup]` auto-apply
      presence into the typed `TuningOwnershipReport`; mark every external authority (NVIDIA App
      Automatic Tuning, G-Assist native tuning, other OC utilities) `UNKNOWN_NOT_OBSERVABLE` —
      never asserted, never modified; complete as success without a write when Afterburner's
      applied state already equals the requested value within tolerance; build the ownership
      clause used by risky-control confirmation prompts.
    - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.5_

- [x]* 9.5 Write unit tests for tuning-ownership reporting
    - Report marks external authorities `unknown_not_observable` in every state; summary never
      claims stacking or asserts external state; no-op success (no adapter write) when applied
      state equals the request; risky confirmations carry the ownership clause.
    - _Requirements: 19.1, 19.2, 19.3, 19.4_

- [x]* 9.6 Write property test for tuning-ownership honesty — **Property 10**
    - **Property 10: Tuning-ownership honesty — no stacking claims, no fabricated
      external-authority knowledge**
    - Over randomized `FakeAfterburner` ownership states, assert (a) external authorities always
      report `unknown_not_observable`; (b) applied state equal to the request results in success
      with no adapter write; (c) risky-control confirmations carry the ownership clause.
    - **Validates: Requirements 19.1, 19.2, 19.3, 19.4, 19.5**

- [x]* 9.7 Create profile-layout fixtures for the sandbox tests — incl. the `[Startup]` A/B pair
    - Add `tests/fixtures/profiles/` mirroring a real Afterburner 4.6.7 Profiles directory per the
      design profile layout, one variant per scenario. The Property 9 tests (9.3) deep-copy a
      variant into a sandbox and mutate only the copy; canonical fixtures are never opened for
      writing. Layout:
      ```
      Profiles/
      +- Profile1.cfg  Profile2.cfg  Profile3.cfg     # markers ([Settings] / ProfileContents=1)
      +- MSIAfterburner.cfg                            # global: RememberSettings, LockProfiles, fan blob
      +- VEN_10DE&DEV_2F04&SUBSYS_89E61043&REV_A1&BUS_11&DEV_0&FN_0.cfg   # per-GPU tuning file
      ```
      The per-GPU file carries `[Startup]`, `[Profile1..3]`, `[Defaults]`, and `[Settings]`
      (`CaptureDefaults=0`) sections with representative values observed on 4.6.7: `Format=2`,
      `PowerLimit=100`, `CoreClkBoost=95000`, `MemClkBoost=200000/400000/600000` per slot,
      `FanMode=1`, `FanSpeed=31` in the slots (30 in `[Defaults]`), `CoreVoltageBoost=0`, and a
      `VFCurve` hex blob (canonical fixtures copy the captured bytes; inline examples use a
      short deterministic placeholder).
    - **`[Startup]` A/B variants** (drive startup-auto-apply detection and the immutable-tree
      assertions):
      ```
      A - disabled (all keys empty):      B - enabled (populated, observed on 4.6.7):
      [Startup]                           [Startup]
      Format=2                            Format=2
      PowerLimit=                         PowerLimit=100
      CoreClkBoost=                       CoreClkBoost=95000
      VFCurve=                            VFCurve=<hex placeholder>
      MemClkBoost=                        MemClkBoost=200000
      FanMode=                            FanMode=1
      FanSpeed=                           FanSpeed=30
      FanMode2=                           FanMode2=
      FanSpeed2=                          FanSpeed2=
      CoreVoltageBoost=                   CoreVoltageBoost=0
      ```
      Variant B pairs with `RememberSettings=1` in `MSIAfterburner.cfg`; variant A with
      `RememberSettings=0`. An additional empty-slot variant omits `[Profile4]`/`[Profile5]`
      sections and their markers (observed when fewer than five slots are saved).
    - Fixtures are reused by profile parsing/listing unit tests (9.2) and by ownership reporting
      (9.5) for the populated-vs-empty `[Startup]` cases.
    - _Requirements: 10.1, 14.3, 14.8_

- [x] 10. Implement `DiagnosticsService` (evidence-based classification)
  - [x] 10.1 Implement diagnosis classification
    - Create `afterburner/services/diagnostics.py` implementing `diagnosePerformance(gpu_index)`:
      return `UNKNOWN_INSUFFICIENT_DATA` when telemetry is stale/missing essential fields; otherwise
      classify one of thermal/power/voltage/utilization-bottleneck/CPU-limited/app-behavior with
      non-empty evidence (field name + value) and a confidence strictly between 0.0 and 1.0.
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_
  - [x]* 10.2 Write unit tests for diagnostics
    - Each limiter branch with representative telemetry; evidence contains field name + value;
      confidence within (0.0, 1.0); stale/missing → `UNKNOWN_INSUFFICIENT_DATA`.
    - _Requirements: 12.1, 12.2, 12.4, 12.5_
  - [x]* 10.3 Write property test for diagnostics honesty — **Property 5**
    - **Property 5: Diagnostics never overclaim on stale or missing data**
    - Inject stale/missing/malformed telemetry; assert every inferred-cause diagnosis has
      `confidence < 1.0` and insufficient inputs always return `UNKNOWN_INSUFFICIENT_DATA`.
    - **Validates: Requirements 12.3, 12.4**

- [x] 11. Implement optimization intents (`optimize_quiet`, `optimize_thermal`)
  - [x] 11.1 Implement focused fan/thermal optimization
    - In `afterburner/services/optimize.py`: `optimize_quiet` (reduce fan setpoint ≥10 pts while
      keeping temp ≤83°C, else error) and `optimize_thermal` (target 40–95°C inclusive, reject
      out-of-range/non-numeric); restrict changes to fan/thermal controls only; route through
      validator + safety confirmation.
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_
  - [x]* 11.2 Write unit tests for optimization intents
    - Quiet reduces fan while respecting temp limit (and refuses when impossible); thermal accepts
      in-range targets and rejects out-of-range/non-numeric; assert clock/voltage/power/memory are
      never modified.
    - _Requirements: 11.1, 11.2, 11.4, 11.5_

- [x] 12. Implement the typed error → user-facing message mapping
  - [x] 12.1 Implement the error mapper
    - Create `afterburner/errors.py` mapping every `ErrorCode` to a user-facing message (condition +
      affected component/value + next action) and to a Protocol V2 error code; unmatched conditions
      map to `COMM_FAILURE`. Clamping is a success `ControlResult` (message states requested and
      applied values) and is never an error; `DISCONNECTED` states nothing was changed.
    - _Requirements: 13.1, 13.2, 13.3, 13.5, 13.6, 13.7_
  - [x]* 12.2 Write unit tests for the error taxonomy → message mapping
    - Every `ErrorCode` yields a non-empty actionable message and correct V2 code; unknown →
      `COMM_FAILURE`; clamped success results include requested + applied values and are not errors.
    - _Requirements: 13.1, 13.2, 13.3, 13.6, 13.7_

- [x] 13. Checkpoint — Ensure all tests pass
  - Ensure all unit and property tests (Properties 1–6, 8, 9, 10) pass against `FakeAfterburner`. Ask the
    user if questions arise.

- [x] 14. Implement the G-Assist Protocol V2 transport (`GAssistProtocol`)
  - [x] 14.1 Implement framing and JSON-RPC transport
    - Create `afterburner/protocol/transport.py`: 4-byte big-endian length-prefixed UTF-8 JSON-RPC
      2.0 over stdin/stdout; reject frames > 10 MB and invalid UTF-8/JSON with a parse/invalid-request
      error without terminating the loop; delegate to `gassist_sdk` where available.
    - _Requirements: 1.1, 1.2_
  - [x]* 14.2 Write unit tests for framing/transport
    - Round-trip encode/decode of framed messages; oversized frame rejected; malformed payload →
      error response with loop intact.
    - _Requirements: 1.1, 1.2_

- [x] 15. Implement the plugin command layer and confirmation wiring (`GAssistPlugin`)
  - [x] 15.1 Implement lifecycle and dispatch
    - Create `afterburner/protocol/plugin.py`: handle `initialize` (≤5s), `ping`→`pong` (≤1s),
      `execute` dispatch (≤30s, unknown function → error, retain state), `shutdown` (release
      resources, exit ≤5s); register all functions (including the read-only `get_tuning_ownership`,
      dispatched to `TuningOwnershipService`) and dispatch to domain services; format
      NL-friendly responses via the error mapper.
    - _Requirements: 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 13.4, 19.3_
  - [x] 15.2 Wire the confirmation flow into risky commands
    - Implement `executeRisky` in the plugin: validate/clamp first; HIGH-risk ops without a token
      return a success `complete` with `needs_confirmation` (never an error). Two confirmation
      paths: (1) token re-invocation with `CONFIRM_TOKEN_TTL_SECONDS = 300`; (2) passthrough
      `input` fallback via a registered `on_input` command (ack within 2 s, confirm/cancel within
      60 s). Apply only on a valid token/passthrough ack. Risky confirmation prompts include
      the ownership clause (Requirement 19.2); when Afterburner's applied state already equals the
      requested value, return success without prompting or writing (Requirement 19.4).
    - _Requirements: 1.10, 1.11, 9.1, 9.2, 9.3, 11.6, 19.2, 19.4_
  - [x]* 15.3 Write unit tests for the plugin layer (with `FakeAfterburner`)
    - Initialize/ping/execute/shutdown behavior, unknown-function error, risky-op confirm/cancel/
      timeout, and read ops requiring no confirmation.
    - _Requirements: 1.5, 1.7, 1.10, 1.11, 9.4_
  - [x]* 15.4 Write property test for graceful degradation — **Property 7**
    - **Property 7: Graceful degradation — no crash when Afterburner is unavailable**
    - Simulate each unavailability mode (not installed, not running, interface absent, disconnected
      mid-op) via `FakeAfterburner`; assert every call returns a typed `PluginError` with a
      user-facing message and never raises an unhandled exception.
    - **Validates: Requirements 2.6, 7.3, 7.4, 13.3, 16.2, 16.3**

- [x] 16. Implement the plugin entry point and startup sequence
  - Implement `plugin.py`: construct the stack (inject the concrete `AfterburnerInterface`), run
    the startup sequence (exactly one capability detection + one telemetry read within 3s, degrade
    on timeout/absence), and run the protocol message loop. When tasks 18–20 landed, the entry's
    injected interface became `CombinedAfterburner` — the live `AfterburnerMonitoringClient` (MAHM
    reads) plus the capability-gated `AfterburnerControlClient` (MACM control) — degrading to the
    honest `UnavailableAfterburner` state when Afterburner is absent or its shared-memory map is down.
  - _Requirements: 16.1, 16.2, 16.3, 16.4, 17.4_

- [x] 17. Checkpoint — Ensure all tests pass
  - Ensure all unit and property tests (Properties 1–10) pass against `FakeAfterburner`. Ask the
    user if questions arise.

- [x] 18. Implement the real MAHM monitoring client (official, OS/shared-memory)
  - [x] 18.1 Implement `AfterburnerMonitoringClient`
    - Create `afterburner/integration/mahm.py` implementing the read-only parts of
      `AfterburnerInterface` against MAHM shared memory (via `ctypes`/`kernel32`): detect/version,
      parse header (validate the MAHM signature and interface version v2.0 against the installed
      `SDK\Include\MAHMSharedMemory.h` layout) + per-GPU entries into typed
      telemetry, limits, capabilities, and tuning state; never write to MAHM. Note in the module
      docstring this is the official interface and one of the three OS-touching modules (MAHM,
      MACM, and the read-only profile-file reader). Wired into the plugin entry point at task 18
      completion; live-verified on the 4.6.7.17439 install (MAHM v2.0, `dwVersion = 0x00020000`;
      typed per-GPU telemetry read end-to-end).
    - _Requirements: 3.1, 3.3, 3.5, 2.1, 2.2, 2.3, 2.4, 2.7_
  - [x]* 18.2 Write optional integration test for MAHM monitoring
    - Opt-in, skipped when Afterburner is absent: read-only smoke test + version/availability
      detection against a real installation. Executed live on the 4.6.7.17439 install: the MAHM
      map reported the v2.0 signature/version and per-GPU telemetry parsed correctly.
    - _Requirements: 2.1, 3.1_
  - [x] 18.3 Mirror both SDK struct layouts into committed reference fixtures + add the
    layout-generation script (MAHM and MACM headers)
    - Create `tests/fixtures/sdk_layouts/` with committed reference snapshots of the two
      officially shipped headers as verified on the 4.6.7 install: one JSON per header — MAHM
      (`MAHM_SHARED_MEMORY_HEADER`, `MAHM_SHARED_MEMORY_ENTRY`, `MAHM_SHARED_MEMORY_GPU_ENTRY`)
      and MACM (`MACM_SHARED_MEMORY_HEADER`, `MACM_SHARED_MEMORY_GPU_ENTRY`, incl. the nested
      `VF_POINT_ENTRY` / `POWER_TUPLE_ENTRY` / `THERMAL_TUPLE_ENTRY` / `VF_CURVE`) — recording
      every field's name, `ctypes` type, size, and offset, plus the header constants (`'MAHM'` /
      `'MACM'` signatures, `0x00020000` v2.0, MACM `MACM_SHARED_MEMORY_COMMAND_*` values, the
      GPU-entry flag bits, MAHM `MONITORING_SOURCE_ID_*` and entry-flag values). The fixtures are
      committed so binding checks always run in CI even when no Afterburner install is present.
    - Create `tools/generate_sdk_layout_fixtures.py` that parses the installed
      `SDK\Include\MAHMSharedMemory.h` / `MACMSharedMemory.h` when present:
      `--generate` rewrites the committed fixtures from the live headers; `--diff` compares and
      exits non-zero on any field/type/offset/size/constant drift so CI fails when an Afterburner
      update changes a layout. Run against the live installed header on any version change
      (per-release re-verification); when the install is absent the diff is skipped and the
      committed fixtures stand.
    - Add a CI unit test (no Afterburner install required) validating that the committed
      fixtures are internally consistent — each fixture's shape is checked against the
      normative JSON Schema / strict loader in design (§ Fixture JSON shape) — then the layout
      rules are replayed from the recorded field declarations and type table: for every
      struct, first field at offset 0; field
      offsets strictly increasing in declaration order; `offset + size <= struct size` for
      each field; struct size = align-up of the last field's end to the struct alignment;
      struct alignment = max member alignment (type table: `DWORD`/`LONG`/`time_t`/
      `__time32_t`/`float` align 4, `char[N]` aligns 1). Also assert every nested/array
      element type reference resolves to a recorded struct and arrays have
      `size = count × size(element)`. The layout rules live in one shared helper imported
      by both the generator and this test, so they can never silently diverge; the test
      catches corrupted, truncated, or hand-edited fixtures (and any mismatch between the
      recorded offsets/sizes and a replay of the rules) so CI fails before the ctypes
      binding checks (18.4 / 20.3) run against a bad fixture.
    - This task owns the fixture files, the generator, and the fixture-consistency unit
      test above. The fixture-vs-`mahm.py` binding assertion and the runtime
      signature/version validator tests are task 18.4 (the monitoring-side mirror of 20.3),
      which reuse this task's fixtures and generator; 20.3 reuses the MACM half of these
      fixtures the same way.
    - _Requirements: 2.1, 2.4, 3.1_
  - [x] 18.4 Verify MAHM ctypes bindings and runtime signature/version validation
    - Monitoring-side mirror of 20.3. Add unit tests that (a) load the installed
      `SDK\Include\MAHMSharedMemory.h` from the detected Afterburner install when present and
      assert the `ctypes.Structure` definitions in `mahm.py` match it — field names, sizes,
      and offsets for `MAHM_SHARED_MEMORY_HEADER`, `MAHM_SHARED_MEMORY_ENTRY`, and
      `MAHM_SHARED_MEMORY_GPU_ENTRY` plus the header constants (`'MAHM'` signature,
      `0x00020000` v2.0, `MONITORING_SOURCE_ID_*` values, entry-flag bits) — falling back to
      the committed fixture from task 18.3 (`tests/fixtures/sdk_layouts/mahm_layout.json`)
      when the install is absent so the check always runs, and run
      `tools/generate_sdk_layout_fixtures.py --diff` (task 18.3) against the live header to
      catch layout drift; and (b) drive the runtime validator with injected shared-memory
      fixtures asserting it rejects a deallocated/absent map (`0xDEAD` marker per the
      header), a wrong signature (≠ `'MAHM'`), an unsupported version (≠ `0x00020000`),
      or zero/oversized `dwEntrySize` / `dwGpuEntrySize` — including a declared region
      `dwHeaderSize + dwNumEntries × dwEntrySize + dwNumGpuEntries × dwGpuEntrySize` that
      exceeds the mapped size — returning typed `INTERFACE_UNAVAILABLE`/
      `UNSUPPORTED_VERSION` and never crashing, and that the client never requests write
      access to the map (read-only monitoring; no `FILE_MAP_WRITE` open) so no malformed
      header can ever cause a write. Re-run (a) against the live installed header on any
      version change (per-release re-verification).
    - _Requirements: 2.1, 2.4, 3.1, 7.3, 7.4_

- [x] 19. Research gate — document tuning-authority precedence before the control layer
  - Create `research/tuning-authority-precedence.md` recording, with sources labeled by status and
    a re-verify-at-implementation caveat: which driver interfaces each authority writes (G-Assist
    native tuning via NVIDIA performance/tuning interfaces; NVIDIA App Automatic Tuning via
    driver/NVAPI-based auto tuning; MSI Afterburner via MAHM/MACM (SDK headers and samples
    verified on Afterburner 4.6.7) plus its apply-at-Windows-startup profile auto-apply); that the GPU exposes ONE shared set of driver tuning parameters whose
    values do not stack across tools; what is observable vs NOT observable through Afterburner
    interfaces; and known cross-tool evidence (e.g., OC offsets written by other utilities not
    recognized via NVAPI perf-pstate deltas). Conclude with design consequences (Requirements
    19.1–19.5) and any unresolved questions to re-validate on real hardware during the optional
    integration tests (18.2, 20.2).
  - Checkpoint with the user: the MACM control client (Task 20) MUST NOT be implemented until
    this document exists and is reviewed (Requirement 19.6).
  - _Requirements: 19.6_

- [x] 20. Implement the capability-gated MACM control client (official SDK header, OS/shared-memory)
  - [x] 20.1 Implement `AfterburnerControlClient`
    - Create `afterburner/integration/macm.py` implementing the control parts of
      `AfterburnerInterface` against MACM shared memory: write only known control fields with
      already-validated/clamped values (no generic memory writer); report `INTERFACE_UNAVAILABLE`/
      `ACCESS_DENIED`/`DISCONNECTED` cleanly. Follow the concrete write protocol in design.md § “MACM Control Write Sequence (observed
      from the shipped SDK)”: mutex-guarded named-field write, `dwCommand = FLUSH` set last,
      completion poll, read-back verification. Note in the module docstring this is the official MACM control interface — shipped SDK header
      `SDK\Include\MACMSharedMemory.h` (v2.0, `'MACM'` signature) — and the only privileged write
      path. Bind structs to the installed header and validate the shared-memory signature/version
      at runtime; re-verify per release (older installs may lack the SDK, in which case control is
      capability-gated unavailable). **Do not begin until the Task 19 research gate has been
      completed and reviewed (Requirement 19.6).**
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 13.5, 14.1, 19.6_
  - [x]* 20.2 Write optional integration test for MACM control
    - Opt-in behind an explicit flag, skipped when Afterburner/MACM absent: apply-and-read-back a
      single clamped control; assert graceful typed error when unavailable. Also assert the
      write-time no-op: when the applied state already equals the request inside the write
      transaction, success is returned without issuing FLUSH (Requirement 19.4 — the authoritative
      check happens at write time, after any confirmation window).
    - Ran **live and PASSING (2/2, elevated) against the real Afterburner 4.6.7.17439 install**: the
      write-time no-op, and a real power-limit apply 100% → 103% with read-back verification and
      restore to 100% (both verified). Two findings from the live run: (a) **elevation** — Afterburner
      runs elevated on this machine and never confirms MACM FLUSH commands from a non-elevated
      process within the protocol's 5 s budget (writes land, `dwCommand` never clears, and the value
      may be applied minutes later); the suite must run elevated when Afterburner is. (b) a latent
      test bug fixed (`result.replaced` → `result.replaced_value`), visible only when the suite
      actually runs live. `AfterburnerControlClient` was hardened accordingly: a best-effort
      elevation-mismatch check (`_afterburner_elevation_mismatch`) fails **fast with `ACCESS_DENIED`**
      and a targeted “restart elevated” message before any FLUSH is issued on an actual write
      (no-ops still succeed — they need no Afterburner action), and the completion-timeout path maps
      a detectable mismatch to the same targeted error instead of the generic
      `INTERFACE_UNAVAILABLE` timeout (5 new unit tests in `tests/test_macm.py`).
    - _Requirements: 7.1, 7.4_
  - [x] 20.3 Verify MACM ctypes bindings and runtime signature/version validation
    - Add unit tests that (a) load the installed `SDK\Include\MACMSharedMemory.h` from the
      detected Afterburner install when present and assert the `ctypes.Structure` definitions in
      `macm.py` match it — field names, sizes, and offsets for the MACM header and GPU-entry
      structs plus the header constants (`MACM_SHARED_MEMORY_COMMAND_*` values, expected `0x00AB0000`
      INIT/FLUSH commands) — falling back to the committed fixture from task 18.3
      (`tests/fixtures/sdk_layouts/macm_layout.json`) when the install is absent so the
      check always runs, and run `tools/generate_sdk_layout_fixtures.py --diff` (task 18.3)
      against the live header to catch layout drift; and (b) drive the runtime
      validator with injected shared-memory fixtures asserting it rejects a deallocated/absent map
      (`0xDEAD` marker per the header), a wrong signature, an unsupported version (v1 `0x00010000`
      or a future major ≥ `0x00030000`; any v2.x `0x00020000`–`0x0002FFFF` whose entry layout
      matches the bound header is accepted — the strict `== 0x00020000` pin was relaxed because the
      live 4.6.7.17439 map reports `0x00020003`, v2.3), or a zero/oversized `dwGpuEntrySize`,
      returning typed
      `INTERFACE_UNAVAILABLE`/`UNSUPPORTED_VERSION` and never attempting a control write until the
      signature/version check passes (capability gating — design Property 2). Re-run (a) against
      the live installed header on any version change (per-release re-verification).
    - _Requirements: 2.1, 2.4, 7.1, 7.3, 7.4, 14.1_

- [x] 21. Author `manifest.json` (Protocol V2, NL-friendly)
  - Create `manifest.json` with Protocol V2 required fields (name, version, entry point) and the full
    function set with NL-friendly `name`/`description`/`properties` for `get_gpu_status`,
    `get_gpu_limits`, `get_tuning_state`, `get_profiles`, `show_configuration`,
    `get_tuning_ownership`, `load_profile`,
    `reset_tuning`, `set_power_limit`, `set_core_offset`, `set_memory_offset`, `set_fan_percent`,
    `set_fan_curve`, `optimize_quiet`, `optimize_thermal`, `diagnose_performance` (control functions
    gated at runtime).
  - _Requirements: 1.12, 18.1, 18.4, 19.3_

- [x] 22. Implement the build/package script with deliverable verification
  - [x] 22.1 Implement the packaging script
    - Create `build.py` (or `package.py`) that assembles the plugin into the Protocol V2 install
      layout (`manifest.json` + `plugin.py` + `afterburner/` + vendored `libs/gassist_sdk`), excludes
      any Afterburner/RTSS binaries, and **fails with a per-item error naming each missing
      deliverable** (build/package script, install instructions, ≥3 example prompts, troubleshooting
      docs, manifest, config).
    - Implemented as root `build.py` (stdlib-only; `--check` / `--out` / `--sdk` /
      `--allow-no-sdk` / `--force`; exit 0 ok / 1 missing deliverable / 2 packaging error).
      Per-item checks: `build-script`, `manifest` (Protocol V2 shape), `executable`
      (manifest's executable file), `package` (`afterburner/__init__.py`), `vendored-sdk`
      (`libs/gassist_sdk/` — an assembly-time input via `--sdk`, not a repo requirement: it is
      deliberately uncommitted per `libs/README.txt`), `install-instructions`, `example-prompts`
      (≥3 quoted bullets under an "Example prompts" heading), `troubleshooting` (README heading),
      and `config` (optional — validated only when present). Assembly whitelist-copies
      `manifest.json` + `plugin.py` + `afterburner/*.py` + `libs/` into `dist/afterburner` (or
      `--out`), then scans the finished tree and refuses to package any Afterburner/RTSS-named or
      binary payload (exit 2, names the file), and re-verifies the artifact itself.
    - _Requirements: 15.1, 18.1, 18.2, 18.3_
  - [x]* 22.2 Write unit tests for the deliverable-verification logic
    - Assert the verifier fails and names each missing deliverable, and passes when all are present.
    - `tests/test_build.py` (21 tests, no Afterburner install): per-item missing-deliverable
      naming for all eight required checks, pass-when-all-present, invalid-manifest and
      missing-declared-executable cases, config-optionality, README heading/bullet conventions,
      assembly layout + whitelist exclusions, forbidden-binary refusal (RTSS.exe / MSIAfterburner,
      partial output removed), vendored-SDK require/override/`--allow-no-sdk`, `--force` guard,
      and CLI exit codes 0/1. Live `--check` at the repo root currently names the two
      genuinely missing deliverables (`example-prompts`, `troubleshooting` — authored in task
      23), so the Task 24 final checkpoint can only pass once the docs exist.
    - _Requirements: 18.2, 18.3_

- [x] 23. Write documentation and example content
  - Create `README.md` with install instructions (install path
    `%PROGRAMDATA%\NVIDIA Corporation\nvtopps\rise\plugins\afterburner\`, vendored `libs/gassist_sdk`),
    at least 3 example G-Assist prompts, troubleshooting docs covering installation-failure and
    interface-unavailable scenarios,    and an interface-status section listing each Afterburner
    interface as **official** (G-Assist V2, MAHM monitoring), **community reference** (third-party
    MAHM wrappers), or **official SDK header**
    (MACM control — `SDK\Include\MACMSharedMemory.h`, re-verified per release against the
    installed header), plus the no-redistribution statement.
  - Root `README.md` rewritten from the spec-era scaffold to the full user-facing document:
    build-then-install instructions for the Protocol V2 path (with `build.py --check` / `--sdk`),
    a 6-prompt **Example prompts** section, the corrected interface-status table (MACM now
    **official SDK header** — the shipped `MACMSharedMemory.h` + sample, re-verified per release;
    stale “reverse-engineered” claim removed), the Requirement 19 tuning-ownership model with the
    “What's controlling my GPU overclock right now?” prompt, the three-path Distribution &
    Submission section with the no-redistribution caveat, a Troubleshooting section covering
    installation-failure and interface-unavailable/elevation/staleness scenarios plus log paths,
    and development/verification commands. Meets the build.py conventions exactly: live
    `build.py --check` at the repo root now PASSES (8/8), and a full `build.py --sdk` assembly
    produced a verified 32-file Protocol V2 artifact.
  - _Requirements: 15.1, 18.2, 18.4_
  - Document the Requirement 19 tuning-ownership model in the README (the GPU has one shared
    tuning state; the plugin applies changes only through Afterburner; NVIDIA App Automatic
    Tuning / G-Assist native tuning are not observable through Afterburner and may override or be
    overridden) and include an example prompt: "What's controlling my GPU overclock right now?"
  - _Requirements: 19.1, 19.3_

- [x] 24. Final checkpoint — Ensure all tests pass and the package builds
  - Run the full test suite (Properties 1–10 + all unit tests) against `FakeAfterburner` and run the
    build script's deliverable verification. Ask the user if questions arise.
  - **PASS.** Full suite: **347 passed** (2 MACM control-integration tests remain opt-in behind
    `MSI_AFTERBURNER_CONTROL_INTEGRATION=1`). Properties P1–P10: all **23 property-test
    functions** pass (Hypothesis ≥100 examples each against `FakeAfterburner`; P9 against
    sandboxed fixture trees). `python build.py --check`: **8/8 deliverables, exit 0**.
    End-to-end package build with a vendored `--sdk` copy: **exit 0, verified 8/8, 32-file
    Protocol V2 artifact** (`manifest.json` + `plugin.py` + `afterburner/` + `libs/gassist_sdk`
    + `README.md` + `LICENSE`). Parent rows 2–6, 8, and 12 were re-marked `[x]` — their
    subtasks were complete; the parent markers had been missed by earlier marking passes.
    Every task row (1–24) is now `[x]`. No questions arose.
  - **Live-conversation follow-up (plugin installed in the real `nvtopps\rise\plugins\` dir):**
    asked G-Assist *"What's my GPU temperature and fan speed right now?"* in NVIDIA App. Engine
    logs + artifacts confirm: (a) **the plugin WAS discovered** — `rise\assistants\G-Assist\
    function_embeddings.bin`, rebuilt at session time (06:16:54), indexes all 16 afterburner
    functions (verified by ASCII scan) alongside the reference plugins'; but (b) **the reply came
    from the engine's native system-info capability, not the plugin** — the session's
    `core_system_info_embeddings.bin` holds the live system snapshot that grounded the answer
    (`gpu: thermal: temperature_c=47.0`, `fan_speed_rpm=0.0`, matching the 47°C / 0.0 RPM
    reply verbatim), and no RISE python process was ever spawned. This is correct platform
    behavior, not a bug: generic GPU telemetry is a core G-Assist capability answered natively;
    the plugin is selected for Afterburner-specific intents (tuning state, power limit, profiles,
    fan curve, ownership, diagnostics, writes) that native system info cannot answer. To observe
    the plugin    invoked live, ask an Afterburner-specific question (e.g. power limit / profile /
    ownership), with the NVIDIA App host elevated for any write.
  - **Live engine-contract conformance fix:** the Afterburner-specific question
    (*"What power limit does MSI Afterburner have applied right now?"*) DID launch the
    plugin (whole package imported under the RISE python at session time, pycache
    evidence) but the engine answered *"Could not parse JSON-RPC message from afterburner
    plugin. Ensure plugin uses Protocol V2."* Root cause: our wire MESSAGE SHAPES
    diverged from the real Protocol V2 contract (framing was byte-identical — 4-byte BE
    length + JSON-RPC 2.0). Diffing against NVIDIA's migration guide and the
    `gassist_sdk` vendored in the reference plugins (`rise\plugins\modio\libs\gassist_sdk`)
    fixed `afterburner/protocol/plugin.py` toward the engine-verified envelope: `complete`
    notifications carry `{request_id, success, data, keep_session}`; failures are `error`
    notifications `{request_id, code, message}`
    (code via `errors.protocol_code_for`); `ping` echoes the engine's `timestamp`;
    `execute` arguments arrive under `arguments`; `input` is acknowledged
    (`{"acknowledged": true}`) before resolving the confirmation verdict; `shutdown` is a
    notification answered with NO frame; the `initialize` result carries
    name/version/protocol_version/commands (manifest-backed). Entry `plugin.py` now logs
    lifecycle + crashes to `rise\plugins\afterburner\afterburner.log` (never the wire
    channel; falls back to %TEMP% when the plugin dir is unwritable — a startup log crash
    reads as a protocol failure to the engine). Tests re-targeted to the SDK-exact shapes;
    real-process harness (`tools/smoke_process_plugin.py`) drives
    initialize→ping→execute→input(ack+cancel)→diagnose→shutdown-notification and PASSES
    live under the RISE python. (First pass left `data` as an OBJECT `{message, ...}` —
    see the final finding below for why that still failed and the real contract.)
  - **Live conversation after conformance fix:** the engine now launches the plugin,
    parses its frames, and renders our typed errors verbatim ("❌ Plugin error: MSI
    Afterburner isn't running"). That exposed the second bug: the engine keeps ONE plugin
    process per session (`persistent: true`), and the interface was chosen ONCE at spawn
    — if Afterburner wasn't exposing MAHM at that instant, every later call (even after
    the user started Afterburner) stayed "isn't running" forever. Fix: new
    `ReconnectingAfterburner` (`afterburner/integration/reconnect.py`) wraps a client
    *factory*; while unavailable it reports the concrete typed status and probes at most
    every 2 s (design cadence), then swaps in a fresh live MAHM/MACM client transparently
    on the first call at least 2 s after Afterburner appears. 5 unit tests prove
    delegate-when-OK / typed-errors-while-down / cooldown gating / swap-on-recovery;
    recovery proven live against the real RTX 5070. Entry-point logging also records the
    "detected again" swap in `afterburner.log`. Full suite 361 passed.
  - **FINAL root cause of the persistent "Could not parse JSON-RPC message" (2026-09-06,
    string-data contract):** after the reconnect fix, the installed build served a real
    session cleanly — the wire transcript (`afterburner.log`, 07:44:28) shows initialize
    → execute(get_tuning_state) → a well-formed `complete` carrying live tuning data — yet
    the engine STILL reported the parse error, while error notifications (strings)
    rendered fine at 07:15. The user pointed us at NVIDIA's public plugin repo
    (github.com/NVIDIA/G-Assist); diffing against its canonical sources settled it:
    **Protocol V2 has no structured output channel — `complete.params.data` is the NL
    text STRING** (PROTOCOL_V2.md + PLUGIN_MIGRATION_GUIDE_V2.md + the SDK + every
    reference plugin agree; the official `plugin_emulator` — the engine-side ground
    truth — crashes on dict data with "can only concatenate str (not \"dict\") to str",
    precisely the failure the real engine reported). Our `complete` sent
    `data: {message, ...structured fields...}`, so the engine's handler threw the moment
    it touched our frame. Fix: `_complete()` now sends `data` = the message text; the
    confirmation token (Req 9.1 single-use + TTL) is issued and consumed INSIDE the
    plugin and never travels on the wire — risky prompts are a `complete`
    (`keep_session: true`) whose text asks for confirmation, and the user verdict arrives
    as `input` (acknowledged, then resolved against the internal pending state; apply /
    cancel / 60 s timeout). Structured executor results remain internal (domain layer),
    and manifest `confirm_token`-style round-trips are gone from the wire. Verified:
    full suite 361 passed re-targeted to string data + input-based confirmation; the
    official plugin_emulator runs the rebuilt dist with ZERO parse errors (live reads,
    risky prompt with keep_session, input confirm routed — apply correctly gated by the
    elevation mismatch, an expected typed error); the real-process harness PASSES under
    the RISE python against `dist/afterburner`. Pending: elevated reinstall into
    `rise\plugins\afterburner` + one real App retry.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core
  implementation tasks are never optional.
- The two optional integration tests (18.2, 20.2) require a real user-installed Afterburner and are
  skipped when it is absent — they are the only tasks needing hardware.
- Each task references specific requirement sub-clauses for traceability.
- Property-based tests (P1–P10, minimum 100 Hypothesis examples each) validate universal correctness
  properties; unit tests cover specific examples and edge cases.
- `afterburner/integration/mahm.py` (MAHM, official) and `afterburner/integration/macm.py` (MACM,
  official SDK header) are the only modules that touch OS shared memory, and the read-only
  profile-file reader (Profiles directory) is the only filesystem-touching module; everything
  else is unit-tested through the `AfterburnerInterface` mock boundary with `FakeAfterburner`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2"] },
    { "id": 3, "tasks": ["2.3", "3.1"] },
    { "id": 4, "tasks": ["3.2"] },
    { "id": 5, "tasks": ["3.3", "4.1"] },
    { "id": 6, "tasks": ["4.2", "4.3", "5.1", "5.2", "6.1", "8.1", "9.1", "12.1"] },
    { "id": 7, "tasks": ["5.3", "5.4", "5.5", "6.2", "6.3", "8.2", "8.3", "9.2", "9.7", "12.2", "7"] },
    { "id": 8, "tasks": ["10.1", "9.4"] },
    { "id": 9, "tasks": ["10.2", "10.3", "11.1"] },
    { "id": 10, "tasks": ["11.2", "14.1"] },
    { "id": 11, "tasks": ["14.2", "15.1"] },
    { "id": 12, "tasks": ["15.2"] },
    { "id": 13, "tasks": ["15.3", "15.4", "16"] },
    { "id": 14, "tasks": ["18.1", "19", "21"] },
    { "id": 15, "tasks": ["18.2", "18.3", "18.4", "20.1", "20.2", "20.3", "22.1", "22.2", "23"] },
    { "id": 16, "tasks": ["24"] }
  ]
}
```
