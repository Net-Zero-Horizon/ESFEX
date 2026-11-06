"""The dataset root defaults to the user's ~/.esfex/data.

It defaulted to a maintainer-local volume, so for any other user the Studio's
caches (Geofabrik extracts, OSM tiles, CMIP6 subsets) tried to create folders
under /media/manuel and failed.
"""

import os
import subprocess
import sys
from pathlib import Path


def _roots(env_value):
    """(DATA_ROOT, GEOFABRIK_DIR) as a fresh interpreter resolves them."""
    env = dict(os.environ)
    env.pop("ESFEX_DATA_ROOT", None)
    if env_value is not None:
        env["ESFEX_DATA_ROOT"] = env_value
    out = subprocess.run(
        [sys.executable, "-c",
         "import esfex.paths as p; print(p.DATA_ROOT); print(p.GEOFABRIK_DIR)"],
        env=env, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    return Path(out[0]), Path(out[1])


def test_default_root_is_under_home():
    root, geofabrik = _roots(None)
    assert root == Path.home() / ".esfex" / "data"
    assert geofabrik.is_relative_to(root)


def test_env_var_sets_root(tmp_path):
    assert _roots(str(tmp_path))[0] == tmp_path
    assert _roots("~/datasets")[0] == Path.home() / "datasets"
