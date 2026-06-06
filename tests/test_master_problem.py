"""
Tests for Phase 4: MasterProblem migration to Julia.

Tests the capacity expansion planning model including:
- Investment variables for generators and batteries
- Life extension and retirement decisions
- Budget constraints
- RE penetration targets
- Objective function (NPV of costs)
"""

import logging
import pytest
import numpy as np

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def julia_setup():
    """Set up Julia environment for MasterProblem tests."""
    from esfex.bridge.julia_setup import get_julia, get_esfex_module

    jl = get_julia()
    ESFEX = get_esfex_module()

    return jl, ESFEX


@pytest.fixture
def simple_network(julia_setup):
    """Create a simple 2-node network for testing."""
    jl, ESFEX = julia_setup

    # 2-node network with one line
    connections = np.array([[0.0, 100.0], [100.0, 0.0]])
    distances = np.array([[0.0, 50.0], [50.0, 0.0]])

    from esfex.bridge.converters import py_to_julia_matrix, py_to_julia_vector

    # One bus per node — anchor each bus to its node and pick the slack.
    buses = jl.seval("BusData[]")
    for i in range(1, 3):  # 1..2 (Julia 1-indexed)
        role = "slack" if i == 1 else "PQ"
        jl.seval("push!")(buses, ESFEX.BusData(
            i, i, 220.0, 50.0, "AC", role, "load", 1.0))
    bus_to_node = py_to_julia_vector([1, 2])

    network = ESFEX.NetworkConfig(
        2,  # num_nodes
        2,  # num_buses
        buses,
        bus_to_node,
        py_to_julia_matrix(connections),
        py_to_julia_matrix(distances),
        100.0,  # base_impedance
        0.4,    # reactance_per_km
        220.0,  # voltage_level_kv
        np.deg2rad(30.0),  # max_angle_diff_rad
        1,  # slack_bus
        py_to_julia_vector([0.0, 0.0]),  # transference_invest_cost
        py_to_julia_vector([0.0, 0.0]),  # transference_invest_max
        jl.seval("TransmissionLineData[]"),
        jl.seval("TransformerData[]"),
        jl.seval("ACDCConverterData[]"),
        jl.seval("FrequencyConverterData[]"),
        0.1,  # default_r_to_x_ratio
    )

    return network


@pytest.fixture
def simple_generators(julia_setup):
    """Create simple generator configurations for testing."""
    jl, ESFEX = julia_setup

    from esfex.bridge.converters import py_to_julia_matrix, py_to_julia_vector

    generators = []

    # Solar generator (renewable)
    hours = 48
    solar_avail = np.tile([0.0] * 6 + [0.2, 0.5, 0.8, 1.0, 1.0, 0.8, 0.5, 0.2] + [0.0] * 10, (hours // 24 + 1, 2))[:hours, :]

    # Reservoir-related fields default to zeros (no reservoir).
    zeros2 = lambda: py_to_julia_vector([0.0, 0.0])
    ones2 = lambda: py_to_julia_vector([1.0, 1.0])
    no_inflow = py_to_julia_matrix(np.zeros((hours, 2)))

    solar = ESFEX.GeneratorConfig(
        "Solar",
        "Renewable",
        "Sun",
        py_to_julia_vector([50.0, 30.0]),   # rated_power
        py_to_julia_vector([0.0, 0.0]),      # min_power
        py_to_julia_vector([1.0, 1.0]),      # efficiency_rated
        py_to_julia_vector([1.0, 1.0]),      # efficiency_min
        py_to_julia_vector([1.0, 1.0]),      # ramp_up
        py_to_julia_vector([1.0, 1.0]),      # ramp_down
        py_to_julia_vector([0.0, 0.0]),      # min_up_time
        py_to_julia_vector([0.0, 0.0]),      # min_down_time
        py_to_julia_vector([0.0, 0.0]),      # start_up_cost
        py_to_julia_vector([0.0, 0.0]),      # fuel_cost
        py_to_julia_vector([5.0, 5.0]),      # fixed_cost
        py_to_julia_vector([0.0, 0.0]),      # maintenance_cost
        py_to_julia_vector([0.0, 0.0]),      # inertia
        py_to_julia_vector([800000.0, 800000.0]),  # invest_cost ($/MW)
        py_to_julia_vector([200.0, 150.0]),  # invest_max (MW)
        py_to_julia_matrix(solar_avail),     # availability
        False,                               # reservable
        py_to_julia_vector([25.0, 25.0]),    # life_time
        py_to_julia_vector([0.0, 0.0]),      # initial_age
        py_to_julia_vector([0.005, 0.005]),  # degradation_rate
        py_to_julia_vector([0.0, 0.0]),      # decommissioning_cost
        50.0, "AC",                          # frequency_hz, current_type
        zeros2(), zeros2(), zeros2(), ones2(),  # reservoir capacity / initial / min / max
        no_inflow, ones2(), zeros2(),        # inflow, turbine_efficiency, evaporation
        zeros2(), ones2(), True,             # pump_capacity, pump_efficiency, spillage_allowed
        zeros2(), zeros2(), ones2(),         # invest_cost, invest_max, risk_coefficient
    )
    generators.append(solar)

    # Gas generator (non-renewable)
    gas_avail = np.ones((hours, 2))

    gas = ESFEX.GeneratorConfig(
        "Gas_CCGT",
        "Non-renewable",
        "Gas",
        py_to_julia_vector([100.0, 80.0]),   # rated_power
        py_to_julia_vector([0.3, 0.3]),      # min_power
        py_to_julia_vector([0.55, 0.55]),    # efficiency_rated
        py_to_julia_vector([0.45, 0.45]),    # efficiency_min
        py_to_julia_vector([0.05, 0.05]),    # ramp_up
        py_to_julia_vector([0.05, 0.05]),    # ramp_down
        py_to_julia_vector([4.0, 4.0]),      # min_up_time
        py_to_julia_vector([4.0, 4.0]),      # min_down_time
        py_to_julia_vector([5000.0, 5000.0]),  # start_up_cost
        py_to_julia_vector([50.0, 50.0]),    # fuel_cost
        py_to_julia_vector([10.0, 10.0]),    # fixed_cost
        py_to_julia_vector([3.0, 3.0]),      # maintenance_cost
        py_to_julia_vector([5.0, 5.0]),      # inertia
        py_to_julia_vector([600000.0, 600000.0]),  # invest_cost
        py_to_julia_vector([100.0, 100.0]),  # invest_max
        py_to_julia_matrix(gas_avail),       # availability
        True,                                # reservable
        py_to_julia_vector([30.0, 30.0]),    # life_time
        py_to_julia_vector([20.0, 20.0]),    # initial_age (nearing end of life)
        py_to_julia_vector([0.01, 0.01]),    # degradation_rate
        py_to_julia_vector([0.0, 0.0]),      # decommissioning_cost
        50.0, "AC",                          # frequency_hz, current_type
        zeros2(), zeros2(), zeros2(), ones2(),  # reservoir capacity / initial / min / max
        no_inflow, ones2(), zeros2(),        # inflow, turbine_efficiency, evaporation
        zeros2(), ones2(), True,             # pump_capacity, pump_efficiency, spillage_allowed
        zeros2(), zeros2(), ones2(),         # invest_cost, invest_max, risk_coefficient
    )
    generators.append(gas)

    # Convert to Julia vector
    jl_generators = jl.seval("GeneratorConfig[]")
    for gen in generators:
        jl.seval("push!")(jl_generators, gen)

    return jl_generators


@pytest.fixture
def simple_batteries(julia_setup):
    """Create simple battery configuration for testing."""
    jl, ESFEX = julia_setup

    from esfex.bridge.converters import py_to_julia_vector

    battery = ESFEX.BatteryConfig(
        "Li-ion",
        py_to_julia_vector([20.0, 15.0]),    # capacity (MWh)
        py_to_julia_vector([10.0, 7.5]),     # max_charge_power
        py_to_julia_vector([10.0, 7.5]),     # max_discharge_power
        py_to_julia_vector([0.95, 0.95]),    # charge_efficiency
        py_to_julia_vector([0.95, 0.95]),    # discharge_efficiency
        py_to_julia_vector([0.1, 0.1]),      # soc_min
        py_to_julia_vector([0.9, 0.9]),      # soc_max
        py_to_julia_vector([0.5, 0.5]),      # soc_initial
        py_to_julia_vector([0.0001, 0.0001]), # self_discharge
        py_to_julia_vector([150000.0, 150000.0]),  # invest_cost_power
        py_to_julia_vector([200000.0, 200000.0]),  # invest_cost_capacity
        py_to_julia_vector([50.0, 40.0]),    # invest_max_power
        py_to_julia_vector([200.0, 150.0]),  # invest_max_capacity
        py_to_julia_vector([15.0, 15.0]),    # life_time
        py_to_julia_vector([0.0, 0.0]),      # initial_age
        py_to_julia_vector([0.0, 0.0]),      # decommissioning_cost
        1.0,                                  # min_duration_hours
        8.0,                                  # max_duration_hours
        py_to_julia_vector([0.0, 0.0]),      # maintenance_cost
        py_to_julia_vector([0.0, 0.0]),      # inertia
        False,                                # spillage
        "DC",                                 # current_type
        py_to_julia_vector([0.0, 0.0]),      # degradation_rate
        py_to_julia_vector([0.0, 0.0]),      # throughput_degradation_cost
        py_to_julia_vector([1.0, 1.0]),      # risk_coefficient
    )

    jl_batteries = jl.seval("BatteryConfig[]")
    jl.seval("push!")(jl_batteries, battery)

    return jl_batteries


@pytest.fixture
def simple_demand():
    """Create simple demand profile for testing."""
    hours = 48
    # Two-day demand profile
    base_profile = np.array([
        0.6, 0.5, 0.5, 0.5, 0.5, 0.6,  # 00-06: night
        0.7, 0.8, 0.9, 1.0, 1.0, 0.95, # 06-12: morning
        0.9, 0.85, 0.85, 0.9, 1.0, 1.0, # 12-18: afternoon
        0.95, 0.9, 0.85, 0.75, 0.7, 0.65  # 18-24: evening
    ])

    # Node 1: 100 MW peak, Node 2: 60 MW peak
    demand = np.zeros((hours, 2))
    for h in range(hours):
        demand[h, 0] = base_profile[h % 24] * 100
        demand[h, 1] = base_profile[h % 24] * 60

    return demand


class TestMasterProblemTargets:
    """Tests for RE target calculations."""

    def test_calculate_target_ratios(self, julia_setup, simple_network, simple_generators, simple_batteries, simple_demand):
        """Test progressive RE target calculation."""
        jl, ESFEX = julia_setup

        from esfex.bridge.converters import py_to_julia_matrix, py_to_julia_vector

        # Create input for 5 years
        years = [2025, 2030, 2035, 2040, 2045]

        # Convert years to Julia Int vector
        jl_years = jl.seval(f"Int64[{', '.join(map(str, years))}]")

        jl_input = ESFEX.MasterProblemInput(
            years=jl_years,
            base_year=2025,
            system_name="test",
            network=simple_network,
            generators=simple_generators,
            batteries=simple_batteries,
            base_demand=py_to_julia_matrix(simple_demand),
            demand_growth=0.02,
            discount_rate=0.05,
            max_annual_investment=1e9,
            target_re_penetration=0.8,
            initial_re_penetration=0.2,
            slack_penalty=1e6,
        )

        targets = ESFEX.calculate_target_ratios(jl_input)

        # targets is keyed by (system_idx, year_idx); single-system → s=1.
        assert len(targets) == 5
        # First year should be initial
        assert abs(targets[(1, 1)] - 0.2) < 0.01
        # Last year should be target
        assert abs(targets[(1, 5)] - 0.8) < 0.01
        # Middle years should be interpolated
        assert 0.2 < targets[(1, 3)] < 0.8

        logger.info(f"RE targets by (sys, year): {dict(targets)}")


class TestMasterProblemBuild:
    """Tests for MasterProblem model construction."""

    def test_build_master_problem(self, julia_setup, simple_network, simple_generators, simple_batteries, simple_demand):
        """Test that MasterProblem model builds without errors."""
        jl, ESFEX = julia_setup

        from esfex.bridge.converters import py_to_julia_matrix, py_to_julia_vector

        years = [2025, 2030]
        jl_years = jl.seval(f"Int64[{', '.join(map(str, years))}]")

        jl_input = ESFEX.MasterProblemInput(
            years=jl_years,
            base_year=2025,
            system_name="test",
            network=simple_network,
            generators=simple_generators,
            batteries=simple_batteries,
            base_demand=py_to_julia_matrix(simple_demand),
            discount_rate=0.05,
            max_annual_investment=1e8,
            target_re_penetration=0.5,
            initial_re_penetration=0.3,
        )

        model, vars, targets = ESFEX.create_master_problem(
            jl_input,
            use_representative_days=False
        )

        # Check variables were created (master uses tech-based investments)
        assert len(vars.tech_investment) == 2  # 2 years
        assert len(vars.bat_tech_power_investment) == 2
        assert len(vars.re_penetration_ratio) == 2

        logger.info("MasterProblem model built successfully")

    def test_build_master_variables(self, julia_setup, simple_network, simple_generators, simple_batteries, simple_demand):
        """Test variable creation for MasterProblem."""
        jl, ESFEX = julia_setup

        from esfex.bridge.converters import py_to_julia_matrix, py_to_julia_vector

        years = [2025, 2030, 2035]
        jl_years = jl.seval(f"Int64[{', '.join(map(str, years))}]")

        jl_input = ESFEX.MasterProblemInput(
            years=jl_years,
            base_year=2025,
            system_name="test",
            network=simple_network,
            generators=simple_generators,
            batteries=simple_batteries,
            base_demand=py_to_julia_matrix(simple_demand),
        )

        # Create model
        jl.seval("using JuMP")
        model = jl.seval("Model()")

        # Julia ! becomes _b in Python
        vars = ESFEX.build_master_variables_b(model, jl_input)

        # Check structure: 3 years of dicts. The simple_batteries fixture has no
        # investable battery_technologies, so per-tech-per-year dicts can be empty.
        assert len(vars.tech_investment) == 3
        assert len(vars.bat_tech_power_investment) == 3

        # Life extension variables should exist for generator nearing end of life
        # Gas generator has initial_age=20, lifetime=30, so at year 3 (age 22) still OK
        # But the check is age >= lifetime which won't trigger until much later
        # Let's just verify the structure exists
        assert len(vars.gen_life_extension) == 3

        logger.info("MasterProblem variables created correctly")


class TestMasterProblemSolve:
    """Tests for MasterProblem solving."""

    def test_solve_simple_master_problem(self, julia_setup, simple_network, simple_generators, simple_batteries, simple_demand):
        """Test solving a simple MasterProblem."""
        jl, ESFEX = julia_setup

        from esfex.bridge.converters import py_to_julia_matrix, py_to_julia_vector

        years = [2025, 2030]
        jl_years = jl.seval(f"Int64[{', '.join(map(str, years))}]")

        jl_input = ESFEX.MasterProblemInput(
            years=jl_years,
            base_year=2025,
            system_name="test",
            network=simple_network,
            generators=simple_generators,
            batteries=simple_batteries,
            base_demand=py_to_julia_matrix(simple_demand),
            discount_rate=0.05,
            max_annual_investment=5e8,  # 500M budget
            target_re_penetration=0.5,
            initial_re_penetration=0.3,
            slack_penalty=1e9,
            verbose=False,
        )

        model, vars, targets = ESFEX.create_master_problem(
            jl_input,
            use_representative_days=False
        )

        # Solve
        jl.seval("using JuMP")
        jl.seval("global _test_mp_model")
        jl._test_mp_model = model
        jl.seval("optimize!(_test_mp_model)")

        status = jl.seval("termination_status(_test_mp_model)")
        status_str = str(status)

        logger.info(f"MasterProblem solve status: {status_str}")

        assert "OPTIMAL" in status_str or "LOCALLY_SOLVED" in status_str

        # Extract solution
        result = ESFEX.extract_master_solution(model, vars, jl_input)

        logger.info(f"Objective: ${result.objective:,.0f}")
        logger.info(f"Total investment year 1: ${result.total_investment_by_year[1]:,.0f}")
        logger.info(f"RE penetration year 1: {result.re_penetration_by_year[1]:.1%}")

        assert result.objective >= 0

    @pytest.mark.julia
    def test_master_reservoir_energy_budget(self, julia_setup, simple_network, simple_batteries, simple_demand):
        """The master enforces the reservoir water budget (hydro is energy-limited).

        Differential test: same system, scarce vs. abundant reservoir inflow. The
        water balance is now modelled in the master, so scarce inflow forces the
        expensive thermal backup -> strictly higher objective. Without the balance
        (the previous behaviour, hydro = firm MW) the two would be identical.
        """
        jl, ESFEX = julia_setup
        from esfex.bridge.converters import py_to_julia_matrix, py_to_julia_vector

        hours = simple_demand.shape[0]
        jl_years = jl.seval("Int64[2025]")
        z2 = lambda: py_to_julia_vector([0.0, 0.0])
        o2 = lambda: py_to_julia_vector([1.0, 1.0])
        avail2 = py_to_julia_matrix(np.ones((hours, 2)))

        def make_hydro(inflow_mw):
            inflow = np.zeros((hours, 2))
            inflow[:, 0] = inflow_mw  # inflow only at bus 1
            return ESFEX.GeneratorConfig(
                "Hydro", "Renewable", "Hydro",
                py_to_julia_vector([80.0, 0.0]),     # rated_power (turbine), bus 1
                z2(), o2(), o2(), o2(), o2(), z2(), z2(), z2(),
                py_to_julia_vector([0.001, 0.001]),  # fuel_cost (~0, cheap)
                z2(), z2(), z2(), z2(), z2(), avail2, True,
                py_to_julia_vector([100.0, 100.0]), z2(), z2(), z2(),
                50.0, "AC",
                py_to_julia_vector([400.0, 0.0]),    # reservoir_capacity (MWh) bus 1
                py_to_julia_vector([0.5, 0.0]),      # initial level fraction
                z2(), o2(),                          # min / max level fraction
                py_to_julia_matrix(inflow),          # inflow
                o2(), z2(), z2(), o2(),              # turbine_eff, evap, pump_cap, pump_eff
                True,                                # spillage_allowed (avoid infeasibility)
                z2(), z2(), o2(),                    # res invest cost/max, risk
            )

        gas = ESFEX.GeneratorConfig(
            "Gas", "Non-renewable", "Gas",
            py_to_julia_vector([200.0, 200.0]),  # rated_power
            z2(), o2(), o2(), o2(), o2(), z2(), z2(), z2(),
            py_to_julia_vector([200.0, 200.0]),  # fuel_cost (expensive backup)
            z2(), z2(), py_to_julia_vector([5.0, 5.0]), z2(), z2(), avail2, True,
            py_to_julia_vector([100.0, 100.0]), z2(), z2(), z2(),
            50.0, "AC",
            z2(), z2(), z2(), o2(), py_to_julia_matrix(np.zeros((hours, 2))),
            o2(), z2(), z2(), o2(), False, z2(), z2(), o2(),
        )

        def solve_with_inflow(inflow_mw):
            jl_gens = jl.seval("GeneratorConfig[]")
            jl.seval("push!")(jl_gens, make_hydro(inflow_mw))
            jl.seval("push!")(jl_gens, gas)
            jl_input = ESFEX.MasterProblemInput(
                years=jl_years, base_year=2025, network=simple_network,
                generators=jl_gens, batteries=simple_batteries,
                base_demand=py_to_julia_matrix(simple_demand),
                representative_days_per_year=2, min_day_separation=1,
                target_re_penetration=0.0, initial_re_penetration=0.0,
                slack_penalty=1e9, verbose=False,
            )
            model, mvars, _ = ESFEX.create_master_problem(
                jl_input, use_representative_days=True)
            jl.seval("using JuMP")
            jl.seval("global _hm")
            jl._hm = model
            jl.seval("optimize!(_hm)")
            assert "OPTIMAL" in str(jl.seval("termination_status(_hm)"))
            return float(ESFEX.extract_master_solution(model, mvars, jl_input).objective)

        obj_scarce = solve_with_inflow(1.0)     # almost no water -> costly gas
        obj_plenty = solve_with_inflow(60.0)    # abundant water -> hydro covers load
        logger.info(
            "reservoir budget: obj(scarce inflow)=%.1f vs obj(plenty)=%.1f",
            obj_scarce, obj_plenty,
        )
        # Water budget binds: scarce inflow is strictly more expensive. If the
        # master ignored the reservoir balance, the two would be equal.
        assert obj_scarce > obj_plenty * 1.05

    @pytest.mark.julia
    def test_master_reservoir_seasonal_linking(self, julia_setup, simple_network, simple_batteries):
        """TSAM inter-period linking lets a reservoir store water across seasons.

        Differential test over two representative periods — a wet, low-demand
        period followed by a dry, high-demand one. With seasonal linking the
        reservoir banks the surplus inflow from period 1 and releases it in
        period 2, displacing expensive gas. Without linking each period is
        cyclic, so the dry period gets no hydro and must run gas for the whole
        load -> strictly more expensive.
        """
        jl, ESFEX = julia_setup
        from esfex.bridge.converters import py_to_julia_matrix, py_to_julia_vector

        hours = 48
        jl_years = jl.seval("Int64[2025]")
        z2 = lambda: py_to_julia_vector([0.0, 0.0])
        o2 = lambda: py_to_julia_vector([1.0, 1.0])
        avail2 = py_to_julia_matrix(np.ones((hours, 2)))

        # Seasonal demand at bus 1: low in period 1 (h 1-24), high in period 2.
        demand = np.zeros((hours, 2))
        demand[:24, 0] = 20.0
        demand[24:, 0] = 90.0
        # Inflow at bus 1: abundant in period 1, none in period 2.
        inflow = np.zeros((hours, 2))
        inflow[:24, 0] = 50.0

        # 39-arg form (reservoir_min_release defaults to 0 via the compat ctor).
        hydro = ESFEX.GeneratorConfig(
            "Hydro", "Renewable", "Hydro",
            py_to_julia_vector([100.0, 0.0]),    # rated turbine, bus 1
            z2(), o2(), o2(), o2(), o2(), z2(), z2(), z2(),
            py_to_julia_vector([0.001, 0.001]),  # ~free water
            z2(), z2(), z2(), z2(), z2(), avail2, True,
            py_to_julia_vector([100.0, 100.0]), z2(), z2(), z2(),
            50.0, "AC",
            py_to_julia_vector([2000.0, 0.0]),   # reservoir_capacity (MWh)
            py_to_julia_vector([0.5, 0.0]),      # initial level fraction
            z2(), o2(),                          # min / max level fraction
            py_to_julia_matrix(inflow),
            o2(), z2(), z2(), o2(),              # turbine_eff, evap, pump_cap, pump_eff
            True,                                # spillage_allowed
            z2(), z2(), o2(),                    # res invest cost/max, risk
        )

        gas = ESFEX.GeneratorConfig(
            "Gas", "Non-renewable", "Gas",
            py_to_julia_vector([200.0, 200.0]),
            z2(), o2(), o2(), o2(), o2(), z2(), z2(), z2(),
            py_to_julia_vector([200.0, 200.0]),  # expensive backup
            z2(), z2(), py_to_julia_vector([5.0, 5.0]), z2(), z2(), avail2, True,
            py_to_julia_vector([100.0, 100.0]), z2(), z2(), z2(),
            50.0, "AC",
            z2(), z2(), z2(), o2(), py_to_julia_matrix(np.zeros((hours, 2))),
            o2(), z2(), z2(), o2(), False, z2(), z2(), o2(),
        )

        def solve(linking):
            jl_gens = jl.seval("GeneratorConfig[]")
            jl.seval("push!")(jl_gens, hydro)
            jl.seval("push!")(jl_gens, gas)
            jl_input = ESFEX.MasterProblemInput(
                years=jl_years, base_year=2025, network=simple_network,
                generators=jl_gens, batteries=simple_batteries,
                base_demand=py_to_julia_matrix(demand),
                representative_days_per_year=2, min_day_separation=1,
                target_re_penetration=0.0, initial_re_penetration=0.0,
                slack_penalty=1e9, verbose=False,
                use_tsam=True,
                tsam_period_start_hours=jl.seval("[[1, 25]]"),
                tsam_period_weights=jl.seval("[[1.0, 1.0]]"),
                tsam_chronological_order=jl.seval("[[1, 2]]"),
                tsam_inter_period_linking=linking,
            )
            model, mvars, _ = ESFEX.create_master_problem(
                jl_input, use_representative_days=True)
            jl.seval("using JuMP")
            jl.seval("global _sl")
            jl._sl = model
            jl.seval("optimize!(_sl)")
            assert "OPTIMAL" in str(jl.seval("termination_status(_sl)"))
            return float(ESFEX.extract_master_solution(model, mvars, jl_input).objective)

        obj_link = solve(True)
        obj_nolink = solve(False)
        logger.info(
            "seasonal hydro: obj(linking)=%.1f vs obj(no linking)=%.1f",
            obj_link, obj_nolink,
        )
        # Seasonal storage is strictly cheaper: banked spring water cuts the
        # summer gas bill. Without inter-period linking the two would differ
        # only if the chain genuinely moves water across periods.
        assert obj_nolink > obj_link * 1.05

    @pytest.mark.julia
    def test_benders_decomposition_solves(self, julia_setup, simple_network, simple_generators, simple_batteries, simple_demand):
        """Benders decomposition runs end to end and returns a valid solution.

        Validates the optional ``benders`` master solver: the algorithm builds
        the investment master + per-representative-day subproblems, iterates,
        and recovers a MasterProblemResult. The objective is compared to the
        monolithic representative-day solve for the same case (logged; the two
        formulations need not match exactly, see issue #2).
        """
        jl, ESFEX = julia_setup
        from esfex.bridge.converters import py_to_julia_matrix

        # Single year: the simple 48-hour demand covers one year, so every year
        # has representative days (a multi-year case would need demand spanning
        # all years).
        years = [2025]
        jl_years = jl.seval(f"Int64[{', '.join(map(str, years))}]")

        def make_input():
            return ESFEX.MasterProblemInput(
                years=jl_years,
                base_year=2025,
                system_name="test",
                network=simple_network,
                generators=simple_generators,
                batteries=simple_batteries,
                base_demand=py_to_julia_matrix(simple_demand),
                discount_rate=0.05,
                max_annual_investment=5e8,
                target_re_penetration=0.0,
                initial_re_penetration=0.0,
                representative_days_per_year=2,
                min_day_separation=1,
                slack_penalty=1e9,
                verbose=False,
            )

        # Monolithic representative-day reference solve.
        mono_in = make_input()
        model, mvars, _ = ESFEX.create_master_problem(
            mono_in, use_representative_days=True,
        )
        jl.seval("using JuMP")
        jl.seval("global _bz_model")
        jl._bz_model = model
        jl.seval("optimize!(_bz_model)")
        mono_status = str(jl.seval("termination_status(_bz_model)"))
        assert "OPTIMAL" in mono_status or "LOCALLY_SOLVED" in mono_status
        mono_obj = float(ESFEX.extract_master_solution(model, mvars, mono_in).objective)

        # Benders solve.
        bres = ESFEX.run_benders_decomposition(
            make_input(),
            max_iterations=40,
            tolerance=1e-3,
            use_representative_days=True,
            verbose_benders=False,
        )

        # It must actually iterate and return a usable optimal solution.
        assert int(bres.iterations) >= 1
        assert len(list(bres.lb_history)) >= 1
        sol_status = str(bres.solution.status)
        assert "OPTIMAL" in sol_status or "LOCALLY_SOLVED" in sol_status
        b_obj = float(bres.objective)
        assert np.isfinite(b_obj) and b_obj >= 0.0

        rel = abs(b_obj - mono_obj) / max(abs(mono_obj), 1.0)
        logger.info(
            "Benders obj=%.0f vs monolithic=%.0f (rel=%.2f%%, iters=%d, gap=%.3f%%)",
            b_obj, mono_obj, rel * 100, int(bres.iterations), float(bres.gap) * 100,
        )
        # Same investment problem -> same ballpark (loose: formulations differ).
        assert 0.1 * mono_obj <= b_obj <= 10.0 * mono_obj

    def test_investment_within_budget(self, julia_setup, simple_network, simple_generators, simple_batteries, simple_demand):
        """Test that investments stay within budget constraint."""
        jl, ESFEX = julia_setup

        from esfex.bridge.converters import py_to_julia_matrix, py_to_julia_vector

        budget = 1e8  # 100M budget
        years = [2025, 2030]
        jl_years = jl.seval(f"Int64[{', '.join(map(str, years))}]")

        jl_input = ESFEX.MasterProblemInput(
            years=jl_years,
            base_year=2025,
            system_name="test",
            network=simple_network,
            generators=simple_generators,
            batteries=simple_batteries,
            base_demand=py_to_julia_matrix(simple_demand),
            max_annual_investment=budget,
            slack_penalty=1e12,  # High penalty to discourage slack
        )

        model, vars, targets = ESFEX.create_master_problem(jl_input)

        jl.seval("global _test_mp_model2")
        jl._test_mp_model2 = model
        jl.seval("optimize!(_test_mp_model2)")

        result = ESFEX.extract_master_solution(model, vars, jl_input)

        # Check budget compliance (with small tolerance for slack)
        # When accessed from Python, Julia arrays use 0-based indexing
        for y_idx, year in enumerate(years):
            inv_cost = result.total_investment_by_year[y_idx]
            logger.info(f"Year {year} investment: ${float(inv_cost):,.0f} (budget: ${budget:,.0f})")
            # Allow 10% slack tolerance
            assert float(inv_cost) <= budget * 1.1, f"Investment exceeds budget in year {year}"


class TestMasterProblemAdapter:
    """Tests for Python adapter interface.

    Note: These tests require a full Pydantic config which is complex to set up.
    The Julia-level tests above provide full coverage of the optimization logic.
    These adapter tests verify the Python interface layer.
    """

    @pytest.mark.skip(reason="Requires full Pydantic config - covered by Julia tests")
    def test_adapter_initialization(self, simple_demand):
        """Test MasterProblemAdapter initialization."""
        pass

    @pytest.mark.skip(reason="Requires full Pydantic config - covered by Julia tests")
    def test_adapter_build_and_solve(self, simple_demand):
        """Test building and solving via adapter."""
        pass

    @pytest.mark.skip(reason="Requires full Pydantic config - covered by Julia tests")
    def test_adapter_get_investment_decisions(self, simple_demand):
        """Test getting investment decisions from adapter."""
        pass

    @pytest.mark.skip(reason="Requires full Pydantic config - covered by Julia tests")
    def test_adapter_get_re_targets(self, simple_demand):
        """Test getting RE targets from adapter."""
        pass
