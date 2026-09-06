"""Task 3.3 — Property 6: no arbitrary memory access, only named validated controls.

The AfterburnerInterface public surface contains exactly the enumerated named operations and no
generic write/poke/offset/address-taking method. FakeAfterburner conforms via the
runtime_checkable protocol.
"""
from __future__ import annotations

import inspect

import pytest

from afterburner.integration.fake import FakeAfterburner
from afterburner.integration.interface import AfterburnerInterface

EXPECTED_SURFACE = frozenset(
    {
        "detect",
        "get_version",
        "read_telemetry",
        "read_all_telemetry",
        "read_capabilities",
        "read_tuning_state",
        "list_profiles",
        "load_profile",
        "reset_tuning",
        "apply_control",
        "apply_fan_curve",
    }
)

FORBIDDEN_SUBSTRINGS = ("write", "poke", "offset", "address", "mem", "ptr", "raw")


class TestProperty6NoGenericWritePrimitive:
    def test_protocol_surface_is_exactly_the_named_operations(self) -> None:
        members = {
            name
            for name, value in inspect.getmembers(AfterburnerInterface)
            if not name.startswith("_")
        }
        assert members == EXPECTED_SURFACE

    def test_no_generic_memory_writer_anywhere_on_the_surface(self) -> None:
        for name in EXPECTED_SURFACE:
            for forbidden in FORBIDDEN_SUBSTRINGS:
                assert forbidden not in name.lower(), (
                    f"{name} looks like a generic memory/offset primitive"
                )

    def test_every_apply_takes_a_named_feature(self) -> None:
        # Control entry points must be feature-named, never take an address/offset argument.
        sig = inspect.signature(AfterburnerInterface.apply_control)
        params = list(sig.parameters)
        assert "feature" in params
        assert "value" in params
        assert not any(p in params for p in ("offset", "address", "ptr", "count"))

    def test_fake_conforms_via_runtime_check(self) -> None:
        fake = FakeAfterburner()
        assert isinstance(fake, AfterburnerInterface)

    def test_no_generic_primitive_on_fake_surface(self) -> None:
        public = {n for n in dir(FakeAfterburner) if not n.startswith("_")}
        assert not (public & {"write", "poke", "write_memory", "write_bytes", "read_bytes"})
