"""Docs regression pin: every shipped smoke gate is named in the README.

Regression pin for review finding m4-f1: the milestone-4 gate
(``scripts/smoke_m4.py``) landed one commit after the README's verification
list (03b3419 then 8e3c4a3) and was never added to it — a new user following
section 8 (Verifying your installation) never ran the M4 gate. This test
fails whenever a ``scripts/smoke_*.py`` gate exists on disk without a mention
in the README, so the next gate cannot be forgotten either.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"
_README = _REPO / "README.md"


def test_every_smoke_gate_script_is_named_in_the_readme() -> None:
    readme = _README.read_text(encoding="utf-8")
    gates = sorted(_SCRIPTS.glob("smoke_*.py"))
    assert gates, "no smoke gate scripts found — the docs pin lost its subject"
    missing = [gate.name for gate in gates if gate.stem not in readme]
    assert not missing, (
        f"README section 8 (Verifying your installation) does not mention"
        f" {missing} — add each gate to the verification command list"
    )
