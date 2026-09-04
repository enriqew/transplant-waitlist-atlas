"""The export must fail loudly when a bronze source on disk contributes nothing.

Regression test for the 2026-05-21 run, which rebuilt the OPTN long-form CSV
after its manually downloaded source files were gone. The export happily wrote
an empty us-fate-distribution.json and a world-waitlist.json with no US rows,
and both were committed.
"""
from __future__ import annotations

import json

import pytest

from export.build_artifacts import _assert_sources_contributed


class _Log:
    def info(self, *args, **kwargs):  # noqa: D102
        pass


@pytest.fixture
def optn_snapshot(tmp_path, monkeypatch):
    """A bronze snapshot declaring 10,480 ingested OPTN rows."""
    from export import build_artifacts

    snap = tmp_path / "optn_waitlist" / "2026-05-19"
    snap.mkdir(parents=True)
    (snap / "meta.json").write_text(
        json.dumps({"files": [{"row_count": 37}, {"row_count": 1012}, {"row_count": 9431}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(build_artifacts, "RAW_ROOT", tmp_path)
    return snap


WORLD_WITH_US = [
    {"country_iso3": "USA", "year": 2024, "organ": "kidney", "patients": 1, "source": "OPTN (derived)"},
    {"country_iso3": "MEX", "year": 2024, "organ": "kidney", "patients": 1, "source": "CENATRA"},
]
FATE = [{"year": 2024, "organ": "kidney", "reason_bucket": "died_waiting", "patients": 1}]


def test_passes_when_optn_contributed(optn_snapshot):
    _assert_sources_contributed(world=WORLD_WITH_US, fate=FATE, log=_Log())


def test_fails_on_empty_fate_distribution(optn_snapshot):
    with pytest.raises(SystemExit) as excinfo:
        _assert_sources_contributed(world=WORLD_WITH_US, fate=[], log=_Log())
    assert "us-fate-distribution is empty" in str(excinfo.value)
    assert "10,480" in str(excinfo.value)


def test_fails_when_us_missing_from_world(optn_snapshot):
    world = [r for r in WORLD_WITH_US if r["country_iso3"] != "USA"]
    with pytest.raises(SystemExit) as excinfo:
        _assert_sources_contributed(world=world, fate=FATE, log=_Log())
    assert "no USA rows" in str(excinfo.value)


def test_silent_when_no_optn_snapshot_on_disk(tmp_path, monkeypatch):
    """No snapshot means nothing to contribute; that is not a broken run."""
    from export import build_artifacts

    monkeypatch.setattr(build_artifacts, "RAW_ROOT", tmp_path)
    _assert_sources_contributed(world=[], fate=[], log=_Log())
