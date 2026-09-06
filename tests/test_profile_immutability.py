"""Plan task 9.3 — Property 9: Profiles-directory immutability under adversarial input.

Hypothesis-randomized adversarial function arguments (path/traversal/absolute-path-looking
profile identifiers, arbitrary strings) and adversarial profile contents (malformed INI,
path-like/traversal values, oversized/duplicate sections, unexpected section names) are run
through ``get_profiles`` / ``load_profile`` / ``reset_profile`` and capability resolution
against a **sandboxed copy** of the task 9.7 fixtures. After every operation the directory
tree (names, sizes, mtimes, content hashes) must be byte-for-byte unchanged and no
write-capable file handle may ever have been requested. The canonical fixtures are never
opened for writing.
"""
from __future__ import annotations

import uuid

from hypothesis import given
from hypothesis import strategies as st

from afterburner.integration.client import AfterburnerClient
from afterburner.integration.fake import FakeAfterburner, default_capabilities
from afterburner.integration.profiles import ProfileFileReader
from afterburner.models import PluginError, TuningState
from afterburner.safety.capabilities import HardwareCapabilityResolver
from afterburner.services.profiles import ProfileManager

from profiles_util import (
    VARIANTS,
    assert_trees_identical,
    manager_for,
    sandbox_copy,
    tree_snapshot,
)

# --------------------------------------------------------------------------- strategies

_PATH_CHARS = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),  # no surrogates (not encodable)
    ),
    max_size=48,
)


@st.composite
def adversarial_profile_id(draw):
    """Path/traversal/absolute-path-looking and arbitrary identifiers."""
    base = draw(
        st.one_of(
            st.integers(min_value=-1_000_000, max_value=1_000_000),
            st.floats(
                min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False
            ),
            st.booleans(),
            st.none(),
            _PATH_CHARS,
        )
    )
    if isinstance(base, str):
        return draw(
            st.sampled_from(
                [
                    base,
                    "..\\" + base,
                    "../" + base,
                    base + "\\..\\..",
                    "C:\\" + base,
                    "\\\\server\\share\\" + base,
                    "/etc/" + base,
                    base + ".cfg",
                    "Profile1",
                    "1",
                ]
            )
        )
    return base


@st.composite
def adversarial_gpu_index(draw):
    return draw(
        st.one_of(
            st.integers(min_value=-100, max_value=100),
            st.none(),
            _PATH_CHARS,
            st.lists(st.integers(), max_size=3),
            st.booleans(),
        )
    )


@st.composite
def adversarial_content(draw):
    """Adversarial per-GPU file content: malformed INI, traversal/path values,
    duplicate/oversized sections, unexpected section names, junk lines."""
    evil_values = [
        "..\\..\\..\\windows\\system32\\evil",
        "../../../../etc/passwd",
        "C:\\Program Files (x86)\\MSI Afterburner\\Profiles\\Profile1.cfg",
        "\\\\server\\share\\file.cfg",
        "&BUS_11&DEV_0&FN_0",
        "VEN_10DE&DEV_2F04",
        "PowerLimit=../../x",
        "=",
    ]
    section_names = [
        "Profile1", "Profile2", "Profile5", "Profile6", "ProfileX",
        "..\\..\\..\\evil", "../evil", "Startup", "Defaults", "Settings",
        "Profile1\\..\\Profile2", "[nested", "", "VEN_10DE&DEV_2F04&SUBSYS_89E61043"
        "&REV_A1&BUS_11&DEV_0&FN_0",
    ]
    key_names = [
        "PowerLimit", "CoreClkBoost", "MemClkBoost", "FanMode", "FanSpeed", "VFCurve",
        "CoreVoltageBoost", "Format", "..\\..\\..\\evil", "../evil", "KEY WITH SPACES",
        "=x", "", "PowerLimit=1", "Profile1",
    ]

    def line() -> st.SearchStrategy[str]:
        return st.one_of(
            st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200),
            st.sampled_from(evil_values),
            st.sampled_from(section_names).map(lambda s: "[" + s + "]"),
            st.sampled_from(key_names).map(lambda k: k + "=" + k),
            st.sampled_from(key_names).map(lambda k: k + "="),
            st.sampled_from(evil_values).map(lambda v: "PowerLimit=" + v),
            st.sampled_from(evil_values).map(lambda v: "CoreClkBoost=" + v),
        )

    n_lines = draw(st.integers(min_value=0, max_value=120))
    lines = draw(st.lists(line(), min_size=n_lines, max_size=n_lines))
    return "\n".join(lines) + "\n"


@st.composite
def oversized_content(draw):
    """Malicious size/structure: oversized single values, very many duplicate sections."""
    kind = draw(st.sampled_from(["huge_value", "huge_section", "many_dupes", "binaryish"]))
    if kind == "huge_value":
        blob = "AB" * draw(st.integers(min_value=0, max_value=50_000))
        return "[Profile1]\nVFCurve=" + blob + "\n"
    if kind == "huge_section":
        inner = "\n".join(
            "Key%d=%d" % (i, i) for i in range(draw(st.integers(min_value=0, max_value=5000)))
        )
        return "[Profile1]\n" + inner + "\n"
    if kind == "many_dupes":
        n = draw(st.integers(min_value=0, max_value=200))
        return "\n".join("[Profile1]\nPowerLimit=100\nMemClkBoost=200000\n" for _ in range(n))
    chars = draw(
        st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=4000)
    )
    return "[Profile1]\nPowerLimit=" + chars + "\n[Profile2]\n" + chars + "\n"


def make_manager(fake: FakeAfterburner, profiles_dir) -> ProfileManager:
    client = AfterburnerClient(interface=fake)
    reader = ProfileFileReader(profiles_dir)
    return ProfileManager(client, reader)


def fresh_fake() -> FakeAfterburner:
    fake = FakeAfterburner()
    fake.set_capabilities(default_capabilities())
    fake.set_tuning(TuningState(gpu_index=0))
    return fake


def unique_sandbox_parent(tmp_path_factory):
    """A fresh, uniquely named directory per generated example (tmp_path_factory is
    session-scoped, so Hypothesis resets it between examples by creating a new one)."""
    return tmp_path_factory.mktemp("property9") / uuid.uuid4().hex


class TestProperty9DirectoryImmutability:
    """One combined property: adversarial ids + contents against every fixture variant."""

    @given(
        variant=st.sampled_from(VARIANTS),
        data=st.data(),
    )
    def test_no_operation_mutates_the_directory(
        self, tmp_path_factory, variant, data
    ) -> None:
        parent = unique_sandbox_parent(tmp_path_factory)
        sandbox = sandbox_copy(variant, parent, suffix="_p9a")
        gpu_file = next(sandbox.glob("VEN_*FN_0.cfg"))

        content = data.draw(
            st.one_of(adversarial_content(), oversized_content())
        )
        gpu_file.write_text(content, encoding="utf-8")

        fake = fresh_fake()
        client = AfterburnerClient(interface=fake)
        reader = ProfileFileReader(sandbox)
        manager = ProfileManager(client, reader)
        resolver = HardwareCapabilityResolver(fake)

        baseline = tree_snapshot(sandbox)

        profile_id = data.draw(adversarial_profile_id())
        gpu_index = data.draw(adversarial_gpu_index())

        ops = [
            lambda: manager.get_profiles(gpu_index),
            lambda: manager.load_profile(profile_id, gpu_index),
            lambda: manager.reset_profile(gpu_index),
            lambda: resolver.resolve(
                data.draw(adversarial_gpu_index())
            ),
        ]
        for op in ops:
            try:
                op()
            except PluginError:
                pass  # typed failure is fine — the tree is what must not change
            except Exception as exc:  # pragma: no cover - Property 9: never unhandled
                raise AssertionError(f"unhandled exception escaped: {type(exc).__name__}: {exc}")
            assert_trees_identical(baseline, tree_snapshot(sandbox))

        # No write-capable handle was ever requested by the reader.
        assert set(reader.open_modes()) <= {"r"}
        assert_trees_identical(baseline, tree_snapshot(sandbox))


class TestProperty9AdversarialArgumentsOnCanonicalContent:
    """Adversarial function arguments against pristine fixture content (incl. the
    [Startup] A/B pair and empty-slot variant)."""

    @given(
        variant=st.sampled_from(VARIANTS),
        profile_id=adversarial_profile_id(),
        data=st.data(),
    )
    def test_arguments_never_become_filesystem_targets(
        self, tmp_path_factory, variant, profile_id, data
    ) -> None:
        parent = unique_sandbox_parent(tmp_path_factory)
        sandbox = sandbox_copy(variant, parent, suffix="_p9b")
        fake = fresh_fake()
        client = AfterburnerClient(interface=fake)
        reader = ProfileFileReader(sandbox)
        manager = ProfileManager(client, reader)

        baseline = tree_snapshot(sandbox)
        gpu_index = data.draw(adversarial_gpu_index())

        for op in (
            lambda: manager.get_profiles(gpu_index),
            lambda: manager.load_profile(profile_id, gpu_index),
            lambda: manager.reset_profile(gpu_index),
        ):
            try:
                op()
            except PluginError:
                pass
            except Exception as exc:  # pragma: no cover
                raise AssertionError(
                    f"unhandled exception escaped: {type(exc).__name__}: {exc}"
                )
            assert_trees_identical(baseline, tree_snapshot(sandbox))

        assert set(reader.open_modes()) <= {"r"}
        assert_trees_identical(baseline, tree_snapshot(sandbox))


class TestProperty9CapabilityResolutionNeverTouchesFiles:
    @given(data=st.data())
    def test_capability_resolution_does_not_open_or_write_anything(
        self, tmp_path_factory, data
    ) -> None:
        parent = unique_sandbox_parent(tmp_path_factory)
        sandbox = sandbox_copy("startup_enabled", parent, suffix="_p9c")
        baseline = tree_snapshot(sandbox)

        fake = fresh_fake()
        fake.set_capabilities(default_capabilities())
        resolver = HardwareCapabilityResolver(fake)
        for _ in range(3):
            gpu_index = data.draw(adversarial_gpu_index())
            try:
                resolved = resolver.resolve(gpu_index)
                assert resolved is not None
            except PluginError:
                pass  # typed degradation is acceptable
        # Capability resolution is control-map work: the Profiles directory is untouched
        # and no file handle of any kind was opened.
        assert_trees_identical(baseline, tree_snapshot(sandbox))
        assert fake.applied == []
