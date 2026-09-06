# Tuning-Authority Precedence & Interaction (Requirement 19 research gate)

Status: **implemented-plan gate for the Task 20 MACM control layer (Requirement 19.6)**.
Authored 2026-09 against an MSI Afterburner **4.6.7.17439** install
(`C:\Program Files (x86)\MSI Afterburner\`) and current NVIDIA public material. Findings below
carry a source-status label (see legend); driver-level behavior beyond what the labeled
sources state **must be re-validated on real hardware** during the optional integration tests
(plan 18.2 monitoring — done; 20.2 control — pending opt-in flag).

**Source-status legend.** *Official, current* = NVIDIA/MSI documentation, product pages, or
shipped SDK material. *Official, shipped SDK header (verified)* = content read directly from
the installed Afterburner SDK. *Official, marketing-level* = vendor announcement copy that
confirms product capability, not implementation detail. *Community evidence, reference only* =
forum/developer posts; treated as corroboration, never as an implementation contract.
*Reference only (not officially documented)* = observed on the real install, no vendor doc.

## 1. The GPU exposes ONE shared tuning state

A GPU exposes a single set of driver tuning parameters (clock/voltage/power/fan behavior).
Any authority applies a change by **replacing that shared state through whichever driver
interface it talks to** — it does not add an independent layer on top of another tool's
values. Consequently:

- Changes from different tools **do not stack**; the last writer wins for the parameters it
  touches. One tool may not even recognize offsets written by another.
- *Community evidence, reference only:* NVIDIA developer-forum reports describe OC offsets
  written by other utilities not being recognized through NVAPI perf-pstate delta queries —
  i.e., tools can write the same physical state through interfaces that do not reflect each
  other's view.
- This is the model the plugin implements everywhere: `TuningOwnershipService` reports one
  shared applied state observed only through Afterburner, and every confirmation prompt says
  the change **replaces** Afterburner's currently applied values.

## 2. The authorities and the driver interfaces each writes

| Authority | What it writes | Interface it uses | Source status |
|---|---|---|---|
| **G-Assist native GPU tuning** (overclock in 15 MHz steps up to +60 MHz; power-efficiency mode) | core-clock offset / efficiency tuning of the shared driver state | NVIDIA performance/tuning interfaces exposed to G-Assist | *Official, current* — NVIDIA Project G-Assist product page (nvidia.com/software/nvidia-app/g-assist) and G-Assist announcement (GeForce News, 2025-03) |
| **NVIDIA App Automatic Tuning** | scanned auto-overclock profile applied to the shared driver tuning state (power/temperature/voltage/fan targets feed its tuning algorithm) | NVIDIA App / driver (NVAPI-based) automatic tuning | *Official, marketing-level* — NVIDIA App beta announcement of one-click GPU performance tuning (GeForce News, 2024-06); *community evidence* for in-app behavior |
| **MSI Afterburner** | applied tuning state + profiles (power limit, core/memory clock boost, voltage, fan speed/auto, thermal limit, VF curve) | MAHM (monitoring) and MACM (control shared memory, v2.0) via the shipped SDK header + sample; **not** NVAPI | *Official, shipped SDK header (verified)* — `SDK\Include\MACMSharedMemory.h` / `MAHMSharedMemory.h`, `SDK\Samples\SharedMemory\MACMSharedMemorySample` on the 4.6.7 install |
| **MSI Afterburner at Windows startup** | auto-applies a saved hardware profile (populated `[Startup]` section) when Afterburner launches | Afterburner's own profile loader | *Reference only (not officially documented)* — verified on the real 4.6.7 Profiles directory (A/B `[Startup]` observed); corroborated by MSI usage docs (*Official*) |
| Other OC utilities | shared driver tuning state | their own (NVAPI/ICCLK/other) interfaces | *Community evidence, reference only* |

Afterburner's auto-apply means Afterburner itself is a *background writer*: an applied change
can be replaced later by a startup profile or by the user inside Afterburner while a G-Assist
confirmation is outstanding — which is why the Requirement 19.4 no-op check is re-evaluated
**at write time, under the mutex**, not only at decision time.

## 3. Stacking-vs-replace semantics

- Driver parameters are shared: an offset applied by Afterburner and an offset applied by
  NVIDIA App Automatic Tuning do not combine; the second replaces the first for the same
  parameter (subject to each tool's own runtime re-assertion, e.g. an auto-tuner that keeps
  a scan active).
- The plugin therefore never claims values "add to" another tool's values (Requirement
  19.1), never asserts whether an external authority is active or inactive (Requirement
  19.2/19.5), and only verifies what it can observe: Afterburner's own read-back after a
  MACM FLUSH.

## 4. What is observable vs NOT observable through Afterburner interfaces

Observable through Afterburner interfaces (the only legitimate source for the plugin):

- Whether Afterburner is installed / running and whether MAHM (monitoring) and MACM
  (control) shared memory are present and valid (signature/version v2.0).
- Live per-GPU monitoring values (MAHM sources) and Afterburner's applied control state
  (MACM `*_Cur` / `*_Min..*_Max` / `*_Def` fields), verified post-FLUSH read-back.
- Saved hardware profiles in the Profiles directory (read-only enumeration) and whether a
  populated `[Startup]` section enables startup auto-apply.

NOT observable through Afterburner interfaces (must never be fabricated):

- NVIDIA App Automatic Tuning's profile/scan state, G-Assist native tuning's current offset
  or efficiency mode, or any other OC utility's applied values. Afterburner maps expose only
  Afterburner's own view of the shared state.
- Which external authority (if any) most recently wrote the shared state, or whether one is
  "active".

Consequence: external authorities are always reported
`unknown_not_observable` (Property 10), and advice to disable a competing auto-tuning feature
is presented as a user action, never performed by the plugin (Requirement 19.5).

## 5. Design consequences (Requirements 19.1–19.5)

1. **One shared resource, replace-only** — applied changes replace Afterburner's current
   values; no stacking claims anywhere (Requirement 19.1).
2. **Observe-then-apply** — before any change (`set_*`, `optimize_*`, `load_profile`,
   `reset_tuning`) the plugin determines through Afterburner what is observable: running
   state, readable applied state, matching active profile, and `[Startup]` presence — the
   confirmation clause names the replaced value and warns external authorities are not
   observable (Requirement 19.2).
3. **`get_tuning_ownership`** — Afterburner authority `observed` with applied state +
   matched profile + startup auto-apply; externals `unknown_not_observable` (Requirement
   19.3; implemented, Property 10 tests).
4. **Write-time no-op** — equality within `TUNING_MATCH_TOLERANCE` (1.0) checked at decision
   time (avoid prompting) **and again at write time inside the mutex transaction** before
   FLUSH; a no-op returns success without issuing FLUSH (Requirement 19.4; MACM client
   implements the authoritative second check).
5. **Never manage externals** — the plugin modifies no external authority and asserts none
   (Requirement 19.5).

## 6. Re-verification plan (Requirement 19.6 close-out)

- [x] MAHM monitoring integration smoke (plan 18.2) on the real 4.6.7 install — signature,
  version, live read-only data; header layout re-bound against installed header and the
  committed `tests/fixtures/sdk_layouts` fixtures.
- [ ] MACM control integration (plan 20.2) — opt-in flag-gated apply-and-read-back of a
  single clamped control, write-time no-op, typed unavailable errors, and re-verification
  that Afterburner honors boost-offset writes (the design's recorded open question: the
  sample exercises absolute core/memory clock fields only; the plugin writes the v2.0
  **boost** fields for offsets and must confirm read-back honors them).
- [ ] Confirm on real hardware that Afterburner's `FLUSH` read-back (dwCommand cleared only
  after apply) matches the header's documented ordering, so verification reads see
  post-apply state.

Unresolved questions to re-check at 20.2/real-hardware time: whether MACM FLUSH applies
boost-offset writes exactly as the sample's absolute-clock writes behave; whether NVIDIA App
Automatic Tuning re-asserts its scan on top of Afterburner changes (not observable through
Afterburner — handled by the no-stacking, replace-only posture regardless); and whether
Afterburner's `MACM_SHARED_MEMORY_FLAG_SYNC` peer mirroring covers all multi-GPU layouts the
plugin must support.
