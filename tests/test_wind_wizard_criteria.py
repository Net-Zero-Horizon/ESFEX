"""The wind wizard's criteria step builds the MCDA settings esfex's analyzer
reads.

It used to build windrex's ``CriterionConfig`` (no ``direction``) and
``MCDAConfig`` (a list of criteria, no LULC scores), so the step raised a
TypeError and no wind analysis could start.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets")
pytest.importorskip("windrex")


@pytest.fixture(scope="module")
def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_criteria_step_config_runs_through_the_analyzer(_app, monkeypatch):
    from esfex.visualization.workflows import wind_analysis as wa
    from esfex.visualization.workflows.wind_steps import CriteriaConfigStep

    mcda = CriteriaConfigStep().get_config()
    assert isinstance(mcda, wa.MCDAConfig)
    assert mcda.criteria["slope"].direction == "minimize"
    assert mcda.lulc_scores == wa.DEFAULT_LULC_SCORES

    lats, lons = np.array([10.0, 10.1]), np.array([-70.0, -69.9])
    cf = np.array([[0.30, 0.45], [0.25, 0.40]])
    an = wa.WindAnalyzer((10.0, -70.0, 10.1, -69.9), wa.WindConfig(), mcda)
    monkeypatch.setattr(an, "_compute_capacity_factors", lambda: (cf, lats, lons))
    monkeypatch.setattr(an, "_fetch_terrain",
                        lambda pts: ([0.0] * len(pts), [1.0] * len(pts)))
    monkeypatch.setattr(an, "_fetch_lulc", lambda pts: [0.5] * len(pts))
    monkeypatch.setattr(an, "_compute_dist_to_grid_vectorized",
                        lambda pts: [5.0] * len(pts))
    summary = an._analyze()
    assert summary.total_cells == 4
    assert summary.cf_max == pytest.approx(0.45)
    assert summary.mcda_score_max >= summary.mcda_score_min
