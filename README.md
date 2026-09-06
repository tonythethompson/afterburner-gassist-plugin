# Afterburner G-Assist Plugin

A production-quality **NVIDIA G-Assist** plugin that lets users monitor and control a
**user-installed** copy of **MSI Afterburner** through natural language — e.g. *"What's my GPU
temperature?"*, *"Make my GPU quieter"*, or *"Why is my clock dropping?"*

The plugin is a safe, strongly-typed bridge between G-Assist and MSI Afterburner. It does
**not** give the language model unrestricted access to processes, memory, or hardware controls:
every tuning value is validated and **clamped** to Afterburner-reported limits, every control is
gated by runtime capability detection, and risky changes require **explicit user confirmation**.

> **Status:** implemented and spec-driven. The full specification lives under
> [`.kiro/specs/afterburner-gassist-plugin/`](.kiro/specs/afterburner-gassist-plugin/) —
> [design.md](.kiro/specs/afterburner-gassist-plugin/design.md) (feasibility + architecture),
> [requirements.md](.kiro/specs/afterburner-gassist-plugin/requirements.md) (19 requirements),
> and [tasks.md](.kiro/specs/afterburner-gassist-plugin/tasks.md) (the implementation plan,
> tracked to completion). Runtime is **Python + the official `gassist_sdk`**, speaking
> **G-Assist Plugin Protocol V2** (JSON-RPC 2.0 over stdin/stdout).

---

## What this plugin does

- **Monitoring** — GPU temperature, utilization, core/memory clocks, voltage, power, power-limit
  %, fan speed, memory usage, GPU name, and driver info (via Afterburner's MAHM shared memory).
- **Controls (capability-gated; risky ones confirmation-gated)** — power limit, core/memory
  clock offsets, fan percentage, fan curves, profile load, and tuning reset (via Afterburner's
  MACM control interface).
- **Higher-level intents** — "make my GPU quieter" / "keep my GPU below 70C" map to focused
  fan/thermal optimization, not scattered tuning changes.
- **Diagnostics** — evidence-based reasoning about what is limiting performance (thermal / power /
  voltage / utilization bottleneck / CPU-limited / app behavior / insufficient data), with honest
  confidence and no false certainty.
- **Ownership awareness** — the plugin reports which tuning Afterburner is currently applying and
  is honest that external tuning tools cannot be observed through Afterburner (see
  [Tuning ownership](#tuning-ownership-whats-actually-controlling-your-gpu)).

## Design principles

- **Reliability, safety, maintainability, and compatibility over maximizing exposed controls.**
- **All tuning values are validated and clamped** to Afterburner-reported limits — the model never
  supplies raw values directly to the underlying interface, and there is no generic memory-write
  primitive at all.
- **Capability detection** — controls are only offered when the hardware and installed Afterburner
  version actually support them.
- **Graceful degradation** — if Afterburner or its control interface is unavailable, monitoring and
  diagnostics still work and the plugin returns clear "unavailable" messages instead of crashing.
- **Never a generic memory writer** — every write targets a single named, validated control field.

## Architecture (summary)

```
G-Assist
  |
G-Assist Plugin (Protocol V2, JSON-RPC 2.0)      manifest.json + plugin.py + afterburner/
  |                                                       + libs/gassist_sdk
Intent / command validation (TuningValidator, SafetyPolicy, HardwareCapabilityResolver)
  |
Afterburner adapter (AfterburnerInterface -- the mock boundary)
  |
MSI Afterburner  (MAHM monitoring, MACM control)
  |
GPU
```

## Requirements to run

- Windows PC with an NVIDIA RTX GPU
- NVIDIA App with Project G-Assist enabled
- MSI Afterburner installed **and running** (download only from
  [msi.com](https://www.msi.com) or [guru3d.com](https://www.guru3d.com)) — the plugin never
  bundles or redistributes Afterburner or RTSS

---

## Installation

### 1. Build the plugin folder

From a checkout of this repository:

```text
python build.py --check          # verify every deliverable is present (exit 0 = ready)
python build.py --sdk <path>     # assemble dist/afterburner/, vendoring gassist_sdk from <path>
```

`build.py` assembles the plugin into the Protocol V2 install layout —
`manifest.json` + `plugin.py` + `afterburner/` + `libs/gassist_sdk` — and refuses to build until
every deliverable exists (manifest, executable, package, vendored SDK, install instructions,
≥3 example prompts, troubleshooting docs), naming each missing item. It also refuses to package
any Afterburner/RTSS or other binary payload. The official NVIDIA G-Assist plugin SDK is **not
committed** to this repository (see `libs/README.txt`); vendor it at build time with `--sdk` or by
placing it at `libs/gassist_sdk/`.

### 2. Install the plugin folder

Copy the built plugin folder into the Protocol V2 plugins directory:

```text
%PROGRAMDATA%\NVIDIA Corporation\nvtopps\rise\plugins\afterburner\
```

G-Assist discovers a locally dropped plugin automatically — **no restart needed**. The plugin
detects your installed copy of Afterburner at runtime; nothing is installed system-wide.

---

## Example prompts

Try these in G-Assist once the plugin is installed and Afterburner is running:

- "What's my GPU temperature and fan speed right now?"
- "What is in each Afterburner profile?"
- "Call profile 1 quiet, then load quiet."
- "Make my GPU quieter."
- "Keep my GPU below 70 degrees."
- "What's controlling my GPU overclock right now?"
- "Why is my GPU clock dropping during my game?"
- "Raise my power limit to 90 percent."

The full 17-function surface behind these prompts: monitoring reads (`get_gpu_status`,
`get_gpu_limits`, `get_tuning_state`, `get_profiles`, `show_configuration`,
`get_tuning_ownership`), profile control (`load_profile`, `reset_tuning`,
`set_profile_nickname`), capability-gated
tuning writes that require confirmation when risky (`set_power_limit`, `set_core_offset`,
`set_memory_offset`, `set_fan_percent`, `set_fan_curve`, `optimize_quiet`, `optimize_thermal`),
and evidence-based diagnostics (`diagnose_performance`).

---

## Afterburner interfaces used (and their status)

| Interface | Purpose | Status |
|---|---|---|
| G-Assist Plugin SDK / Protocol V2 | Plugin transport, lifecycle & method contract | **Official / documented** (NVIDIA) |
| MAHM shared memory (`MAHMSharedMemory`) | Read-only GPU telemetry & limits | **Official** — shipped header `SDK\Include\MAHMSharedMemory.h` (interface v2.0) |
| MACM shared memory (`MACMSharedMemory`) | GPU control (power / clock / fan / reset) | **Official SDK header** — shipped `SDK\Include\MACMSharedMemory.h` plus the `MACMSharedMemorySample`; runtime signature/version re-verified against the installed header **per release** (the live map may carry a later v2.x revision, e.g. `0x00020003` on Afterburner 4.6.7) |
| Community MAHM wrappers (aleab/MSIAfterburnerNET, dejectedarcher/MSIAB_MAHM_CS) | Third-party, mirror the MAHM layout | **Community reference only** — the shipped header is authoritative |

Control is isolated, capability-gated, and confirmation-protected, and degrades gracefully: when
the SDK/interface is absent, control reports a clear "unavailable" response while monitoring and
diagnostics keep working. All Afterburner interface interactions are read/written only through
the two official shared-memory interfaces above — never through the Afterburner GUI, its config
files, or arbitrary memory.

## Tuning ownership — what's actually controlling your GPU?

The GPU exposes **one shared set of driver tuning parameters**. MSI Afterburner (including its
apply-at-Windows-startup profile auto-apply), NVIDIA App Automatic Tuning, G-Assist's own native
tuning, and other OC utilities are all writers of that same state — their values **do not stack**;
the last writer wins and can override or be overridden by another tool at any time.

This plugin treats **Afterburner as its single tuning authority**: it reads and writes only through
Afterburner, and it is honest about ownership —

- it reports what Afterburner is currently applying (read back live from the control interface),
  which profile is active, and whether `[Startup]` auto-apply is enabled;
- it marks external authorities (NVIDIA App Automatic Tuning, G-Assist native tuning, other OC
  tools) as **not observable** through Afterburner rather than guessing at their state;
- every risky confirmation names the currently applied value it will replace and warns that other
  tools may override or be overridden;
- if Afterburner's applied state already equals what you asked for, it says so and changes nothing.

Ask *"What's controlling my GPU overclock right now?"* to see the ownership report.

---

## Distribution & submission

1. **Local install (primary).** Drop the built plugin folder into
   `%PROGRAMDATA%\NVIDIA Corporation\nvtopps\rise\plugins\afterburner\`
   (`manifest.json` + `plugin.py` + `afterburner/` + `libs/gassist_sdk`). G-Assist discovers it
   locally with no restart needed.
2. **Community sharing via a self-hosted GitHub repo.** Publish the plugin's own repository; users
   clone/download it and copy it into the plugins folder. Contributing an example or documentation
   into the official [NVIDIA/G-Assist](https://github.com/NVIDIA/G-Assist) repository is a
   fork → pull-request flow subject to NVIDIA review.
3. **Official channels.** The NVIDIA G-Assist Plug-in Hackathon accepts submissions as a GitHub
   repository containing `plugin.py`, `requirements.txt`, `manifest.json`, `config.json` (if
   used), the plugin executable, and a README. NVIDIA is also rolling out in-app plugin
   discovery/download as a curated channel.

> **No-redistribution caveat:** only **MSI** and **Guru3D** may redistribute MSI Afterburner and
> RivaTuner Statistics Server (RTSS). This repository and any built plugin MUST NOT bundle
> Afterburner or RTSS — users install Afterburner themselves and the plugin detects it at runtime
> (`build.py` refuses to package such binaries).

---

## Troubleshooting

**The plugin is not discovered by G-Assist (installation failure).**

- Confirm the folder is at exactly `%PROGRAMDATA%\NVIDIA Corporation\nvtopps\rise\plugins\afterburner\`
  and contains `manifest.json` next to `plugin.py` (not nested in a subfolder).
- Confirm the vendored SDK is present under `libs/gassist_sdk\` — assemble with
  `python build.py --sdk <path>` rather than copying the repository as-is.
- Check G-Assist's plugin/log directory (`...\rise\logs\`, per-plugin `afterburner.log`) for
  startup errors. Protocol V2 runs the manifest's `executable` with the bundled
  `...\rise\python\python.exe` (`GA_PYTHON_DEV` overrides the interpreter for development).
- After changing the folder, ask G-Assist to re-scan or restart it; a locally dropped plugin needs
  no restart, but a malformed manifest may be silently rejected.

**The plugin reports that Afterburner or an interface is unavailable.**

- **"Afterburner doesn't appear to be installed."** — install MSI Afterburner from
  [msi.com](https://www.msi.com) or [guru3d.com](https://www.guru3d.com) and start it once so its
  shared-memory interfaces exist.
- **"Monitoring unavailable."** — Afterburner is not running, or its MAHM shared memory is not
  present (`0xDEAD`/missing map). Start Afterburner; monitoring resumes on the next read.
- **"That control isn't available on this hardware/version."** — the control is capability-gated:
  the GPU or this Afterburner version does not advertise it (common for voltage / fan-curve
  controls). This is expected, not an error.
- **Control keeps failing or times out.** — MACM control generally requires the Afterburner
  process running and often **elevated privileges**; if Afterburner is running as administrator,
  run G-Assist/NVIDIA App elevated too. Read-back verification is honest: if a value did not
  apply, the plugin says so rather than claiming success.
- **Stale values.** — telemetry older than 5 s is flagged stale; if Afterburner closed, reads
  report "unavailable" instead of serving old data.

**Development / verification.**

- Full test suite: `python -m pytest` (Properties 1–10 + unit tests, no GPU needed).
- Deliverable check: `python build.py --check` (exit 0 = all deliverables present).
- If Afterburner was updated, re-run `tools/generate_sdk_layout_fixtures.py --diff` and re-run the
  integration tests to re-verify the shared-memory layouts against the new install.
- Wire-contract gate (needs NVIDIA's `plugin_emulator`, e.g. a local `G-Assist` checkout of
  github.com/NVIDIA/G-Assist): `python tools/check_emulator.py` builds `dist/afterburner`, runs it
  through the official emulator (the engine-side Protocol V2 ground truth), executes every
  function, resolves risky prompts via `input`, and fails on reader parse errors or timeouts.
  The write-applying functions (`load_profile`/`reset_tuning`) run automatically when Afterburner
  is unreachable (every degraded CI run) and are skipped on a live host, where they would write;
  `--allow-writes` forces them everywhere.
- CI (`.github/workflows/ci.yml`) runs the full test suite, `build.py --check`, the `--diff`
  drift gate, and the plugin_emulator wire-contract gate on every push to `main` and every
  pull request.

---

## License

See [LICENSE](LICENSE). This project integrates with — but does not redistribute — MSI Afterburner.
