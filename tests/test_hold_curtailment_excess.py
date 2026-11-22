"""MGA and SPORES hold every curtailment excess of the cost-optimal plan.

``hold_curtailment_excess!`` read each excess's value inside the loop that
bounds them. Setting the first bound changes the model, which discards its
solution, so with two or more excesses (two systems, or two years) the next
``value`` raised ``OptimizeNotCalled`` and the master failed before any
alternative.
"""
import pytest


@pytest.mark.julia
def test_every_excess_is_held_at_its_optimal_value():
    from esfex.bridge.julia_setup import get_esfex_module, get_julia

    jl = get_julia()
    get_esfex_module()
    jl.seval("using JuMP, HiGHS")
    held = jl.seval("""
    let
        m = Model(HiGHS.Optimizer); set_silent(m)
        @variable(m, x[1:2] >= 0)
        @variable(m, re[1:2] >= 0)
        @constraint(m, x[1] >= 3); @constraint(m, x[2] >= 5)
        @constraint(m, re[1] == 100); @constraint(m, re[2] == 200)
        @objective(m, Min, sum(x))
        m.ext[:curtailment_excess] = [
            ESFEX.CurtailmentExcess(0, "A", 2025, 0.05, x[1], AffExpr(0.0, re[1] => 1.0), 0.05, 1.0),
            ESFEX.CurtailmentExcess(0, "B", 2025, 0.05, x[2], AffExpr(0.0, re[2] => 1.0), 0.05, 1.0),
        ]
        optimize!(m)
        ESFEX.hold_curtailment_excess!(m)
        [upper_bound(x[1]) - 3 - ESFEX.curtailment_tolerance(100 * 0.05),
         upper_bound(x[2]) - 5 - ESFEX.curtailment_tolerance(200 * 0.05)]
    end
    """)
    assert [round(float(v), 9) for v in held] == [0.0, 0.0]
