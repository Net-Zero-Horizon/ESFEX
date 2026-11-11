"""min_annual_increment above max_annual_increment is flagged.

The run is not infeasible (the RE floor shortfall is priced), but the floor
is then missed every year: the loader and the Studio validation warn.
"""

import logging
import sys

import pytest

sys.path.insert(0, "tests")
from test_zones import _make_system_config  # noqa: E402

from esfex.config.schema import re_increment_conflict  # noqa: E402
from esfex.visualization.data.gui_model import GuiSystemState  # noqa: E402
from esfex.visualization.data.validation import validate_state  # noqa: E402


@pytest.mark.parametrize("min_inc, max_inc, flagged", [
    (0.15, 0.10, True),
    (0.10, 0.10, False),
    (0.01, 0.10, False),
    (1.5, 1.0, False),      # a maximum ≥ 1 switches the cap off
])
def test_conflict_rule(min_inc, max_inc, flagged):
    assert (re_increment_conflict(min_inc, max_inc) is not None) == flagged


def test_loader_warns(caplog):
    base = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)])
    data = base.model_dump()
    data.update(min_annual_increment=0.2, max_annual_increment=0.05)
    with caplog.at_level(logging.WARNING, logger="esfex.config.schema"):
        type(base).model_validate(data)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("min_annual_increment (0.2) is greater than max_annual_increment (0.05)"
               in m for m in msgs)


def test_loader_silent_when_consistent(caplog):
    base = _make_system_config(num_nodes=1, coords=[(21.0, -80.0)])
    with caplog.at_level(logging.WARNING, logger="esfex.config.schema"):
        type(base).model_validate(base.model_dump())
    assert not any("min_annual_increment" in r.getMessage() for r in caplog.records)


def test_studio_validation_warns():
    s = GuiSystemState()
    s.settings.min_annual_increment = 0.2
    s.settings.max_annual_increment = 0.05
    issues = validate_state(s, categories={"configuration"})
    hits = [i for i in issues if "min_annual_increment" in i.message]
    assert len(hits) == 1 and hits[0].severity == "warning"
    s.settings.min_annual_increment = 0.01
    assert not [i for i in validate_state(s, categories={"configuration"})
                if "min_annual_increment" in i.message]
