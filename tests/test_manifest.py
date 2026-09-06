"""manifest.json parity checks (plan task 21).

The static manifest must stay in lock-step with the plugin's runtime registry and risk
policy: every function the plugin registers is declared with NL-friendly properties.
High-risk (`set_*` / `optimize_*`) entries say the operation requires confirmation but do
NOT advertise a `confirm_token` input — Protocol V2 has no token channel (the engine
relays the user's plain-text verdict as `input`; tokens are internal to the plugin).
Control functions are declared (G-Assist registers the static manifest) but execution is
capability-gated at runtime.
"""
from __future__ import annotations

import json
from pathlib import Path

from afterburner.integration.fake import FakeAfterburner
from afterburner.protocol.plugin import GAssistPlugin, build_services
from afterburner.safety.policy import HIGH_RISK_FUNCTIONS

MANIFEST = json.loads(Path("manifest.json").read_text(encoding="utf-8"))
FUNCTIONS = MANIFEST["functions"]
BY_NAME = {f["name"]: f for f in FUNCTIONS}


def _registered() -> set:
    services = build_services(FakeAfterburner(), None)
    plugin = GAssistPlugin(services)
    return set(plugin.registered_functions())


class TestManifestSurface:
    def test_every_registered_function_is_declared(self) -> None:
        assert BY_NAME.keys() == _registered()
        assert len(FUNCTIONS) == 17

    def test_top_level_shape_matches_protocol_v2(self) -> None:
        assert MANIFEST["manifestVersion"] == 1
        assert MANIFEST["name"] == "afterburner"
        assert MANIFEST["protocol_version"] == "2.0"
        assert MANIFEST["executable"] == "plugin.py"
        assert MANIFEST["persistent"] is True
        assert Path(MANIFEST["executable"]).is_file()

    def test_names_unique_and_required_subset_of_properties(self) -> None:
        names = [f["name"] for f in FUNCTIONS]
        assert len(names) == len(set(names))
        for fn in FUNCTIONS:
            assert set(fn.get("required", [])) <= set(fn.get("properties", {}))
            for spec in fn["properties"].values():
                assert spec["type"] in (
                    "integer",
                    "number",
                    "string",
                    "boolean",
                    "array",
                    "object",
                )

    def test_descriptions_are_nl_friendly_and_non_empty(self) -> None:
        for fn in FUNCTIONS:
            assert fn["description"].strip().endswith((".", ")"))
            assert fn["description"][0].isupper()
            assert fn["tags"]

    def test_arg_names_match_the_plugin_parser(self) -> None:
        # The argument keys the executors read must be the ones the manifest advertises.
        expected_args = {
            "set_power_limit": {"percent"},
            "set_core_offset": {"offset_mhz"},
            "set_memory_offset": {"offset_mhz"},
            "set_fan_percent": {"percent"},
            "set_fan_curve": {"points"},
            "optimize_thermal": {"target_c"},
            "load_profile": {"profile_id"},
            "set_profile_nickname": {"profile_id", "nickname"},
        }
        for name, required_args in expected_args.items():
            fn = BY_NAME[name]
            assert required_args <= set(fn["properties"].keys()), name
            assert required_args <= set(fn["required"]), name


class TestManifestRiskGating:
    def test_high_risk_functions_say_confirmation_is_required(self) -> None:
        assert set(BY_NAME) & HIGH_RISK_FUNCTIONS == HIGH_RISK_FUNCTIONS
        for name in HIGH_RISK_FUNCTIONS:
            fn = BY_NAME[name]
            # Protocol V2: no token channel exists, so the schema must not ask for one.
            assert "confirm_token" not in fn["properties"], name
            assert "Requires confirmation" in fn["description"], name

    def test_low_risk_functions_do_not_require_confirmation(self) -> None:
        for name, fn in BY_NAME.items():
            if name not in HIGH_RISK_FUNCTIONS:
                assert "confirm_token" not in fn["properties"], name
                assert "Requires confirmation" not in fn["description"], name
