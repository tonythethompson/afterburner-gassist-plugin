# Requirements Document

## Introduction

This feature is a production-quality NVIDIA G-Assist plugin, written in Python on the
official `gassist_sdk` and speaking G-Assist Plugin Protocol V2, that lets users monitor and
control a **user-installed** copy of MSI Afterburner through natural language (for example,
"what's my GPU doing?", "make my GPU quieter", or "why is my clock dropping?").

Monitoring is built on Afterburner's officially shipped MAHM shared-memory interface (read-only
telemetry). Control is built on Afterburner's officially shipped MACM SDK interface — the install ships
`SDK\Include\MACMSharedMemory.h` and a runnable `MACMSharedMemorySample` (verified on Afterburner
4.6.7) — isolated behind the smallest possible adapter, gated by runtime capability detection,
and requiring explicit user confirmation for risky operations. When control is
unavailable, monitoring and diagnostics still function and the plugin returns clear, actionable
"unavailable" responses instead of failing.

The design — and therefore these requirements — prioritize **reliability, safety,
maintainability, and compatibility over maximizing the number of exposed controls**. The plugin
never bundles, redistributes, or installs Afterburner or RTSS; it integrates with the user's
existing installation and detects its presence at runtime.

These requirements are derived from the approved design document and are written to be concrete
and testable. Where a requirement maps to a design Correctness Property, the mapping is noted so
property references can be linked back.

## Glossary

- **Plugin**: The NVIDIA G-Assist Afterburner plugin defined by this specification (Python +
  `gassist_sdk`), comprising the protocol, safety, domain-service, and Afterburner-integration
  layers.
- **G-Assist**: NVIDIA's on-device assistant engine that maps natural-language user requests to
  plugin functions and displays plugin responses.
- **Protocol_V2**: G-Assist Plugin Protocol version 2 — JSON-RPC 2.0 messages over stdin/stdout
  with 4-byte big-endian length-prefix framing; the mandatory current protocol.
- **MAHM**: The officially shipped "MAHMSharedMemory" interface of MSI Afterburner, providing
  read-only GPU telemetry and Afterburner-reported limit/capability data. The
  `MAHMSharedMemory.h` header ships with Afterburner installs at
  `SDK\Include\MAHMSharedMemory.h` (the interface predates v4.6.0) and documents `szGpuId` (the
  `VEN_%04X&…&FN_%d` per-GPU profile filename encoding); the plugin validates its
  signature/version (v2.0) at runtime.
- **MACM**: The Afterburner control shared-memory interface, shipped officially in the
  Afterburner SDK folder (`SDK\Include\MACMSharedMemory.h` plus a runnable
  `SDK\Samples\SharedMemory\MACMSharedMemorySample`; verified on Afterburner 4.6.7; v2.0 header,
  `'MACM'` signature). Changelogs document the interface (4.6.0 cites the SDK sample; 4.6.6+
  announces a new control shared-memory interface for third-party software). Older releases may
  lack the SDK, so the plugin validates the shared-memory signature/version at runtime and
  re-verifies the layout against the installed header per release.
- **RTSS**: RivaTuner Statistics Server, which ships alongside Afterburner (OSD / framerate);
  out of scope for this plugin.
- **Telemetry**: Live GPU readings (temperature, hotspot, utilization, clocks, voltage, power,
  power-limit %, fan %, fan RPM, memory usage, GPU name, driver info) read from MAHM.
- **Tuning_Offset**: A user-adjustable delta applied to a GPU clock (core or memory), expressed
  in MHz relative to the stock clock.
- **Power_Limit**: The GPU power budget expressed as a percentage, within an Afterburner-reported
  supported range.
- **Fan_Curve**: An ordered set of temperature/fan-percent points defining fan speed as a
  function of temperature.
- **Profile**: A stored Afterburner tuning configuration. Current Afterburner versions provide
  five hardware profile slots (1..5) per GPU, stored read-only in the installation's `Profiles`
  directory (see the design document's profile storage layout); no user-named profiles exist
  today. A profile can be listed, identified as active, loaded, or reset.
- **Capability_Detection**: Runtime determination of which controls are actually supported,
  based on hardware capability flags, Afterburner-reported limits, MACM availability, and version.
- **Clamping**: Constraining a requested control value to the Afterburner-reported minimum /
  maximum before any write, so no out-of-range value reaches hardware.
- **Staleness**: The condition where telemetry is older than a defined threshold or its
  underlying MAHM counter has stopped advancing, indicating readings may no longer be current.
- **AfterburnerInterface**: The abstract adapter contract (Python `Protocol`) that isolates the
  domain stack from MAHM/MACM and serves as the unit-test mock boundary.
- **Confirm_Token**: A short-lived, single-use token issued for a high-risk operation that must
  be presented back before the operation is applied.
- **Tuning_Authority**: A component that writes the GPU's single shared set of driver
  tuning parameters. The GPU exposes one tuning state; multiple tools (Afterburner, NVIDIA App
  Automatic Tuning, G-Assist native tuning, other OC utilities) write or replace that same state
  through different driver interfaces — their offsets do not necessarily stack, and one tool may
  not recognize offsets written by another.
- **External_Tuning_Authorities**: NVIDIA App Automatic Tuning, G-Assist native tuning, and other
  OC utilities. None are observable through Afterburner's MAHM/MACM interfaces, so the plugin
  always reports them as `unknown_not_observable` and never asserts their state.

## Requirements

### Requirement 1: G-Assist Protocol V2 Integration

**User Story:** As a G-Assist user, I want the plugin to communicate reliably with the G-Assist engine, so that my natural-language requests are dispatched, executed, and answered within expected time limits.

#### Acceptance Criteria

1. THE Plugin SHALL exchange messages with G-Assist using JSON-RPC 2.0 messages encoded as UTF-8 JSON with a 4-byte big-endian unsigned integer length-prefix header indicating the byte length of the following message payload, transmitted over stdin/stdout.
2. IF an incoming message has a length-prefix header exceeding 10,485,760 bytes (10 MB) or contains a payload that is not valid UTF-8 JSON-RPC 2.0, THEN THE Plugin SHALL discard the message and return a JSON-RPC error response indicating a parse or invalid-request error without terminating its message loop.
3. WHEN G-Assist sends an `initialize` request, THE Plugin SHALL complete initialization and return a successful `initialize` response within 5 seconds.
4. IF initialization fails, THEN THE Plugin SHALL return a JSON-RPC error response indicating the initialization failure and SHALL NOT enter its message-processing loop.
5. WHEN G-Assist sends a `ping` request, THE Plugin SHALL return a `pong` response within 1 second (1000 milliseconds).
6. WHEN G-Assist sends an `execute` request for a registered function, THE Plugin SHALL dispatch the request to the corresponding domain service and return a `complete` notification within 30 seconds (30,000 milliseconds).
7. IF an `execute` request references a function name that is not registered in `manifest.json`, THEN THE Plugin SHALL return a JSON-RPC error response indicating the function is unknown and SHALL retain its running state.
8. THE Plugin SHALL keep every `execute` handler responsive enough to produce a `complete` notification within the engine's 30-second (30,000 ms) execute timeout, streaming progress for long operations; IF the engine times the operation out (JSON-RPC error code `-2`, `Timeout`) before a `complete` arrives, THEN THE Plugin SHALL NOT have performed any partial write, SHALL treat the operation as failed, and SHALL retain its running state for subsequent requests.
9. WHEN G-Assist sends a `shutdown` request, THE Plugin SHALL release all acquired resources, terminate its message loop, and exit within 5 seconds.
10. WHILE a high-risk operation is awaiting confirmation, WHEN G-Assist sends a follow-up `input` message containing the user's confirmation, THE Plugin SHALL proceed with the operation if the confirmation is affirmative and SHALL cancel the operation and return a cancellation response if the confirmation is negative.
11. IF no `input` confirmation message is received within 60 seconds (60,000 milliseconds) for a high-risk operation awaiting confirmation, THEN THE Plugin SHALL cancel the operation and return a response indicating the confirmation timed out.
12. THE Plugin SHALL register its available functions through a `manifest.json` file in which each function entry includes a non-empty `name`, a non-empty `description`, and a `properties` field describing the function's parameters for natural-language mapping.

### Requirement 2: Afterburner Detection and Availability

**User Story:** As a user, I want the plugin to detect the state of my Afterburner installation, so that I receive an accurate status instead of confusing failures.

#### Acceptance Criteria

1. WHEN a monitoring or control operation is requested, THE Plugin SHALL complete detection of whether Afterburner is installed, running, of a supported version, and accessible within 5 seconds, and SHALL classify the result as exactly one of `ok`, `not_installed`, `not_running`, `unsupported_version`, `access_denied`, or `unavailable`.
2. IF Afterburner is not installed, THEN THE Plugin SHALL return a typed `NOT_INSTALLED` status accompanied by a non-empty user-facing message of at most 256 characters indicating that Afterburner was not found.
3. IF Afterburner is installed but not running, THEN THE Plugin SHALL return a typed `NOT_RUNNING` status accompanied by a non-empty user-facing message of at most 256 characters indicating that Afterburner is installed but not currently running.
4. IF the installed Afterburner version is below the minimum supported version or does not expose the required interface, THEN THE Plugin SHALL return a typed `UNSUPPORTED_VERSION` status accompanied by a non-empty user-facing message of at most 256 characters indicating that the installed version is not supported.
5. IF the interface requires elevated privileges that are not available, THEN THE Plugin SHALL return a typed `ACCESS_DENIED` status accompanied by a non-empty user-facing message of at most 256 characters indicating that access was denied due to insufficient privileges.
6. WHILE any detection outcome is being produced, THE Plugin SHALL return a typed result and SHALL NOT raise an unhandled exception. (Validates design Property 7)
7. IF detection does not complete within 5 seconds or the installation state cannot be determined, THEN THE Plugin SHALL return a typed `UNAVAILABLE` status accompanied by a non-empty user-facing message of at most 256 characters indicating that Afterburner availability could not be determined.

### Requirement 3: GPU Monitoring via MAHM

**User Story:** As a user, I want to ask for my GPU's current status in natural language, so that I can see temperature, clocks, utilization, power, fan speed, and memory usage.

#### Acceptance Criteria

1. WHEN `get_gpu_status` is executed and MAHM telemetry is available, THE Plugin SHALL return a single response containing the following metrics with their units: GPU temperature (°C), hotspot temperature (°C), utilization (0–100%), core clock (MHz), memory clock (MHz), voltage (mV), power draw (W), power-limit percentage (0–100%), fan percentage (0–100%), fan RPM (revolutions per minute), memory used (MB), memory total (MB), GPU name (text), and driver version (text). Hot-spot temperature is returned with an explicit unavailable indicator wherever Afterburner/MAHM does not expose it (Afterburner does not expose hot-spot temperature for NVIDIA GPUs).
2. WHERE a specific metric is not provided by the installed hardware or Afterburner, THE Plugin SHALL report that metric with an explicit unavailable indicator and SHALL NOT substitute a default, zero, or fabricated numeric value.
3. THE Plugin SHALL read telemetry only from the MAHM interface and SHALL NOT perform any write operation to the MAHM interface.
4. WHEN a telemetry read is returned, THE Plugin SHALL include the sample timestamp expressed in ISO 8601 format with millisecond precision.
5. WHEN `read_all_telemetry` is executed on a multi-GPU system, THE Plugin SHALL return a separate telemetry set for each detected GPU, where each set is identified by a zero-based GPU index that is unique across the response.
6. IF `get_gpu_status` or `read_all_telemetry` is executed and MAHM telemetry is unavailable, THEN THE Plugin SHALL return an error response indicating that MAHM telemetry could not be read and SHALL NOT return partial or fabricated metric values.
7. WHEN `get_gpu_status` is executed and MAHM telemetry is available, THE Plugin SHALL return the response within 2 seconds of receiving the request.

### Requirement 4: Telemetry Caching and Staleness

**User Story:** As a user, I want telemetry to be current and rate-limited, so that I get accurate readings without the plugin polling Afterburner excessively.

#### Acceptance Criteria

1. WHEN a telemetry read is requested and the elapsed time since the timestamp of the most recent successful MAHM read is less than or equal to TELEMETRY_TTL_SECONDS (2 seconds), THE Plugin SHALL return the cached telemetry values without issuing a new MAHM read. (Validates design Property 8)
2. WHEN a telemetry read is requested and the elapsed time since the timestamp of the most recent successful MAHM read is greater than TELEMETRY_TTL_SECONDS (2 seconds), THE Plugin SHALL issue a new MAHM read and update the cached telemetry values and their timestamp. (Validates design Property 8)
3. THE Plugin SHALL enforce a minimum interval of MIN_POLL_INTERVAL_SECONDS (1 second) between consecutive MAHM reads, such that no two MAHM reads occur less than MIN_POLL_INTERVAL_SECONDS apart. (Validates design Property 8)
4. IF a telemetry read is requested less than MIN_POLL_INTERVAL_SECONDS (1 second) after the most recent MAHM read, THEN THE Plugin SHALL serve the cached telemetry values and SHALL NOT issue a new MAHM read. (Validates design Property 8)
5. IF the elapsed time since the timestamp of the most recent successful MAHM read exceeds STALE_THRESHOLD_SECONDS (5 seconds), THEN THE Plugin SHALL flag the telemetry as stale. (Validates design Property 8)
6. IF the MAHM shared-memory update counter has not increased across a span of at least STALE_THRESHOLD_SECONDS (5 seconds), THEN THE Plugin SHALL flag the telemetry as stale. (Validates design Property 8)
7. WHEN telemetry is flagged as stale, THE Plugin SHALL return the telemetry to G-Assist accompanied by an explicit stale indicator identifying the values as not current, and SHALL include the age of the telemetry in seconds.

### Requirement 5: Capability Detection

**User Story:** As a user, I want the plugin to only offer controls that my hardware and Afterburner actually support, so that I am not offered actions that cannot work.

#### Acceptance Criteria

1. WHEN resolving capabilities for a GPU, THE Plugin SHALL derive the supported control set from Afterburner-reported capability flags and reported minimum/maximum limits, and SHALL NOT use hardcoded per-GPU assumptions to add or remove controls from that set.
2. IF resolving capabilities for a GPU does not complete within 5 seconds, THEN THE Plugin SHALL treat that GPU as having no resolved capabilities, SHALL expose read-only capabilities only, and SHALL indicate the resolution failure to the user via an error indication.
3. IF the MACM control interface is not functional for a GPU, THEN THE Plugin SHALL expose read-only capabilities for that GPU, SHALL mark every write-capable control feature as unavailable, and SHALL NOT execute any write-capable control for that GPU.
4. WHEN a control feature is not present in the resolved supported set, THE Plugin SHALL NOT execute that control feature and SHALL NOT report it as available in any capability or configuration response; IF it is still invoked (e.g., because `manifest.json` is static at registration while capability is resolved at runtime), THEN THE Plugin SHALL return a typed `UNSUPPORTED_FEATURE` or `INTERFACE_UNAVAILABLE` error without performing any write, and SHALL phrase NL-friendly function descriptions to indicate that control availability varies with detected hardware and Afterburner state. (Validates design Property 2)
5. WHEN Afterburner reports a minimum value and a maximum value for a feature, THE Plugin SHALL use that reported minimum as the sole lower bound and that reported maximum as the sole upper bound for that feature, and SHALL clamp any finite requested value outside the inclusive range [reported minimum, reported maximum] to the nearest bound before applying it, returning a success result that reports both the requested and the applied value (see Requirement 13, criteria 6 and 7); an `INVALID_VALUE` error with no write applies only to values that cannot be interpreted as a finite number.
6. IF Afterburner reports a range for a feature in which the minimum value is greater than the maximum value, or either bound is missing, THEN THE Plugin SHALL treat that feature as unavailable and SHALL NOT offer or execute that feature.

### Requirement 6: Tuning Validation and Clamping

**User Story:** As a user, I want every tuning change to be validated and constrained to safe limits, so that no unsafe or invalid value ever reaches my GPU.

#### Acceptance Criteria

1. WHEN a control value is requested, THE Plugin SHALL treat the requested value as untrusted and SHALL clamp it to the closed interval [minimum, maximum] reported by the AfterburnerInterface for that feature before performing any write, such that the written value satisfies minimum ≤ written value ≤ maximum. (Validates design Property 1)
2. THE Plugin SHALL ensure that the value handed to the AfterburnerInterface for any control write is greater than or equal to the reported minimum and less than or equal to the reported maximum for that feature. (Validates design Property 1)
3. IF a requested control value is not a finite number (that is, it is NaN, positive infinity, or negative infinity), THEN THE Plugin SHALL return a typed `INVALID_VALUE` error, SHALL NOT perform any write, and SHALL leave the current control value unchanged.
4. IF a requested feature is not supported for the target GPU, THEN THE Plugin SHALL return a typed `UNSUPPORTED_FEATURE` error, SHALL NOT perform any write, and SHALL leave all control values unchanged. (Validates design Property 2)
5. WHEN a requested value is clamped, THE Plugin SHALL return the clamped result and the originally requested value so that G-Assist can inform the user of the applied value.
6. WHEN a requested value is within the reported [minimum, maximum] range and requires no clamping, THE Plugin SHALL write the requested value unchanged and SHALL report the applied value as equal to the requested value.

### Requirement 7: Control Operations via MACM

**User Story:** As a user, I want to adjust power limit, clock offsets, and fan speed through natural language, so that I can tune my GPU when my setup supports it.

#### Acceptance Criteria

1. WHERE the target feature is supported AND the MACM interface status is `ok`, WHEN a control operation is requested, THE Plugin SHALL apply `set_power_limit`, `set_core_offset`, `set_memory_offset`, or `set_fan_percent` using the requested value clamped to the device-reported minimum and maximum for that control, and SHALL return a success result reporting the applied value. (Validates design Property 2)
2. IF a requested control value is non-numeric, missing, or outside the device-reported valid range for the target control, THEN THE Plugin SHALL clamp the value to the nearest range boundary before applying it, or SHALL return a typed `INVALID_VALUE` error with a user-facing message and perform no write when the value cannot be interpreted as a number. (Validates design Property 2)
3. WHEN `reset_tuning` is executed AND the MACM interface status is `ok`, THE Plugin SHALL set power limit, core clock offset, memory clock offset, and fan control to the device default (stock) settings and SHALL return a success result within 5 seconds.
4. IF the control interface is unavailable when a control operation is requested, THEN THE Plugin SHALL return a typed `INTERFACE_UNAVAILABLE` error with a user-facing message indicating the control interface is unavailable, and SHALL NOT perform any write to GPU settings. (Validates design Property 7)
5. WHILE the control interface is unavailable, THE Plugin SHALL continue to return monitoring and diagnostics data without error. (Validates design Property 7)

### Requirement 8: Fan Curve Support

**User Story:** As a user, I want to set a custom fan curve as temperature/fan-percent points, so that I can control how my fans respond to temperature.

#### Acceptance Criteria

1. WHEN a fan curve is submitted, THE Plugin SHALL accept it only if it contains at least 2 points and at most the Afterburner-reported maximum number of fan-curve points for the target GPU (`fan_curve_max_points`), which SHALL default to 2 under Afterburner's documented two-point software fan-curve model and SHALL never exceed 32.
2. WHEN a fan curve is validated, THE Plugin SHALL require the temperature values to be non-decreasing across consecutive points in the given order (each point's temperature greater than or equal to the previous point's temperature). (Validates design Property 3)
3. WHEN a fan curve is validated, THE Plugin SHALL clamp each point's temperature to the inclusive Afterburner-reported temperature range and each point's fan percentage to the inclusive Afterburner-reported fan range, replacing any value below the minimum with the minimum and any value above the maximum with the maximum. (Validates design Property 3)
4. IF a submitted fan curve contains fewer than 2 points, contains more points than the Afterburner-reported `fan_curve_max_points` (default 2, maximum 32), or has any point whose temperature is less than the preceding point's temperature, THEN THE Plugin SHALL return a typed `INVALID_VALUE` error indicating the specific validation failure, SHALL NOT apply the curve, and SHALL leave the previously active fan configuration unchanged. (Validates design Property 3)
5. IF fan curve control is not supported for the target GPU, THEN THE Plugin SHALL return a typed `UNSUPPORTED_FEATURE` error indicating the feature is unavailable for the target GPU, SHALL NOT apply the curve, and SHALL leave the previously active fan configuration unchanged. (Validates design Property 2)

### Requirement 9: Confirmation for Risky Operations

**User Story:** As a user, I want risky tuning changes to require my explicit confirmation, so that no potentially harmful change is applied without my consent.

#### Acceptance Criteria

1. WHEN a high-risk operation (`set_power_limit`, `set_core_offset`, `set_memory_offset`, `set_fan_percent`, `set_fan_curve`, or an `optimize_*` intent) is requested without a valid confirmation token or explicit passthrough confirmation, THE Plugin SHALL generate a single-use confirmation token with a time-to-live of CONFIRM_TOKEN_TTL_SECONDS (300 seconds), SHALL return a successful `complete` result whose payload marks the operation as pending confirmation (`needs_confirmation`) and carries the token and a human-readable prompt, and SHALL NOT apply the requested change; a pending confirmation is a success result, not an error. (Validates design Property 4)
2. WHEN a high-risk operation is presented with a confirmation token that is unused and whose age is less than or equal to CONFIRM_TOKEN_TTL_SECONDS (300 seconds), or with an explicit passthrough confirmation, THE Plugin SHALL apply the validated, clamped change and SHALL mark that confirmation token as used. (Validates design Property 4)
3. IF a high-risk operation is presented with a confirmation token that is absent, whose age exceeds CONFIRM_TOKEN_TTL_SECONDS (300 seconds), or that has already been marked as used, THEN THE Plugin SHALL return a typed `CONFIRMATION_REQUIRED` error, SHALL NOT perform the write, and SHALL leave the current tuning state unchanged. (Validates design Property 4)
4. WHEN a read operation, `reset_tuning`, or `load_profile` is requested, THE Plugin SHALL execute it without requiring a confirmation token.

### Requirement 10: Profile Management

**User Story:** As a user, I want to list, identify, load, and reset Afterburner profiles, so that I can switch between saved tuning configurations.

#### Acceptance Criteria

1. THE Plugin SHALL obtain Afterburner profile data by READ-ONLY access to the detected Afterburner installation's Profiles directory (permitted by Requirement 14.3), performed exclusively through the AfterburnerInterface abstraction; THE Plugin SHALL NOT write, create, or delete files or directories in Afterburner's Profiles directory or anywhere else outside its own configuration and log files.
2. WHEN `get_profiles` is executed, THE Plugin SHALL return the list of available Afterburner profiles read from Afterburner's Profiles directory (storage layout defined in the design document), where each entry includes the profile identifier (hardware profile slot 1..5) and a name, and SHALL mark as active the single profile whose stored settings match Afterburner's currently applied state as read back through the control interface (within tolerance); THE Plugin SHALL mark at most one profile as active and SHALL NOT mark a profile active when the control interface is unavailable or the match is ambiguous.
3. IF `get_profiles` is executed and Afterburner's Profiles directory contains no profiles, THEN THE Plugin SHALL return an empty list with no profile marked active within 2 seconds.
4. WHEN `load_profile` is executed with a profile identifier that matches an existing profile AND the control interface is functional, THE Plugin SHALL read the stored profile (read-only) and apply its settings exclusively through the AfterburnerInterface's named, validated control operations, treating profile-derived values as untrusted (validated and clamped like LLM-supplied values) and ignoring with a notice any setting that has no corresponding named control, and SHALL return a success result within 5 seconds only when the applied settings are verified via Afterburner.
5. IF `load_profile` is executed with a profile identifier that does not match any existing profile, THEN THE Plugin SHALL reject the operation, return an error result indicating the identifier is invalid, and leave the currently active profile unchanged.
6. WHEN `reset_profile` is executed AND the control interface is functional, THE Plugin SHALL restore the active profile to its last-saved Afterburner state (re-applying its stored settings as in criterion 4) and SHALL return a success or error result within 5 seconds.
7. IF `load_profile` or `reset_profile` is executed while the control interface is unavailable, THEN THE Plugin SHALL return a typed `INTERFACE_UNAVAILABLE` error with a user-facing message, SHALL NOT apply any settings, and SHALL NOT modify Afterburner's Profiles directory.
8. IF a profile create or update operation, or any profile apply that cannot be completed with a verified state change confirmed via Afterburner, is requested, THEN THE Plugin SHALL return a result classifying the operation as best-effort or unavailable, and SHALL NOT return a success result or a fabricated profile list for that operation.
9. Every profile operation SHALL return either a success result or an error result on every invocation, and SHALL NOT raise an unhandled exception.

### Requirement 11: Higher-Level Optimization Intents

**User Story:** As a user, I want to say "make my GPU quieter" or "keep my GPU below 70C", so that the plugin applies focused fan/thermal optimization without unrelated changes.

#### Acceptance Criteria

1. WHEN `optimize_quiet` is executed, THE Plugin SHALL reduce the fan speed setpoint by at least 10 percentage points below the current setpoint and SHALL verify via telemetry readback that the GPU temperature remains at or below 83°C.
2. IF executing `optimize_quiet` would require the GPU temperature to exceed 83°C to achieve any fan noise reduction, THEN THE Plugin SHALL NOT reduce the fan speed and SHALL return an error indicating that noise reduction cannot be applied without violating the temperature limit.
3. WHEN `optimize_thermal` is executed with a target temperature between 40°C and 95°C inclusive, THE Plugin SHALL adjust fan and thermal controls toward keeping the GPU temperature at or below the specified target temperature, SHALL verify via telemetry readback within 30 seconds of execution whether the target is met, and SHALL return a success result with the measured temperature or an honest failure result (target not achievable within Afterburner/hardware capability); THE Plugin SHALL NOT report success without that verification.
4. IF `optimize_thermal` is executed with a target temperature outside the range of 40°C to 95°C inclusive, or with a missing or non-numeric target temperature, THEN THE Plugin SHALL reject the request and return an error indicating the valid target temperature range, and SHALL leave existing fan and thermal settings unchanged.
5. WHEN a higher-level optimization intent (`optimize_quiet` or `optimize_thermal`) is executed, THE Plugin SHALL restrict all applied changes to fan and thermal controls and SHALL NOT modify clock, voltage, power-limit, or memory tuning parameters.
6. WHEN a higher-level optimization intent applies a change classified as high-risk, THE Plugin SHALL require explicit user confirmation before applying the change and SHALL preserve the prior settings unchanged until confirmation is received. (Validates design Property 4)

### Requirement 12: Performance Diagnostics

**User Story:** As a user, I want the plugin to explain what is limiting my GPU performance, so that I understand why clocks are dropping without being misled.

#### Acceptance Criteria

1. WHEN `diagnose_performance` is executed and live telemetry is available with all essential fields present and captured no more than 5 seconds before execution, THE Plugin SHALL classify the likely limiter as exactly one of the following values: thermal, power, voltage, utilization-bottleneck, CPU-limited, application-behavior, or unknown-insufficient-data.
2. WHEN a diagnosis with a limiter other than `unknown-insufficient-data` is produced, THE Plugin SHALL include a non-empty list of at least one supporting-evidence item, where each item identifies the telemetry field name and its observed value.
3. WHEN a diagnosis is produced, THE Plugin SHALL include a confidence value expressed as a number in the range 0.0 to 1.0 inclusive.
4. IF the most recent telemetry sample was captured more than 5 seconds before execution, OR one or more essential telemetry fields are missing or null, THEN THE Plugin SHALL return a diagnosis with the limiter value `unknown-insufficient-data`. (Validates design Property 5)
5. WHEN a cause other than `unknown-insufficient-data` is inferred, THE Plugin SHALL report a confidence value strictly greater than 0.0 and strictly less than 1.0. (Validates design Property 5)

### Requirement 13: Error Handling and User-Facing Messages

**User Story:** As a user, I want clear, actionable messages when something goes wrong, so that I know what happened and what to do next.

#### Acceptance Criteria

1. WHEN an error condition occurs, THE Plugin SHALL map it to exactly one typed error code from the taxonomy: `NOT_INSTALLED`, `NOT_RUNNING`, `UNSUPPORTED_VERSION`, `UNSUPPORTED_GPU`, `INTERFACE_UNAVAILABLE`, `ACCESS_DENIED`, `INVALID_VALUE`, `COMM_FAILURE`, `STALE_TELEMETRY`, `UNSUPPORTED_FEATURE`, `DISCONNECTED`, or `CONFIRMATION_REQUIRED`.
2. WHEN a typed error is returned, THE Plugin SHALL include a user-facing message that states the condition, the affected component or value, and a recommended next action.
3. IF an error condition does not match any specific taxonomy code, THEN THE Plugin SHALL map it to `COMM_FAILURE` and include a user-facing message.
4. WHEN a typed error is returned to G-Assist, THE Plugin SHALL deliver it through exactly one `error` or `complete` notification and SHALL continue running without terminating the process. (Validates design Property 7)
5. IF Afterburner disconnects during a control operation, THEN THE Plugin SHALL return a typed `DISCONNECTED` error indicating that nothing was changed, and SHALL leave prior settings preserved.
6. WHEN a requested value exceeds the reported maximum limit, THE Plugin SHALL clamp it to that maximum, apply the clamped value, and SHALL return a success result whose message states both the requested value and the supported maximum value to which it was clamped; clamping SHALL NOT be reported as an error.
7. WHEN a requested value is below the reported minimum limit, THE Plugin SHALL clamp it to that minimum, apply the clamped value, and SHALL return a success result whose message states both the requested value and the supported minimum value to which it was clamped; clamping SHALL NOT be reported as an error.

### Requirement 14: Security Constraints

**User Story:** As a security-conscious user, I want the plugin to restrict itself to safe, named operations, so that it cannot perform dangerous or arbitrary actions on my system.

#### Acceptance Criteria

1. THE AfterburnerInterface SHALL expose only named, validated control operations drawn from a fixed, enumerable set defined at build time, and SHALL NOT expose any operation that accepts a caller-supplied memory address, offset, or register index as a write target. (Validates design Property 6)
2. IF any operation is invoked that would perform process injection, DLL injection, or a memory write to a target outside the AfterburnerInterface's named control operations, THEN THE Plugin SHALL reject the operation without performing the write and SHALL return an error indicating the operation is not permitted.
3. IF any operation is invoked that would execute a shell or system command, or that would read, write, create, or delete a filesystem path other than the plugin's own configuration files and log files, THEN THE Plugin SHALL reject the operation without performing it and SHALL return an error indicating the operation is not permitted; EXCEPT that THE Plugin SHALL be permitted to READ (and only read — never write, create, or delete) Afterburner's Profiles directory and the profile files within it, restricted to the Profiles directory of the detected Afterburner installation, for the purpose of enumerating and loading profiles per Requirement 10; the target path SHALL be derived from the detected Afterburner installation and SHALL NOT be supplied by the LLM or by function arguments.
4. WHEN a control value originating from the LLM boundary is received, THE Plugin SHALL apply validation and clamping to produce a sanitized value within the control's defined minimum and maximum bounds before invoking any AfterburnerInterface write operation, such that no unvalidated LLM-supplied value is passed to the interface. (Validates design Property 1)
5. IF a control value received from the LLM boundary is non-numeric, missing, or outside the control's defined minimum and maximum bounds, THEN THE Plugin SHALL clamp it to the nearest bound or reject it without invoking any write operation, and SHALL record the rejection or clamping in the plugin's log files. (Validates design Property 1)
6. IF any function argument or profile-file value could be interpreted as a filesystem path, profile location, or write target (e.g., a profile identifier containing path separators or traversal sequences, an absolute path, or a path-like value inside a profile file), THEN THE Plugin SHALL NOT derive any filesystem access from that value — the only directory ever accessed for profiles SHALL be the Profiles directory of the detected Afterburner installation — and SHALL reject the request or ignore the value with a typed error or notice. (Validates design Property 9)
7. WHEN any profile or control operation executes, including under adversarial function arguments or adversarial profile-file contents, THE Plugin SHALL open Afterburner profile files with read-only access only and SHALL NOT create, modify, or delete any file or directory under Afterburner's Profiles directory, and SHALL NOT request any write-capable handle (e.g., `GENERIC_WRITE` / `FILE_WRITE_DATA`) on that directory or its files. (Validates design Property 9)
8. THE Plugin SHALL be verifiable for the read-only guarantee by acceptance tests that run a representative set of adversarial function arguments and adversarial profile-file contents (malformed INI, path-like and traversal values in setting keys, oversized or duplicate sections, unexpected section names) against a sandboxed copy of a Profiles directory and assert that the directory tree — names, file sizes, modification timestamps, and content hashes — is unchanged after every operation and that no write-capable open was attempted. (Validates design Property 9)

### Requirement 15: Licensing and Redistribution Compliance

**User Story:** As a distributor of the plugin, I want to comply with Afterburner licensing, so that the plugin can be shared without redistributing restricted binaries.

#### Acceptance Criteria

1. THE Plugin SHALL NOT include MSI Afterburner or RTSS binaries, installers, or their component files within the plugin's distribution package.
2. WHEN the Plugin starts, THE Plugin SHALL detect the presence of an existing MSI Afterburner installation on the host system within 5 seconds.
3. IF an existing MSI Afterburner installation is not detected within 5 seconds of Plugin start, THEN THE Plugin SHALL display an error message indicating that MSI Afterburner is not installed and SHALL NOT attempt to download or install Afterburner or RTSS binaries.
4. WHEN an existing MSI Afterburner installation is detected, THE Plugin SHALL integrate with that installation without modifying, copying, or redistributing its binaries.

### Requirement 16: Graceful Startup and Degradation

**User Story:** As a user, I want the plugin to start quickly and behave gracefully when Afterburner is absent, so that it remains usable and informative in any state.

#### Acceptance Criteria

1. WHEN the Plugin starts, THE Plugin SHALL execute a startup sequence consisting of exactly one capability detection pass followed by exactly one telemetry read, and SHALL complete this sequence within STARTUP_BUDGET_SECONDS (3 seconds).
2. IF the startup sequence does not complete within STARTUP_BUDGET_SECONDS (3 seconds), THEN THE Plugin SHALL abort the pending capability detection or telemetry read, SHALL complete loading in a degraded state, and SHALL record a typed error with a user-facing message indicating that startup exceeded the time budget.
3. IF Afterburner is absent at startup, THEN THE Plugin SHALL complete loading successfully within STARTUP_BUDGET_SECONDS (3 seconds), and SHALL set all monitoring and diagnostics operations to return a typed "unavailable" response containing a user-facing message that identifies Afterburner as the missing dependency. (Validates design Property 7)
4. WHILE Afterburner is unavailable, IF a monitoring or diagnostics operation is invoked, THEN THE Plugin SHALL return a typed error containing a user-facing message identifying the affected operation and the unavailable state, SHALL retain any previously loaded configuration and last-known telemetry values without modification, and SHALL continue running without terminating the process. (Validates design Property 7)

### Requirement 17: Testability via Abstraction

**User Story:** As a developer, I want the Afterburner integration abstracted behind an interface, so that I can unit-test the entire stack without a real GPU or Afterburner.

#### Acceptance Criteria

1. THE Plugin SHALL perform all detect, read, profile, and control operations exclusively through the AfterburnerInterface abstraction and SHALL NOT reference any concrete MAHM/MACM implementation type outside the AfterburnerInterface implementations.
2. WHERE unit tests are run, THE Plugin SHALL support injecting a fake AfterburnerInterface implementation via a constructor or setter, such that no real GPU, Afterburner process, or hardware access is required.
3. WHILE a fake AfterburnerInterface implementation is injected, THE Plugin SHALL obtain all Afterburner-related data solely from that fake and SHALL NOT access the real system, network, or filesystem for Afterburner data.
4. IF no AfterburnerInterface implementation is provided, THEN THE Plugin SHALL reject Afterburner-dependent operations with an error indication and SHALL preserve its current state.

### Requirement 18: Packaging and Deliverables

**User Story:** As a user installing the plugin, I want a complete, documented package, so that I can install and use it and understand which interfaces it relies on.

#### Acceptance Criteria

1. THE Plugin SHALL provide a `manifest.json` file and a configuration file that conform to G-Assist Protocol V2 conventions, where conformance means the manifest contains all Protocol V2 required fields (plugin name, version, entry point, and function definitions) and the configuration file parses without errors.
2. WHEN the package is built, THE Plugin SHALL include all of the following deliverables: build/package scripts, installation instructions, at least 3 example G-Assist prompts, and troubleshooting documentation covering at least the installation-failure and interface-unavailable scenarios.
3. IF any required deliverable listed in criterion 2 is absent from the built package, THEN THE build/package script SHALL fail and produce an error message identifying each missing deliverable by name.
4. THE Plugin documentation SHALL list every Afterburner interface the Plugin uses and, for each interface, state its status using the three-level labels defined in the Glossary: "official" (G-Assist
  Protocol V2, MAHM monitoring), "community reference" (third-party MAHM wrappers), or
  "official SDK header" (MACM control interface — `SDK\Include\MACMSharedMemory.h`, re-verified
  per release against the installed header).
### Requirement 19: Tuning Ownership and Authority Conflicts

**User Story:** As a user who may run several tuning tools, I want the plugin to treat the GPU's
tuning state as one shared resource and be honest about which tool owns it, so that Afterburner
changes do not silently fight, double-apply, or appear to stack with another tool.

The GPU exposes a single set of driver tuning parameters. G-Assist native tuning, NVIDIA App
Automatic Tuning, other OC utilities, and MSI Afterburner (which can also auto-apply a profile at
Windows startup) are all writers that replace or modify that same state through different driver
interfaces; their values do not necessarily stack, and one tool may not even recognize offsets
written by another. Afterburner interfaces (MAHM/MACM) expose only Afterburner's own view of that
state — the other authorities are not observable through them.

#### Acceptance Criteria

1. THE Plugin SHALL model the GPU's applied tuning state as a single shared resource and SHALL NOT
   assume or claim that values it applies through Afterburner add to (stack with) values applied
   by any other tuning tool; an applied change replaces Afterburner's currently applied values
   only. (Validates design Property 10)
2. BEFORE the Plugin applies any tuning change (`set_*`, `optimize_*`, `load_profile`, or
   `reset_tuning`), THE Plugin SHALL determine, exclusively through Afterburner interfaces, what is
   observable about the current tuning state — whether Afterburner is running, whether the applied
   tuning state is readable, which profile (if any) matches the applied state, and whether
   startup auto-apply is configured (a populated `[Startup]` section) — and the response or confirmation SHALL state that
   the change replaces Afterburner's currently applied values and SHALL warn that external tuning
   authorities (NVIDIA App Automatic Tuning, G-Assist native tuning, other OC utilities) are not
   observable through Afterburner interfaces and may override or be overridden by the change,
   without ever asserting that such an authority is active or inactive. (Validates design
   Property 10)
3. THE Plugin SHALL expose a read-only `get_tuning_ownership` function that returns a typed report
   in which Afterburner's authority state is marked `observed` together with its applied tuning
   state, matched active profile (or none), and startup auto-apply presence (populated `[Startup]`), and each external
   authority (NVIDIA App Automatic Tuning, G-Assist native tuning, other OC utilities) is marked
   `unknown_not_observable`; the report SHALL explain the single shared tuning state and SHALL NOT
   fabricate knowledge about any external authority. (Validates design Property 10)
4. IF Afterburner's currently applied tuning state already equals the requested change within
   tolerance, THEN THE Plugin SHALL report success without issuing a redundant write. (Validates
   design Property 10)
5. THE Plugin SHALL NOT attempt to modify, disable, or claim to manage any external tuning
   authority (NVIDIA App Automatic Tuning, G-Assist native tuning, driver-level OC state), and
   SHALL NOT assert whether such an authority is active based on Afterburner data; where advice
   recommends that the user disable a competing auto-tuning feature, the Plugin SHALL present it
   as a user action and SHALL NOT perform it. (Validates design Property 10)
6. The Plugin's control-layer implementation SHALL NOT begin until a documented research milestone
   records the precedence and interaction between G-Assist native tuning, NVIDIA App Automatic
   Tuning, and MSI Afterburner (the driver interfaces each writes, stacking-vs-replace semantics,
   what is observable through Afterburner interfaces, and known cross-tool evidence), and the
   findings SHALL be re-validated against real hardware during integration testing before release.
