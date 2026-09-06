# Afterburner G-Assist Plugin

A production-quality **NVIDIA G-Assist** plugin that lets users monitor and control a
**user-installed** copy of **MSI Afterburner** through natural language — e.g. *"What's my GPU
temperature?"*, *"Make my GPU quieter"*, or *"Why is my clock dropping?"*

> **Status:** This repository currently contains the **specification** (design, requirements, and
> implementation plan). Implementation follows the task plan in
> [`.kiro/specs/afterburner-gassist-plugin/tasks.md`](.kiro/specs/afterburner-gassist-plugin/tasks.md).

---

## What this plugin does

The plugin acts as a safe, strongly-typed bridge between G-Assist and MSI Afterburner. It does
**not** give the language model unrestricted access to processes, memory, or hardware controls.

- **Monitoring** — GPU temperature, hotspot, utilization, core/memory clocks, voltage, power,
  power-limit %, fan %/RPM, memory usage, GPU name, and driver info.
- **Controls (capability-gated + confirmation for risky ones)** — power limit, core/memory clock
  offsets, fan percentage, fan curves, profile load, and tuning reset.
- **Higher-level intents** — "make my GPU quieter" / "keep my GPU below 70C" map to focused
  fan/thermal optimization, not scattered tuning changes.
- **Diagnostics** — evidence-based reasoning about what is limiting performance (thermal / power /
  voltage / utilization bottleneck / CPU-limited / app behavior / insufficient data), with honest
  confidence and no false certainty.

## Design principles

- **Reliability, safety, maintainability, and compatibility over maximizing exposed controls.**
- **All tuning values are validated and clamped** to Afterburner-reported limits — the model never
  supplies raw values directly to the underlying interface.
- **Capability detection** — controls are only offered when the hardware and installed Afterburner
  version actually support them.
- **Graceful degradation** — if Afterburner or its control interface is unavailable, monitoring and
  diagnostics still work and the plugin returns clear "unavailable" messages instead of crashing.

## Architecture (summary)

```
G-Assist
  |
G-Assist Plugin (Protocol V2, JSON-RPC 2.0)
  |
Intent / command validation (TuningValidator, SafetyPolicy, HardwareCapabilityResolver)
  |
Afterburner adapter (AfterburnerInterface -- the mock boundary)
  |
MSI Afterburner  (MAHM monitoring, MACM control)
  |
GPU
```

Runtime: **Python + the official `gassist_sdk`**, speaking **G-Assist Plugin Protocol V2**.

## Afterburner interfaces used (and their status)

| Interface | Purpose | Status |
|---|---|---|
| G-Assist Plugin SDK / Protocol V2 | Plugin transport & lifecycle | **Official / documented** |
| MAHM shared memory (`MAHMSharedMemory`) | Read-only GPU telemetry & limits | **Officially shipped header** (since Afterburner 4.6.0) |
| MACM shared memory | GPU control (power/offset/fan/profile) | **Undocumented / reverse-engineered** -- isolated, capability-gated, confirmation-gated, degrades gracefully |

See the [design document](.kiro/specs/afterburner-gassist-plugin/design.md) for the full
feasibility report and honest API-trust labeling.

## The specification

This repo is spec-driven. The full spec lives under
[`.kiro/specs/afterburner-gassist-plugin/`](.kiro/specs/afterburner-gassist-plugin/):

- **[design.md](.kiro/specs/afterburner-gassist-plugin/design.md)** -- technical feasibility report,
  architecture, data models, algorithms, error handling, and 8 executable correctness properties.
- **[requirements.md](.kiro/specs/afterburner-gassist-plugin/requirements.md)** -- 18 EARS-style
  requirements with testable acceptance criteria, cross-linked to the correctness properties.
- **[tasks.md](.kiro/specs/afterburner-gassist-plugin/tasks.md)** -- an ordered, test-driven
  implementation plan with property-based tests (Hypothesis) for each correctness property.

## Distribution & submission (planned)

There is no single "app store submit" button for G-Assist plugins. Three paths exist:

1. **Local install (primary).** Drop the built plugin folder into
   `%PROGRAMDATA%\NVIDIA Corporation\nvtopps\rise\plugins\afterburner\`
   (`manifest.json` + `plugin.py` + `afterburner/` + `libs/gassist_sdk`). G-Assist discovers it
   locally, no restart required.
2. **Community sharing via a self-hosted GitHub repo.** Publish the plugin's own repository; users
   clone/download it and copy it into the plugins folder. Contributing an example or docs into the
   official [NVIDIA/G-Assist](https://github.com/NVIDIA/G-Assist) repo is a fork -> pull-request flow
   subject to NVIDIA review.
3. **Official channels.** The NVIDIA G-Assist Plug-in Hackathon accepts submissions as a GitHub
   repo (`plugin.py`, `requirements.txt`, `manifest.json`, `config.json` if used, the plugin
   executable, and a README). NVIDIA is also rolling out in-app plugin discovery/download as a
   curated channel.

> **Licensing note:** Only **MSI** and **Guru3D** may distribute MSI Afterburner and RivaTuner
> Statistics Server. This plugin **does not bundle or redistribute Afterburner/RTSS** -- users
> install Afterburner themselves and the plugin detects it at runtime.

## Requirements to run (once implemented)

- Windows PC with an NVIDIA RTX GPU
- NVIDIA App with Project G-Assist enabled
- MSI Afterburner installed and running (download only from
  [msi.com](https://www.msi.com) or [guru3d.com](https://www.guru3d.com))

## License

See [LICENSE](LICENSE). This project integrates with -- but does not redistribute -- MSI Afterburner.
