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

The 8 design Correctness Properties (P1–P8) are implemented as Hypothesis property-based tests,
placed next to the code they validate so regressions surface early.

Testing tools: `pytest` (unit) + `Hypothesis` (property-based). All tests run with
`FakeAfterburner`; only the two OS/shared-memory modules (MAHM monitoring, MACM control) touch
the real system and are covered by **optional** integration tests where practical.

---

## Tasks

- [ ] 1. Scaffold the plugin package, dependencies, and test harness
  - Create the plugin package layout under the spec project folder: an `afterburner/` package
    (protocol, safety, services, integration subpackages), a top-level `plugin.py` entry point
    placeholder, a `tests/` directory, and a `libs/` folder placeholder for the vendored `gassist_sdk`.
  - Add `requirements.txt` (runtime) and `requirements-dev.txt` (dev) with minimal deps only:
    runtime = standard library + vendored `gassist_sdk` (document that `mmap`/`ctypes` are stdlib);
    dev = `pytest`, `hypothesis`. Avoid heavyweight/optional deps (pydantic stays optional/unused).
  - Add `pytest.ini`/`pyproject.toml` test config (test discovery, Hypothesis profile with a
    minimum of 100 examples per property test) and a placeholder `tests/conftest.py`.
  - _Requirements: 17.1, 18.1_

- [ ] 2. Define strongly-typed domain models and the error taxonomy
  - [ ] 2.1 Implement enums and value objects
    - Create `afterburner/models.py` with `InterfaceStatus`, `ControlFeature`, `RiskLevel`,
      `ErrorCode`, `DiagnosisCause`, and the `Range` value object (`clamp`, `contains`).
    - _Requirements: 2.1, 5.5, 6.1, 13.1_
  - [ ] 2.2 Implement dataclass models
    - Add `GpuLimits`, `GpuCapabilities` (with `supports()`), `GpuTelemetry` (with `sampled_at`,
      `is_stale`), `TuningState`, `Profile`, `FanCurvePoint`, `FanCurve`, `ControlResult`,
      `Diagnosis`, and the `PluginError(Exception)` type (`code`, `user_message`, `detail`).
    - Add a `rangeFor(feature)` helper on `GpuLimits` used by the validator.
    - _Requirements: 3.1, 5.1, 6.5, 10.1, 12.2, 12.3, 13.1_
  - [ ]* 2.3 Write unit tests for models and `Range`
    - Test `Range.clamp`/`contains` at/inside/outside bounds and inverted/degenerate ranges;
      test `GpuCapabilities.supports` and `GpuLimits.rangeFor`.
    - _Requirements: 5.5, 5.6, 6.2_

- [ ] 3. Define the `AfterburnerInterface` Protocol and implement `FakeAfterburner`
  - [ ] 3.1 Declare the adapter Protocol (the mock boundary)
    - Create `afterburner/integration/interface.py` with a `runtime_checkable` `AfterburnerInterface`
      exposing exactly: `detect`, `get_version`, `read_telemetry`, `read_all_telemetry`,
      `read_capabilities`, `read_tuning_state`, `list_profiles`, `load_profile`, `reset_tuning`,
      `apply_control`, `apply_fan_curve`. Deliberately include **no** generic `write(offset, value)`
      primitive.
    - _Requirements: 14.1, 17.1_
  - [ ] 3.2 Implement `FakeAfterburner`
    - Create `afterburner/integration/fake.py` implementing `AfterburnerInterface` fully in memory:
      configurable detect status, telemetry (including missing/null fields and malformed records),
      capability sets/limits, profiles, and control results. Record every `apply_control`/
      `apply_fan_curve` call so tests can assert whether a write occurred.
    - Support simulating unavailability modes (not installed, not running, interface absent,
      disconnected mid-op) and a stalled MAHM update counter.
    - _Requirements: 17.2, 17.3, 7.4, 16.3_
  - [ ]* 3.3 Write structural test that no generic memory-write primitive exists — **Property 6**
    - **Property 6: No arbitrary memory access — only named validated controls**
    - Assert the `AfterburnerInterface` public surface contains only the enumerated named
      operations and no `write`/`poke`/offset/address-taking method; assert `FakeAfterburner`
      conforms via `isinstance` runtime check.
    - **Validates: Requirements 14.1, 14.2, 14.3**

- [ ] 4. Implement `HardwareCapabilityResolver` (capability detection & gating)
  - [ ] 4.1 Implement capability resolution
    - Create `afterburner/safety/capabilities.py` implementing `resolveCapabilities(gpu_index)`:
      derive supported controls from Afterburner-reported flags + min/max only (no hardcoded
      per-GPU assumptions); require functional MACM for write-capable features; expose read-only +
      `PROFILE_LOAD`/`PROFILE_RESET` when control is unavailable; treat inverted/missing ranges as
      unavailable; time-box resolution to 5s → read-only + error indication.
    - _Requirements: 5.1, 5.2, 5.3, 5.6_
  - [ ]* 4.2 Write unit tests for capability detection
    - Cover supported/unsupported/mixed capability sets, MACM-unavailable (read-only) degradation,
      inverted/missing ranges → unavailable, and resolution timeout → read-only.
    - _Requirements: 5.1, 5.2, 5.3, 5.6_
  - [ ]* 4.3 Write property test for capability gating — **Property 2**
    - **Property 2: Capability gating — unsupported features are never actuated**
    - Over randomized capability sets, assert that for every feature not in the supported set no
      adapter write is invoked and a typed `UNSUPPORTED_FEATURE` error is returned.
    - **Validates: Requirements 5.3, 6.4, 7.1, 8.5**

- [ ] 5. Implement `TuningValidator` (validation, clamping, fan-curve rules)
  - [ ] 5.1 Implement value validation & clamping
    - Create `afterburner/safety/validator.py` implementing `validateAndClamp(gpu_index, feature,
      requested_value)`: reject unsupported features (`UNSUPPORTED_FEATURE`), reject missing limits
      (`INTERFACE_UNAVAILABLE`), reject NaN/±inf (`INVALID_VALUE`), otherwise clamp to reported
      `[min,max]` and return `(safe_value, clamped)`.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 14.4, 14.5_
  - [ ] 5.2 Implement fan-curve validation
    - Implement `validateFanCurve(gpu_index, curve)`: require `FAN_CURVE` support; require 2–32
      points; enforce non-decreasing temperatures; clamp each point's temp/fan into reported ranges;
      raise typed `INVALID_VALUE`/`UNSUPPORTED_FEATURE` and leave prior config unchanged on failure.
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_
  - [ ]* 5.3 Write unit tests for tuning & safety boundaries
    - Clamp at/inside/outside min & max, reject NaN/inf, unsupported feature error, missing-range
      error, and report requested-vs-applied values.
    - _Requirements: 6.3, 6.4, 6.5, 6.6, 13.6, 13.7_
  - [ ]* 5.4 Write property test for clamping invariant — **Property 1**
    - **Property 1: Clamping invariant — no raw LLM value reaches hardware**
    - Generate arbitrary requested values (far below min, far above max, boundary) against
      randomized reported ranges; assert the value handed to the adapter is always `∈ [min,max]`.
    - **Validates: Requirements 6.1, 6.2, 6.5, 5.4, 7.1, 13.5, 14.4**
  - [ ]* 5.5 Write property test for fan-curve monotonicity & bounds — **Property 3**
    - **Property 3: Fan-curve monotonicity and bounds**
    - Generate random candidate curves; assert accepted curves are non-decreasing in temperature
      and every point is within reported bounds, and curves violating monotonicity/point-count are
      rejected.
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4**

- [ ] 6. Implement `SafetyPolicy` (risk classification & confirm-token lifecycle)
  - [ ] 6.1 Implement risk classification and confirm tokens
    - Create `afterburner/safety/policy.py`: classify each function as LOW/HIGH risk (reads,
      `reset_tuning`, `load_profile` = LOW; `set_*`, `optimize_*` = HIGH); issue single-use tokens
      with `CONFIRM_TOKEN_TTL_SECONDS = 300`; `validateToken` rejects missing/expired/reused tokens
      and marks valid tokens consumed.
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 11.6_
  - [ ]* 6.2 Write unit tests for the confirmation flow
    - Issue → validate → consume; reject reused token; reject expired token; reject missing token;
      LOW-risk ops bypass confirmation.
    - _Requirements: 9.1, 9.2, 9.3, 9.4_
  - [ ]* 6.3 Write property test for confirmation before high-risk ops — **Property 4**
    - **Property 4: Confirmation before high-risk operations**
    - Over randomized token lifecycles (issue/validate/expire/replay), assert a high-risk write
      occurs only with a currently-valid token and never on missing/expired/reused tokens.
    - **Validates: Requirements 9.1, 9.2, 9.3, 9.4, 11.4**

- [ ] 7. Implement `AfterburnerClient` facade over the integration layer
  - Create `afterburner/integration/client.py`: hold the injected `AfterburnerInterface`, orchestrate
    detect/read/profile/control calls, and translate raw adapter failures into typed `PluginError`s
    (graceful degradation; never raise unhandled). Reject Afterburner-dependent operations when no
    interface is injected.
  - _Requirements: 7.4, 7.5, 13.3, 13.4, 17.1, 17.4_

- [ ] 8. Implement `TelemetryService` (caching & staleness)
  - [ ] 8.1 Implement cache + rate limiting + staleness
    - Create `afterburner/services/telemetry.py` with `TELEMETRY_TTL_SECONDS`,
      `STALE_THRESHOLD_SECONDS`, `MIN_POLL_INTERVAL_SECONDS`; serve cache within TTL; enforce min
      poll interval; flag `is_stale` past threshold or when the MAHM update counter stalls; include
      telemetry age and ISO-8601 millisecond timestamps; parse well-formed and malformed MAHM
      records without fabricating values.
    - _Requirements: 3.1, 3.2, 3.4, 3.5, 3.6, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_
  - [ ]* 8.2 Write unit tests for telemetry parsing, caching & staleness
    - Well-formed and malformed MAHM records, missing-field → explicit unavailable (no fabricated
      values), multi-GPU indexing, cache hit within TTL, rate-limit enforcement, stale flag past
      threshold, and stalled-counter detection.
    - _Requirements: 3.2, 3.5, 3.6, 4.4, 4.5, 4.6, 4.7_
  - [ ]* 8.3 Write property test for caching & rate invariant — **Property 8**
    - **Property 8: Telemetry caching and rate invariant**
    - Issue bursts of reads with randomized timing; assert reads inside the staleness window are
      served from cache (bounded underlying poll count) and reads past the threshold are flagged
      `is_stale=True`.
    - **Validates: Requirements 4.1, 4.2, 4.3**

- [ ] 9. Implement `ProfileManager` (list / active / load / reset)
  - [ ] 9.1 Implement profile operations
    - Create `afterburner/services/profiles.py`: `get_profiles` (exactly one active; empty list when
      none), `load_profile` (validate id, reject unknown id leaving active unchanged), `reset_profile`;
      mark create/update best-effort or unavailable (never a false success).
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_
  - [ ]* 9.2 Write unit tests for profile handling
    - List with one active, empty list, load valid/invalid id, reset, and best-effort/unavailable
      classification for create/update.
    - _Requirements: 10.1, 10.2, 10.4, 10.7_

- [ ] 10. Implement `DiagnosticsService` (evidence-based classification)
  - [ ] 10.1 Implement diagnosis classification
    - Create `afterburner/services/diagnostics.py` implementing `diagnosePerformance(gpu_index)`:
      return `UNKNOWN_INSUFFICIENT_DATA` when telemetry is stale/missing essential fields; otherwise
      classify one of thermal/power/voltage/utilization-bottleneck/CPU-limited/app-behavior with
      non-empty evidence (field name + value) and a confidence strictly between 0.0 and 1.0.
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_
  - [ ]* 10.2 Write unit tests for diagnostics
    - Each limiter branch with representative telemetry; evidence contains field name + value;
      confidence within (0.0, 1.0); stale/missing → `UNKNOWN_INSUFFICIENT_DATA`.
    - _Requirements: 12.1, 12.2, 12.4, 12.5_
  - [ ]* 10.3 Write property test for diagnostics honesty — **Property 5**
    - **Property 5: Diagnostics never overclaim on stale or missing data**
    - Inject stale/missing/malformed telemetry; assert every inferred-cause diagnosis has
      `confidence < 1.0` and insufficient inputs always return `UNKNOWN_INSUFFICIENT_DATA`.
    - **Validates: Requirements 12.3, 12.4**

- [ ] 11. Implement optimization intents (`optimize_quiet`, `optimize_thermal`)
  - [ ] 11.1 Implement focused fan/thermal optimization
    - In `afterburner/services/optimize.py`: `optimize_quiet` (reduce fan setpoint ≥10 pts while
      keeping temp ≤83°C, else error) and `optimize_thermal` (target 40–95°C inclusive, reject
      out-of-range/non-numeric); restrict changes to fan/thermal controls only; route through
      validator + safety confirmation.
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_
  - [ ]* 11.2 Write unit tests for optimization intents
    - Quiet reduces fan while respecting temp limit (and refuses when impossible); thermal accepts
      in-range targets and rejects out-of-range/non-numeric; assert clock/voltage/power/memory are
      never modified.
    - _Requirements: 11.1, 11.2, 11.4, 11.5_

- [ ] 12. Implement the typed error → user-facing message mapping
  - [ ] 12.1 Implement the error mapper
    - Create `afterburner/errors.py` mapping every `ErrorCode` to a user-facing message (condition +
      affected component/value + next action) and to a Protocol V2 error code; unmatched conditions
      map to `COMM_FAILURE`; `LIMIT_VIOLATION` messages state both requested and clamped values;
      `DISCONNECTED` states nothing was changed.
    - _Requirements: 13.1, 13.2, 13.3, 13.5, 13.6, 13.7_
  - [ ]* 12.2 Write unit tests for the error taxonomy → message mapping
    - Every `ErrorCode` yields a non-empty actionable message and correct V2 code; unknown →
      `COMM_FAILURE`; limit-violation message includes requested + clamped values.
    - _Requirements: 13.1, 13.2, 13.3, 13.6, 13.7_

- [ ] 13. Checkpoint — Ensure all tests pass
  - Ensure all unit and property tests (Properties 1–6, 8) pass against `FakeAfterburner`. Ask the
    user if questions arise.

- [ ] 14. Implement the G-Assist Protocol V2 transport (`GAssistProtocol`)
  - [ ] 14.1 Implement framing and JSON-RPC transport
    - Create `afterburner/protocol/transport.py`: 4-byte big-endian length-prefixed UTF-8 JSON-RPC
      2.0 over stdin/stdout; reject frames > 10 MB and invalid UTF-8/JSON with a parse/invalid-request
      error without terminating the loop; delegate to `gassist_sdk` where available.
    - _Requirements: 1.1, 1.2_
  - [ ]* 14.2 Write unit tests for framing/transport
    - Round-trip encode/decode of framed messages; oversized frame rejected; malformed payload →
      error response with loop intact.
    - _Requirements: 1.1, 1.2_

- [ ] 15. Implement the plugin command layer and confirmation wiring (`GAssistPlugin`)
  - [ ] 15.1 Implement lifecycle and dispatch
    - Create `afterburner/protocol/plugin.py`: handle `initialize` (≤5s), `ping`→`pong` (≤1s),
      `execute` dispatch (≤30s, unknown function → error, retain state), `shutdown` (release
      resources, exit ≤5s); register all functions and dispatch to domain services; format
      NL-friendly responses via the error mapper.
    - _Requirements: 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 13.4_
  - [ ] 15.2 Wire the confirmation flow into risky commands
    - Implement `executeRisky` in the plugin: validate/clamp first, issue confirm token + set
      `keep_session` for HIGH-risk ops, honor `input` confirm/cancel and the 60s confirmation
      timeout, and apply only on a valid token/passthrough ack.
    - _Requirements: 1.10, 1.11, 9.1, 9.2, 9.3, 11.6_
  - [ ]* 15.3 Write unit tests for the plugin layer (with `FakeAfterburner`)
    - Initialize/ping/execute/shutdown behavior, unknown-function error, risky-op confirm/cancel/
      timeout, and read ops requiring no confirmation.
    - _Requirements: 1.5, 1.7, 1.10, 1.11, 9.4_
  - [ ]* 15.4 Write property test for graceful degradation — **Property 7**
    - **Property 7: Graceful degradation — no crash when Afterburner is unavailable**
    - Simulate each unavailability mode (not installed, not running, interface absent, disconnected
      mid-op) via `FakeAfterburner`; assert every call returns a typed `PluginError` with a
      user-facing message and never raises an unhandled exception.
    - **Validates: Requirements 2.6, 7.3, 7.4, 13.3, 16.2, 16.3**

- [ ] 16. Implement the plugin entry point and startup sequence
  - Implement `plugin.py`: construct the stack (inject the concrete `AfterburnerInterface`), run
    the startup sequence (exactly one capability detection + one telemetry read within 3s, degrade
    on timeout/absence), and run the protocol message loop.
  - _Requirements: 16.1, 16.2, 16.3, 16.4, 17.4_

- [ ] 17. Checkpoint — Ensure all tests pass
  - Ensure all unit and property tests (Properties 1–8) pass against `FakeAfterburner`. Ask the
    user if questions arise.

- [ ] 18. Implement the real MAHM monitoring client (official, OS/shared-memory)
  - [ ] 18.1 Implement `AfterburnerMonitoringClient`
    - Create `afterburner/integration/mahm.py` implementing the read-only parts of
      `AfterburnerInterface` against MAHM shared memory (via `mmap`/`ctypes`): detect/version, parse
      header + per-GPU entries into typed telemetry, limits, capabilities, and tuning state; never
      write to MAHM. Note in the module docstring this is the official interface and one of only two
      OS-touching modules.
    - _Requirements: 3.1, 3.3, 3.5, 2.1, 2.2, 2.3, 2.4, 2.7_
  - [ ]* 18.2 Write optional integration test for MAHM monitoring
    - Opt-in, skipped when Afterburner is absent: read-only smoke test + version/availability
      detection against a real installation.
    - _Requirements: 2.1, 3.1_

- [ ] 19. Implement the capability-gated MACM control client (undocumented/RE, OS/shared-memory)
  - [ ] 19.1 Implement `AfterburnerControlClient`
    - Create `afterburner/integration/macm.py` implementing the control parts of
      `AfterburnerInterface` against MACM shared memory: write only known control fields with
      already-validated/clamped values (no generic memory writer); report `INTERFACE_UNAVAILABLE`/
      `ACCESS_DENIED`/`DISCONNECTED` cleanly. Note in the module docstring this is the
      undocumented/reverse-engineered interface and the only privileged write path.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 13.5, 14.1_
  - [ ]* 19.2 Write optional integration test for MACM control
    - Opt-in behind an explicit flag, skipped when Afterburner/MACM absent: apply-and-read-back a
      single clamped control; assert graceful typed error when unavailable.
    - _Requirements: 7.1, 7.4_

- [ ] 20. Author `manifest.json` (Protocol V2, NL-friendly)
  - Create `manifest.json` with Protocol V2 required fields (name, version, entry point) and the full
    function set with NL-friendly `name`/`description`/`properties` for `get_gpu_status`,
    `get_gpu_limits`, `get_tuning_state`, `get_profiles`, `show_configuration`, `load_profile`,
    `reset_tuning`, `set_power_limit`, `set_core_offset`, `set_memory_offset`, `set_fan_percent`,
    `set_fan_curve`, `optimize_quiet`, `optimize_thermal`, `diagnose_performance` (control functions
    gated at runtime).
  - _Requirements: 1.12, 18.1, 18.4_

- [ ] 21. Implement the build/package script with deliverable verification
  - [ ] 21.1 Implement the packaging script
    - Create `build.py` (or `package.py`) that assembles the plugin into the Protocol V2 install
      layout (`manifest.json` + `plugin.py` + `afterburner/` + vendored `libs/gassist_sdk`), excludes
      any Afterburner/RTSS binaries, and **fails with a per-item error naming each missing
      deliverable** (build/package script, install instructions, ≥3 example prompts, troubleshooting
      docs, manifest, config).
    - _Requirements: 15.1, 18.1, 18.2, 18.3_
  - [ ]* 21.2 Write unit tests for the deliverable-verification logic
    - Assert the verifier fails and names each missing deliverable, and passes when all are present.
    - _Requirements: 18.2, 18.3_

- [ ] 22. Write documentation and example content
  - Create `README.md` with install instructions (install path
    `%PROGRAMDATA%\NVIDIA Corporation\nvtopps\rise\plugins\afterburner\`, vendored `libs/gassist_sdk`),
    at least 3 example G-Assist prompts, troubleshooting docs covering installation-failure and
    interface-unavailable scenarios, and an interface-status section listing each Afterburner
    interface as **official** (G-Assist V2, MAHM monitoring) or **undocumented/reverse-engineered**
    (MACM control), plus the no-redistribution statement.
  - Add a **Distribution & Submission** section documenting three distribution paths: (1) *local
    install (primary)* — drop the plugin folder into
    `%PROGRAMDATA%\NVIDIA Corporation\nvtopps\rise\plugins\afterburner\`
    (`manifest.json` + `plugin.py` + `afterburner/` + `libs/gassist_sdk`); G-Assist discovers it
    locally with no restart needed; (2) *community sharing via a self-hosted GitHub repo* — publish
    the plugin's own repo for users to clone/download and copy into the plugins folder, while
    contributing an example/docs into the official NVIDIA/G-Assist repo follows a fork → pull-request
    flow subject to NVIDIA review; (3) *official channels* — the NVIDIA G-Assist Plug-in Hackathon
    accepts submissions as a GitHub repo containing `plugin.py`, `requirements.txt`, `manifest.json`,
    `config.json` (if used), the plugin executable, and a README, and NVIDIA's rolling-out in-app
    plugin discovery/download as a curated channel. State the no-redistribution caveat: the repo MUST
    NOT bundle MSI Afterburner or RTSS (only MSI and Guru3D may redistribute them) — the README
    instructs users to install Afterburner themselves and the plugin detects it at runtime.
  - _Requirements: 15.1, 18.2, 18.4_

- [ ] 23. Final checkpoint — Ensure all tests pass and the package builds
  - Run the full test suite (Properties 1–8 + all unit tests) against `FakeAfterburner` and run the
    build script's deliverable verification. Ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core
  implementation tasks are never optional.
- The two optional integration tests (18.2, 19.2) require a real user-installed Afterburner and are
  skipped when it is absent — they are the only tasks needing hardware.
- Each task references specific requirement sub-clauses for traceability.
- Property-based tests (P1–P8, minimum 100 Hypothesis examples each) validate universal correctness
  properties; unit tests cover specific examples and edge cases.
- `afterburner/integration/mahm.py` (MAHM, official) and `afterburner/integration/macm.py` (MACM,
  undocumented/RE) are the only modules that touch OS shared memory; everything else is unit-tested
  through the `AfterburnerInterface` mock boundary with `FakeAfterburner`.

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
    { "id": 7, "tasks": ["5.3", "5.4", "5.5", "6.2", "6.3", "8.2", "8.3", "9.2", "12.2", "7"] },
    { "id": 8, "tasks": ["10.1"] },
    { "id": 9, "tasks": ["10.2", "10.3", "11.1"] },
    { "id": 10, "tasks": ["11.2", "14.1"] },
    { "id": 11, "tasks": ["14.2", "15.1"] },
    { "id": 12, "tasks": ["15.2"] },
    { "id": 13, "tasks": ["15.3", "15.4", "16"] },
    { "id": 14, "tasks": ["18.1", "19.1", "20", "21.1"] },
    { "id": 15, "tasks": ["18.2", "19.2", "21.2", "22"] }
  ]
}
```
