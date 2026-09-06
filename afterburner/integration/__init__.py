"""Afterburner integration layer.

Only the OS-touching modules live here: the MAHM monitoring client, the MACM control client,
the read-only profile-file reader, plus the mock boundary (AfterburnerInterface / FakeAfterburner)
and the AfterburnerClient facade.
"""
