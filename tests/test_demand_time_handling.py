"""Demand reading and model time handling (#7, #48, #49, #50).

Every modelled year is 8760 h: demand and profiles are read in 8760-row
blocks, Feb 29 is dropped from leap-year data and from the date labels, and
year labels follow ``date_start`` unless a start year is given.
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import openpyxl
import pytest

from esfex.bridge.adapters import PowerSystemAdapter
from esfex.config.schema import ESFEXConfig, MetaNetworkConfig, NodeConfig, SystemConfig
from esfex.io.demand import DemandDataManager, load_availability_profile, load_demand_data
from esfex.runner import Orchestrator
from esfex.utils.temporal import HOURS_STD_YEAR

H = HOURS_STD_YEAR


def _write_xlsx(path: Path, rows, header=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    if header:
        ws.append(header)
    for r in rows:
        ws.append(list(r))
    wb.save(path)


def _orch(tmp_path, demand_file, date_start="01/01/2025 00:00", num_nodes=2):
    sys_cfg = SystemConfig(
        name="s",
        nodes=NodeConfig(nodes_connections=[0.0] * num_nodes ** 2, num_nodes=num_nodes),
        demand_path=demand_file.name,
    )
    cfg = ESFEXConfig(
        meta_network=MetaNetworkConfig(systems=["s"]),
        systems={"s": sys_cfg},
        date_start=date_start,
    )
    return Orchestrator(cfg, output_dir=tmp_path / "out",
                        config_path=tmp_path / "c.yaml")


# ── #7: Excel demand read by the run ────────────────────────────────────────

def test_run_reads_first_excel_row_as_data(tmp_path):
    f = tmp_path / "demand.xlsx"
    _write_xlsx(f, [(i, 10 * i) for i in range(1, 49)])
    demand, total, nodes, _, _ = _orch(tmp_path, f)._load_demand(years=1, start_year=2025)
    assert nodes == 2
    assert demand[0].tolist() == [1.0, 10.0]   # first row is hour 0, not a header
    assert total == 48


def test_run_skips_excel_header_row(tmp_path):
    f = tmp_path / "demand.xlsx"
    _write_xlsx(f, [(i, 10 * i) for i in range(1, 49)], header=["node_0", "node_1"])
    demand, total, _, _, _ = _orch(tmp_path, f)._load_demand(years=1, start_year=2025)
    assert demand[0].tolist() == [1.0, 10.0]
    assert total == 48


def test_api_and_run_read_same_rows(tmp_path):
    f = tmp_path / "demand.xlsx"
    _write_xlsx(f, [(i, i) for i in range(48)])
    run_demand, *_ = _orch(tmp_path, f)._load_demand(years=1, start_year=2025)
    api_demand, *_ = load_demand_data(f)
    np.testing.assert_array_equal(run_demand, api_demand)


def test_run_drops_feb29_of_leap_year_demand(tmp_path):
    f = tmp_path / "demand.csv"
    np.savetxt(f, np.column_stack([np.arange(8784.0)] * 2), delimiter=",")
    orch = _orch(tmp_path, f, date_start="01/01/2028 00:00")
    orch._start_date = datetime(2028, 1, 1)
    demand, total, _, _, time_index = orch._load_demand(years=2, start_year=2028)
    # one 8760-h year (Feb 29 removed), tiled for the second year
    assert total == 2 * H
    assert demand[1416, 0] == 1440.0          # 1 Mar 00:00 follows 28 Feb 23:00
    assert demand[H, 0] == 0.0
    assert time_index[1416] == datetime(2028, 3, 1)
    assert time_index[H] == datetime(2029, 1, 1)


# ── #49 / #50: date and year labels ─────────────────────────────────────────

def test_run_time_index_skips_feb29(tmp_path):
    f = tmp_path / "demand.csv"
    np.savetxt(f, np.ones((5 * H, 2)), delimiter=",")
    orch = _orch(tmp_path, f, date_start="01/01/2027 00:00")
    orch._start_date = orch._resolve_start_date()
    *_, years_list, time_index = orch._load_demand(years=5, start_year=2027)
    assert years_list == [2027, 2028, 2029, 2030, 2031]
    for k, year in enumerate(years_list):
        assert time_index[k * H] == datetime(year, 1, 1)
    assert not any(t.month == 2 and t.day == 29 for t in time_index)


def test_start_year_defaults_to_date_start_year(tmp_path):
    orch = _orch(tmp_path, tmp_path / "d.csv", date_start="01/01/2030 00:00")
    assert orch._resolve_start_date() == datetime(2030, 1, 1)


def test_explicit_start_year_moves_date_labels(tmp_path):
    orch = _orch(tmp_path, tmp_path / "d.csv", date_start="15/06/2030 06:00")
    assert orch._resolve_start_date(2040) == datetime(2040, 6, 15, 6, 0)


def test_cli_passes_start_year(tmp_path):
    from typer.testing import CliRunner
    from esfex.cli import app

    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text("simulation_mode: development\n")
    orch = MagicMock()
    with patch("esfex.config.loader.load_config", return_value=MagicMock()), \
         patch("esfex.runner.Orchestrator", return_value=orch), \
         patch("esfex.logging_config.setup_console_logging"):
        CliRunner().invoke(app, ["run", "--config", str(cfg_path), "--years", "2",
                                 "--start-year", "2031",
                                 "--output", str(tmp_path / "out")])
    orch.run.assert_called_once_with(years=2, start_year=2031)


# ── #50: load_demand_data(year_to_load) ─────────────────────────────────────

def test_year_to_load_returns_that_years_rows(tmp_path):
    f = tmp_path / "d.csv"
    np.savetxt(f, np.arange(3 * H, dtype=float), delimiter=",")
    demand, hours, _, years, t = load_demand_data(f, year_to_load=2026)
    assert hours == H
    assert demand[0, 0] == H and demand[-1, 0] == 2 * H - 1
    assert years == [2025, 2026, 2027]
    assert t[0] == datetime(2026, 1, 1) and t[-1] == datetime(2026, 12, 31, 23)


def test_year_to_load_after_leap_year_is_8760_block(tmp_path):
    f = tmp_path / "d.csv"
    np.savetxt(f, np.arange(2 * H, dtype=float), delimiter=",")
    demand, hours, *_ = load_demand_data(f, date_start="01/01/2028 00:00",
                                         year_to_load=2029)
    assert hours == H and demand[0, 0] == H


def test_year_to_load_outside_data_raises(tmp_path):
    f = tmp_path / "d.csv"
    np.savetxt(f, np.ones(H), delimiter=",")
    with pytest.raises(ValueError, match="Year 2030 not in demand data"):
        load_demand_data(f, year_to_load=2030)


def test_demand_manager_uses_8760_blocks(tmp_path):
    f = tmp_path / "d.csv"
    np.savetxt(f, np.arange(2 * H, dtype=float), delimiter=",")
    mgr = DemandDataManager(f, date_start="01/01/2028 00:00")
    mgr.prepare_hdf5_storage()
    try:
        demand, hours, _, t = mgr.load_year_data(2029)
        assert hours == H and demand[0, 0] == H
        assert t[0] == datetime(2029, 1, 1)
        assert mgr.metadata["years"] == [2028, 2029]
    finally:
        mgr.cleanup()


def test_availability_8784_rows_loses_feb29(tmp_path):
    f = tmp_path / "a.csv"
    np.savetxt(f, np.linspace(0, 1, 8784), delimiter=",")
    a = load_availability_profile(f, num_nodes=1)
    assert a.shape == (H, 1)


# ── #48: dispatch uses the current year of a multi-year profile ─────────────

def _adapter(year, base_year, start_hour, hours):
    ps = PowerSystemAdapter.__new__(PowerSystemAdapter)
    ps.year, ps.base_year, ps.start_hour, ps.hours = year, base_year, start_hour, hours
    return ps


def test_dispatch_slices_current_year_of_multi_year_profile():
    profile = np.repeat(np.arange(3.0), H)[:, None]   # year k has value k
    assert _adapter(2027, 2025, 100, 24)._slice_availability(profile, 1)[0, 0] == 2.0
    assert _adapter(2026, 2025, 0, 24)._slice_availability(profile, 1)[0, 0] == 1.0


def test_dispatch_slice_with_coarser_resolution():
    profile = np.repeat(np.arange(2.0), H)[:, None]
    out = _adapter(2026, 2025, 300, 8)._slice_availability(profile, 3)
    assert out.shape == (8, 1) and (out == 1.0).all()


def test_dispatch_repeats_single_year_profile():
    profile = np.arange(float(H))[:, None]
    out = _adapter(2030, 2025, 50, 4)._slice_availability(profile, 1)
    np.testing.assert_array_equal(out[:, 0], [50, 51, 52, 53])
    out = _adapter(2030, 2025, 150, 4)._slice_availability(profile, 3)
    np.testing.assert_allclose(out[:, 0], [151, 154, 157, 160])


def test_dispatch_wraps_short_multi_year_profile_like_master():
    # Master indexes profiles by horizon hour modulo their length.
    profile = np.repeat(np.arange(2.0), H)[:, None]
    assert _adapter(2027, 2025, 0, 24)._slice_availability(profile, 1)[0, 0] == 0.0
    assert _adapter(2028, 2025, 0, 24)._slice_availability(profile, 1)[0, 0] == 1.0
