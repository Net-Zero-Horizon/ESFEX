"""Project data-file path resolution (demand / availability / inflow).

Regression guard for the bug where running a project from the GUI silently
dropped every generator's availability profile: the GUI writes the run config
to a temp file in ``/tmp`` and the availability paths in the live state are
absolute, so the old strict ``safe_resolve_under(config_dir, ...)`` rejected
them as "outside the config directory" — the whole Japan network then fell back
to constant availability. ``resolve_project_data_file`` accepts absolute paths
(as demand paths already did) while still refusing ``../`` traversal.
"""

import os

import pytest

from esfex.utils.paths import resolve_project_data_file, safe_resolve_under


def test_absolute_path_outside_config_dir_is_accepted(tmp_path):
    """An absolute path to an existing file is trusted as-is (the fix)."""
    data = tmp_path / "data" / "avail.csv"
    data.parent.mkdir()
    data.write_text("1.0\n")
    # A *different* directory stands in for the /tmp run-config location.
    config_dir = tmp_path / "run_tmp"
    config_dir.mkdir()

    resolved = resolve_project_data_file(config_dir, str(data))
    assert resolved == data

    # The old strict check would have rejected exactly this — that was the bug.
    with pytest.raises(ValueError):
        safe_resolve_under(config_dir, str(data))


def test_relative_path_resolves_under_config_dir(tmp_path):
    resolved = resolve_project_data_file(tmp_path, "availability/x.csv")
    assert resolved == (tmp_path / "availability" / "x.csv").resolve()


def test_relative_traversal_is_still_refused(tmp_path):
    """Security is preserved: a relative ``../`` escape still raises."""
    with pytest.raises(ValueError):
        resolve_project_data_file(tmp_path, os.path.join("..", "..", "etc", "passwd"))
