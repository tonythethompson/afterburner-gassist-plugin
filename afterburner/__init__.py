"""NVIDIA G-Assist plugin for MSI Afterburner.

The plugin is built bottom-up: typed models -> the AfterburnerInterface mock boundary
(FakeAfterburner) -> safety/validation -> domain services -> protocol/plugin wiring -> real
MAHM/MACM clients. See .kiro/specs/afterburner-gassist-plugin/ for the full specification.
"""
__version__ = "0.1.0"
