# Profile-layout fixtures (plan task 9.7)

Mirrors of a real MSI Afterburner **4.6.7 (4.6.7.17439)** `Profiles\` directory,
captured from a live installation. Consumed by:

- profile parsing / listing unit tests (plan task 9.2),
- the **Property 9** immutable-directory security tests (task 9.3), which deep-copy a
  variant into a sandbox and mutate **only the copy** — the canonical fixtures below are
  never opened for writing by the suite,
- ownership reporting tests (task 9.5) for the populated-vs-empty `[Startup]` cases.

The on-disk profile format is not officially documented by MSI; these fixtures let the
reader and tests run without an Afterburner install while still exercising genuinely
Afterburner-shaped content. Re-verify against the installed version whenever the plugin
targets a new Afterburner release (same policy as `tests/fixtures/sdk_layouts/`).

## Layout (per variant)

```
Profiles/
+- ProfileN.cfg                      # per-slot marker files (saved slots only)
|                                    # observed content: [Settings]\r\nProfileContents=1\r\n
+- MSIAfterburner.cfg                # global config subset — NEVER parsed as profile
|                                    # content (holds RememberSettings / LockProfiles)
+- VEN_<…>&DEV_<…>&SUBSYS_<…>&REV_<…>&BUS_<n>&DEV_<n>&FN_<n>.cfg   # per-GPU tuning file
```

## Variants

| Variant | `[Startup]` | `RememberSettings` | Slots | Purpose |
|---|---|---|---|---|
| `startup_enabled/` | **populated** (auto-apply enabled, captured on 4.6.7) | `1` | 1–3 saved (`Profile1..3.cfg` markers; 4–5 absent) | canonical reference — the per-GPU file is a **byte-for-byte copy** of the real captured file (incl. the ~9 KB `VFCurve` blobs; LF line endings as Afterburner writes them) |
| `startup_disabled/` | **present but all keys empty** (disabled form) | `0` | 1–3 saved | the disabled A/B half — proves empty ≠ absent is *disabled*, never a loadable slot |
| `empty_slots/` | empty | `0` | `[Profile1..3]` contain **only** `Format=2` (freshly-initialized empty form); no markers | proves bare `Format=2`-only sections and absent sections are both *empty* |

The `[Startup]` A/B pair:

```ini
A - disabled (all keys empty):      B - enabled (populated, observed on 4.6.7):
[Startup]                           [Startup]
Format=2                            Format=2
PowerLimit=                         PowerLimit=100
CoreClkBoost=                       CoreClkBoost=95000
VFCurve=                            VFCurve=<hex blob>
MemClkBoost=                        MemClkBoost=200000
FanMode=                            FanMode=1
FanSpeed=                           FanSpeed=30
FanMode2=                           FanMode2=
FanSpeed2=                          FanSpeed2=
CoreVoltageBoost=                   CoreVoltageBoost=0
```

Observed per-slot values (identical in `startup_enabled` and `startup_disabled`):
`Format=2`, `PowerLimit=100`, `CoreClkBoost=95000` (+95 MHz), `MemClkBoost=200000 /
400000 / 600000` per slot, `FanMode=1`, `FanSpeed=31` in the slots (30 in `[Defaults]`),
`CoreVoltageBoost=0`. Non-tuning `[Defaults]` / `[Settings]` sections are present in
every per-GPU file and are never treated as loadable slots.

## `VFCurve` note

`VFCurve` has **no named control mapping** (it is ignored with a notice on load), so its
exact bytes do not affect behavior. The canonical `startup_enabled` per-GPU file carries
the captured hex verbatim (authenticity for the parser on real 33 KB files); the derived
`startup_disabled` and `empty_slots` variants use a short deterministic placeholder, as
allowed by plan task 9.7 ("canonical fixtures copy the captured bytes; inline examples use
a short deterministic placeholder").

## Refreshing after a version change

1. Copy the installed `Profiles\` directory's per-GPU `VEN_…&FN_*.cfg` over
   `startup_enabled/` (byte-for-byte).
2. Save a slot and enable/disabling startup auto-apply in Afterburner to recapture the
   A/B `[Startup]` pair and `RememberSettings`; update `startup_disabled/` /
   `empty_slots/` accordingly (placeholders stay).
3. Re-run the 9.2 / 9.3 suites.
