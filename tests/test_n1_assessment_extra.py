"""Additive coverage tests for esfex.analysis.n1_assessment.

These tests drive the IntegratedN1Analyzer branches using lightweight fake
analyzers plus the real ContingencyResult / ACContingencyResult /
FrequencyResponse dataclasses, so all real logic in the module executes.
"""
from __future__ import annotations

import pytest

from esfex.analysis.contingency import (
    ContingencyAnalyzer,
    ContingencyResult,
)
from esfex.analysis.ac_contingency import (
    ACContingencyAnalyzer,
    ACContingencyResult,
)
from esfex.analysis.frequency import FrequencyResponse
from esfex.analysis.n1_assessment import (
    IntegratedN1Analyzer,
    N1SecurityAssessment,
    _DEFAULT_V_MIN,
    _DEFAULT_V_MAX,
)


# ── Fakes ──────────────────────────────────────────────────────────────────


def _make_result(**kw) -> ContingencyResult:
    base = dict(
        contingency_type="generator",
        element_id="G1",
        element_description="",
    )
    base.update(kw)
    return ContingencyResult(**base)


def _make_ac_result(**kw) -> ACContingencyResult:
    base = dict(
        contingency_type="line",
        element_id="L1",
        element_description="",
    )
    base.update(kw)
    return ACContingencyResult(**base)


def _make_freq(**kw) -> FrequencyResponse:
    base = dict(
        delta_p_mw=100.0,
        h_total_mws=500.0,
        rocof_hz_per_s=0.5,
        nadir_hz=49.5,
        steady_state_hz=49.8,
        t_nadir_s=2.0,
        d_total_mw_per_hz=10.0,
        is_stable=True,
        rocof_ok=True,
    )
    base.update(kw)
    return FrequencyResponse(**base)


class FakeDCAnalyzer(ContingencyAnalyzer):
    """Stand-in for ContingencyAnalyzer; not an ACContingencyAnalyzer."""

    def __init__(self, result=None, contingency_list=None):
        self._result = result if result is not None else _make_result()
        self._list = contingency_list if contingency_list is not None else []

    def analyze_generator_loss(self, snapshot, element_id):
        return self._result

    def analyze_line_loss(self, snapshot, element_id):
        return self._result

    def get_contingency_list(self, snapshot):
        return list(self._list)


class FakeACAnalyzer(ACContingencyAnalyzer):
    """Stand-in for ACContingencyAnalyzer so isinstance(_is_ac) is True."""

    def __init__(self, result=None, contingency_list=None):
        self._result = result if result is not None else _make_ac_result()
        self._list = contingency_list if contingency_list is not None else []

    def analyze_generator_loss(self, snapshot, element_id):
        return self._result

    def analyze_line_loss(self, snapshot, element_id):
        return self._result

    def get_contingency_list(self, snapshot):
        return list(self._list)


class FakeFreqAnalyzer:
    def __init__(self, response=None, nadir_limit=49.0, rocof_limit=1.0):
        self._response = response if response is not None else _make_freq()
        self.nadir_limit = nadir_limit
        self.rocof_limit = rocof_limit

    def analyze(self, snapshot, delta_p):
        return self._response


class FakePFResult:
    def __init__(self, converged=True, bus_vm_pu=None):
        self.converged = converged
        self.bus_vm_pu = bus_vm_pu or {}


class FakeBridge:
    def __init__(self, pf_result=None, raise_exc=False):
        self._pf = pf_result
        self._raise = raise_exc

    def rerun_power_flow(self):
        if self._raise:
            raise RuntimeError("boom")
        return self._pf


# ── _run_electrical dispatch ──────────────────────────────────────────────


def test_run_electrical_unknown_type_returns_placeholder():
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    res = az._run_electrical({}, "wibble", "X9")
    assert res.contingency_type == "wibble"
    assert res.element_id == "X9"
    assert "Unknown type" in res.element_description


def test_run_electrical_line_path():
    res = _make_result(contingency_type="line", element_id="L7")
    az = IntegratedN1Analyzer(FakeDCAnalyzer(result=res))
    out = az._run_electrical({}, "transformer", "L7")
    assert out is res


# ── _get_lost_power ────────────────────────────────────────────────────────


def test_get_lost_power_generator():
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    snap = {"generators": {"G1": {"output_mw": 42.0}}}
    assert az._get_lost_power(snap, "generator", "G1") == 42.0


def test_get_lost_power_generator_missing_defaults_zero():
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    assert az._get_lost_power({}, "generator", "G1") == 0.0


def test_get_lost_power_battery_net_injection():
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    snap = {"batteries": {"B1": {"discharge_mw": 30.0, "charge_mw": 5.0}}}
    assert az._get_lost_power(snap, "battery", "B1") == 25.0


def test_get_lost_power_battery_charging_clamped_zero():
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    snap = {"batteries": {"B1": {"discharge_mw": 0.0, "charge_mw": 10.0}}}
    assert az._get_lost_power(snap, "battery", "B1") == 0.0


def test_get_lost_power_unknown_type_zero():
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    assert az._get_lost_power({}, "line", "L1") == 0.0


# ── _find_worst_voltage ──────────────────────────────────────────────────────


def test_find_worst_voltage_under_and_over():
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    viols = [
        {"vm_pu": 0.90, "type": "under"},
        {"vm_pu": 1.12, "type": "over"},
        {"vm_pu": 0.85, "type": "under"},
    ]
    # NOTE: _find_worst_voltage keeps a single running `worst` that is
    # alternately min()'d (under) and max()'d (over) across the iteration.
    # The trailing under entry min()s the already-raised 1.12 down to 0.85,
    # so the observed result is 0.85 (not 1.12). This documents current
    # behavior; mixing under/over in one accumulator is arguably a quirk.
    assert az._find_worst_voltage(viols) == 0.85


def test_find_worst_voltage_only_under():
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    assert az._find_worst_voltage([{"vm_pu": 0.80, "type": "under"}]) == 0.80


# ── _run_voltage_check ──────────────────────────────────────────────────────


def test_voltage_check_under_and_over():
    pf = FakePFResult(
        converged=True,
        bus_vm_pu={"b1": 0.90, "b2": 1.10, "b3": 1.00},
    )
    az = IntegratedN1Analyzer(FakeDCAnalyzer(), ac_bridge=FakeBridge(pf))
    viols = az._run_voltage_check({}, "line", "L1")
    types = {v["bus_id"]: v["type"] for v in viols}
    assert types == {"b1": "under", "b2": "over"}
    assert all("vm_pu" in v for v in viols)


def test_voltage_check_diverged_returns_empty():
    pf = FakePFResult(converged=False)
    az = IntegratedN1Analyzer(FakeDCAnalyzer(), ac_bridge=FakeBridge(pf))
    assert az._run_voltage_check({}, "line", "L1") == []


def test_voltage_check_exception_returns_empty():
    az = IntegratedN1Analyzer(FakeDCAnalyzer(), ac_bridge=FakeBridge(raise_exc=True))
    assert az._run_voltage_check({}, "line", "L1") == []


# ── _compute_severity ──────────────────────────────────────────────────────


def test_compute_severity_thermal_only():
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    score = az._compute_severity(20.0, 0.0, {}, None, [])
    assert score == 20.0


def test_compute_severity_load_shed_fraction():
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    snap = {"loads": {"L1": {"demand_mw": 100.0}}}
    # 50 MW shed of 100 demand -> +50; plus 10 overload
    score = az._compute_severity(10.0, 50.0, snap, None, [])
    assert score == pytest.approx(60.0)


def test_compute_severity_zero_demand_skips_shed_term():
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    score = az._compute_severity(5.0, 50.0, {"loads": {}}, None, [])
    assert score == 5.0


def test_compute_severity_frequency_nadir_and_rocof():
    freq = FakeFreqAnalyzer(nadir_limit=49.0, rocof_limit=1.0)
    az = IntegratedN1Analyzer(FakeDCAnalyzer(), frequency_analyzer=freq)
    fr = _make_freq(nadir_hz=48.0, rocof_hz_per_s=1.5, rocof_ok=False)
    score = az._compute_severity(0.0, 0.0, {}, fr, [])
    # nadir term: (49-48)*20=20 ; rocof term: |1.5-1.0|*10=5
    assert score == pytest.approx(25.0)


def test_compute_severity_frequency_within_limits_no_contribution():
    freq = FakeFreqAnalyzer(nadir_limit=49.0, rocof_limit=1.0)
    az = IntegratedN1Analyzer(FakeDCAnalyzer(), frequency_analyzer=freq)
    fr = _make_freq(nadir_hz=49.5, rocof_hz_per_s=0.2, rocof_ok=True)
    assert az._compute_severity(0.0, 0.0, {}, fr, []) == 0.0


def test_compute_severity_voltage_under_and_over():
    az = IntegratedN1Analyzer(
        FakeDCAnalyzer(), v_min=_DEFAULT_V_MIN, v_max=_DEFAULT_V_MAX,
    )
    viols = [
        {"vm_pu": 0.90, "type": "under"},  # (0.95-0.90)*100 = 5
        {"vm_pu": 1.10, "type": "over"},   # (1.10-1.05)*100 = 5
    ]
    assert az._compute_severity(0.0, 0.0, {}, None, viols) == pytest.approx(10.0)


# ── _build_contingency_list ──────────────────────────────────────────────────


def test_build_contingency_list_adds_discharging_battery_and_sorts():
    base = [
        {"type": "generator", "element_id": "G1", "impact_mw": 10.0},
        {"type": "line", "element_id": "L1", "impact_mw": 5.0},
    ]
    snap = {
        "batteries": {
            "B1": {"discharge_mw": 50.0, "charge_mw": 0.0},  # net 50 -> added
            "B2": {"discharge_mw": 0.0, "charge_mw": 20.0},  # net -20 -> skipped
            "B3": {"discharge_mw": 0.05, "charge_mw": 0.0},  # net 0.05 <=0.1 skip
            "G1": {"discharge_mw": 99.0, "charge_mw": 0.0},  # id collision skip
        },
    }
    az = IntegratedN1Analyzer(FakeDCAnalyzer(contingency_list=base))
    out = az._build_contingency_list(snap)
    ids = [c["element_id"] for c in out]
    assert "B1" in ids
    assert "B2" not in ids
    assert "B3" not in ids
    # B1 has impact 50 -> sorted first
    assert out[0]["element_id"] == "B1"
    assert out[0]["type"] == "battery"


# ── assess_single full integration paths ──────────────────────────────────────


def test_assess_single_secure_dc():
    res = _make_result(
        contingency_type="generator",
        element_id="G1",
        element_description="Loss of G1",
        overloaded_lines=[],
        total_load_shed_mw=0.0,
        max_overload_pct=0.0,
    )
    az = IntegratedN1Analyzer(FakeDCAnalyzer(result=res))
    a = az.assess_single({}, "generator", "G1")
    assert a.is_secure is True
    assert a.binding_constraint == "none"
    assert a.severity_score == 0.0
    assert a.description == "Loss of G1"


def test_assess_single_thermal_binding():
    res = _make_result(
        overloaded_lines=[{"line_id": "x"}],
        total_load_shed_mw=0.0,
        max_overload_pct=30.0,
    )
    az = IntegratedN1Analyzer(FakeDCAnalyzer(result=res))
    a = az.assess_single({}, "line", "L1")
    assert a.has_thermal_violations is True
    assert a.binding_constraint == "thermal"
    assert a.is_secure is False


def test_assess_single_load_shed_binding():
    res = _make_result(
        overloaded_lines=[],
        total_load_shed_mw=20.0,
        max_overload_pct=0.0,
    )
    snap = {"loads": {"L1": {"demand_mw": 200.0}}}
    az = IntegratedN1Analyzer(FakeDCAnalyzer(result=res))
    a = az.assess_single(snap, "generator", "G1")
    assert a.has_load_shedding is True
    assert a.binding_constraint == "thermal"


def test_assess_single_frequency_binding():
    res = _make_result(overloaded_lines=[], total_load_shed_mw=0.0, max_overload_pct=0.0)
    fr = _make_freq(is_stable=False, rocof_ok=True, nadir_hz=48.0, rocof_hz_per_s=0.3)
    freq = FakeFreqAnalyzer(response=fr, nadir_limit=49.0, rocof_limit=1.0)
    az = IntegratedN1Analyzer(FakeDCAnalyzer(result=res), frequency_analyzer=freq)
    snap = {"generators": {"G1": {"output_mw": 100.0}}}
    a = az.assess_single(snap, "generator", "G1")
    assert a.has_frequency_violation is True
    assert a.binding_constraint == "frequency"
    assert a.rocof_hz_per_s == 0.3
    assert a.nadir_hz == 48.0
    assert a.frequency is fr


def test_assess_single_frequency_skipped_when_delta_p_zero():
    res = _make_result(overloaded_lines=[], total_load_shed_mw=0.0, max_overload_pct=0.0)
    freq = FakeFreqAnalyzer()
    az = IntegratedN1Analyzer(FakeDCAnalyzer(result=res), frequency_analyzer=freq)
    # no generators -> lost power 0 -> analyze not called -> no freq response
    a = az.assess_single({}, "generator", "G1")
    assert a.frequency is None
    assert a.has_frequency_violation is False


def test_assess_single_ac_voltage_binding():
    res = _make_ac_result(
        contingency_type="line",
        element_id="L1",
        overloaded_lines=[],
        total_load_shed_mw=0.0,
        max_overload_pct=0.0,
        voltage_violations=[{"bus_id": "b1", "vm_pu": 0.90, "type": "under"}],
    )
    az = IntegratedN1Analyzer(FakeACAnalyzer(result=res))
    a = az.assess_single({}, "line", "L1")
    assert a.has_voltage_violation is True
    assert a.binding_constraint == "voltage"
    assert a.worst_voltage_pu == 0.90
    assert a.is_secure is False


def test_assess_single_ac_no_voltage_violations():
    res = _make_ac_result(
        overloaded_lines=[],
        total_load_shed_mw=0.0,
        max_overload_pct=0.0,
        voltage_violations=[],
    )
    az = IntegratedN1Analyzer(FakeACAnalyzer(result=res))
    a = az.assess_single({}, "line", "L1")
    assert a.has_voltage_violation is False
    assert a.worst_voltage_pu == 1.0
    assert a.is_secure is True


def test_assess_single_voltage_via_bridge_for_dc():
    res = _make_result(overloaded_lines=[], total_load_shed_mw=0.0, max_overload_pct=0.0)
    pf = FakePFResult(converged=True, bus_vm_pu={"b1": 0.88})
    az = IntegratedN1Analyzer(FakeDCAnalyzer(result=res), ac_bridge=FakeBridge(pf))
    a = az.assess_single({}, "generator", "G1")
    assert a.has_voltage_violation is True
    assert a.binding_constraint == "voltage"
    assert a.worst_voltage_pu == 0.88


def test_assess_single_default_description_when_empty():
    res = _make_result(element_description="")
    az = IntegratedN1Analyzer(FakeDCAnalyzer(result=res))
    a = az.assess_single({}, "generator", "GX")
    assert a.description == "Loss of generator GX"


# ── assess_all ──────────────────────────────────────────────────────────────


def test_assess_all_sorts_and_limits():
    clist = [
        {"type": "generator", "element_id": "G1", "impact_mw": 5.0},
        {"type": "line", "element_id": "L1", "impact_mw": 3.0},
        {"type": "line", "element_id": "L2", "impact_mw": 1.0},
    ]
    # Different per-element results would be ideal; here all share one result
    res = _make_result(max_overload_pct=10.0)
    az = IntegratedN1Analyzer(FakeDCAnalyzer(result=res, contingency_list=clist))
    out = az.assess_all({}, max_contingencies=2)
    assert len(out) == 2
    # sorted by severity descending (all equal -> stable, but is a list)
    scores = [a.severity_score for a in out]
    assert scores == sorted(scores, reverse=True)


def test_assess_all_unlimited_and_handles_exception():
    clist = [
        {"type": "generator", "element_id": "G1", "impact_mw": 5.0},
        {"type": "generator", "element_id": "BAD", "impact_mw": 4.0},
    ]

    class BoomAnalyzer(FakeDCAnalyzer):
        def analyze_generator_loss(self, snapshot, element_id):
            if element_id == "BAD":
                raise ValueError("kaboom")
            return _make_result(element_id=element_id)

    az = IntegratedN1Analyzer(BoomAnalyzer(contingency_list=clist))
    out = az.assess_all({}, max_contingencies=0)
    # BAD raised and was logged/skipped; only G1 survives
    ids = [a.element_id for a in out]
    assert ids == ["G1"]


# ── get_security_summary ──────────────────────────────────────────────────────


def test_security_summary_empty():
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    summary = az.get_security_summary([])
    assert summary["total_contingencies"] == 0
    assert summary["secure_count"] == 0
    assert summary["insecure_count"] == 0
    assert summary["worst_contingency"] == ""
    assert summary["worst_score"] == 0.0
    assert summary["binding_constraints"] == {}


def test_security_summary_mixed():
    a_secure = N1SecurityAssessment(
        element_id="S1", element_type="line", description="",
        electrical=_make_result(), is_secure=True, binding_constraint="none",
        severity_score=0.0,
    )
    a_thermal = N1SecurityAssessment(
        element_id="T1", element_type="line", description="",
        electrical=_make_result(), is_secure=False,
        has_thermal_violations=True, binding_constraint="thermal",
        severity_score=30.0,
    )
    a_freq = N1SecurityAssessment(
        element_id="F1", element_type="generator", description="",
        electrical=_make_result(), is_secure=False,
        has_frequency_violation=True, binding_constraint="frequency",
        severity_score=80.0,
    )
    a_volt = N1SecurityAssessment(
        element_id="V1", element_type="line", description="",
        electrical=_make_result(), is_secure=False,
        has_voltage_violation=True, has_load_shedding=True,
        binding_constraint="voltage", severity_score=15.0,
    )
    az = IntegratedN1Analyzer(FakeDCAnalyzer())
    s = az.get_security_summary([a_secure, a_thermal, a_freq, a_volt])
    assert s["total_contingencies"] == 4
    assert s["secure_count"] == 1
    assert s["insecure_count"] == 3
    # thermal_count counts thermal violations OR load shedding (T1 + V1)
    assert s["thermal_violations"] == 2
    assert s["frequency_violations"] == 1
    assert s["voltage_violations"] == 1
    assert s["worst_contingency"] == "F1"
    assert s["worst_score"] == 80.0
    assert s["binding_constraints"] == {
        "none": 1, "thermal": 1, "frequency": 1, "voltage": 1,
    }


# ── constructor flags ──────────────────────────────────────────────────────


def test_is_ac_flag_detection():
    dc = IntegratedN1Analyzer(FakeDCAnalyzer())
    ac = IntegratedN1Analyzer(FakeACAnalyzer())
    assert dc._is_ac is False
    assert ac._is_ac is True
