# Native Julia test suite for the ESFEX optimization core.
#
# Run with:  julia --project=src/esfex/julia -e 'using Pkg; Pkg.test(coverage=true)'
# (or `using ESFEX` after activating + instantiating the project).
#
# These are unit tests for the pure / self-contained pieces of the Julia core:
# linear-algebra builders (Y-bus, incidence matrix), AC power-flow flow
# equations, the solver factory, temporal/period mappings, NPV finance, RE-target
# interpolation, representative-day selection, and the plain data structs.
#
# Integration tests that build and solve full optimization models live in the
# Python `julia`-marked suite (tests/test_power_system_parity.py, etc.); those
# exercise create_power_system / build_*! / add_*! / extract_* end to end, so
# they are intentionally NOT duplicated here.

using Test
using ESFEX
using JuMP: optimize!, termination_status, value, constraint_by_name, objective_value,
             coefficient, objective_function, all_variables, name
# Pre-import Ipopt into Main so create_optimizer's lazy ("ipopt") path resolves
# the optimizer type without the world-age @eval import (see _load_solver_module).
import Ipopt

@testset "ESFEX" begin

    @testset "version" begin
        @test ESFEX.version() == "0.1.0"
        @test ESFEX.version() isa String
    end

    @testset "hours_in_year" begin
        @test ESFEX.HOURS_STD_YEAR == 8760
        @test ESFEX.hours_in_year(2025) == 8760  # common year
        # Leap years are modelled as 8760 h (data come in 8760-h blocks)
        @test ESFEX.hours_in_year(2024) == 8760
        @test ESFEX.hours_in_year(2000) == 8760
        @test ESFEX.hours_in_year(1900) == 8760
    end

    @testset "parse_acopf_formulation" begin
        @test parse_acopf_formulation("acopf_soc") isa ESFEX.SOCFormulation
        @test parse_acopf_formulation("acopf_qc") isa ESFEX.QCFormulation
        @test parse_acopf_formulation("acopf_sdp") isa ESFEX.SDPFormulation
        @test parse_acopf_formulation("acopf_polar") isa ESFEX.PolarNLPFormulation
        @test parse_acopf_formulation("acopf_rect") isa ESFEX.RectNLPFormulation
        @test_throws ErrorException parse_acopf_formulation("not_a_mode")
        # Every result is a concrete subtype of the formulation abstract type.
        for m in ("acopf_soc", "acopf_qc", "acopf_sdp", "acopf_polar", "acopf_rect")
            @test parse_acopf_formulation(m) isa ESFEX.ACOPFFormulation
        end
    end

    @testset "build_incidence_matrix" begin
        # Two lines on a 3-bus path: 1->2, 2->3.
        K = build_incidence_matrix(3, [(1, 2), (2, 3)])
        @test size(K) == (3, 2)
        # +1 leaving the "from" bus, -1 entering the "to" bus.
        @test K[1, 1] == 1.0 && K[2, 1] == -1.0
        @test K[2, 2] == 1.0 && K[3, 2] == -1.0
        # Bus not on a line has no entry; every column is balanced.
        @test K[3, 1] == 0.0
        @test all(==(0.0), sum(K, dims = 1))
        # No lines -> an (n, 0) matrix.
        @test size(build_incidence_matrix(4, Tuple{Int,Int}[])) == (4, 0)

        # A small mesh: every column has exactly one +1 and one -1, all
        # entries are in {-1, 0, +1}, and there are exactly 2 nnz per line.
        K2 = build_incidence_matrix(4, [(1, 2), (2, 3), (1, 4), (3, 4)])
        @test size(K2) == (4, 4)
        @test all(x -> x in (-1.0, 0.0, 1.0), K2)
        @test count(!=(0.0), K2) == 2 * 4
        for col in 1:4
            @test count(==(1.0), K2[:, col]) == 1
            @test count(==(-1.0), K2[:, col]) == 1
        end
    end

    # A helper to spell out the 12 positional fields of TransmissionLineData
    # once; keeps the flow/Ybus tests readable.
    line(; from = 1, to = 2, x = 0.1, r = 0.01, b = 0.0, circuits = 1) =
        TransmissionLineData("L", from, to, 100.0, x, r, b, 10.0, 220.0,
                             circuits, 50.0, "AC")

    @testset "build_ybus" begin
        # Single lossy line, no shunt: Y is the textbook 2x2 series-admittance
        # stamp. Diagonals carry +y_series, off-diagonals -y_series.
        ys = 1.0 / complex(0.01, 0.1)
        Y = build_ybus([line()], TransformerData[], 2)
        @test size(Y) == (2, 2)
        @test Y[1, 1] ≈ ys
        @test Y[2, 2] ≈ ys
        @test Y[1, 2] ≈ -ys
        @test Y[2, 1] ≈ -ys
        @test Y ≈ permutedims(Y)              # symmetric for a passive line
        @test imag(Y[1, 1]) < 0               # inductive series branch

        # Line shunt susceptance adds +j*b/2 to each diagonal only.
        Yb = build_ybus([line(b = 0.06)], TransformerData[], 2)
        @test Yb[1, 1] ≈ ys + im * 0.03
        @test Yb[2, 2] ≈ ys + im * 0.03
        @test Yb[1, 2] ≈ -ys                  # off-diagonals unaffected by shunt

        # Parallel circuits halve the effective series impedance, so the
        # series admittance doubles relative to a single circuit.
        Yp = build_ybus([line(x = 0.2, r = 0.02, circuits = 2)],
                        TransformerData[], 2)
        @test Yp[1, 2] ≈ -1.0 / complex(0.01, 0.1)

        # Zero-impedance lines are skipped (matrix stays all-zero).
        Yz = build_ybus([line(x = 0.0, r = 0.0)], TransformerData[], 2)
        @test all(==(0.0 + 0.0im), Yz)

        # No branches at all -> an explicit zero matrix of the right size.
        @test all(==(0.0 + 0.0im), build_ybus(TransmissionLineData[],
                                              TransformerData[], 3))
    end

    @testset "classify_buses" begin
        # With no generators, every bus is PQ (type 1) except the slack (3).
        types = classify_buses(zeros(Float64, 0, 4), 4, GeneratorConfig[], 2)
        @test types == [1, 3, 1, 1]
        @test types[2] == 3
        @test all(t -> t in (1, 2, 3), types)
        # Slack on the last bus.
        @test classify_buses(zeros(Float64, 0, 3), 3, GeneratorConfig[], 3) ==
              [1, 1, 3]
    end

    @testset "calculate_line_flows" begin
        # Flat voltage, zero angle difference: no active flow and no losses.
        pf, pt, qf, qt, pl, ql =
            calculate_line_flows([1.0, 1.0], [0.0, 0.0], [line()],
                                 TransformerData[], 100.0)
        @test length(pf) == length(pt) == length(pl) == 1
        @test pf[1] ≈ 0.0 atol = 1e-9
        @test pt[1] ≈ 0.0 atol = 1e-9
        @test pl[1] ≈ 0.0 atol = 1e-9

        # Lossless line (r = 0) at a positive angle difference: power leaves
        # the leading-angle bus and the two ends cancel (zero real loss).
        llf = calculate_line_flows([1.0, 1.0], [0.1, 0.0], [line(r = 0.0)],
                                   TransformerData[], 100.0)
        p_from, p_to, p_loss = llf[1][1], llf[2][1], llf[5][1]
        @test p_from > 0.0                    # flows from bus 1 (higher θ)
        @test p_loss ≈ 0.0 atol = 1e-6        # no resistance -> no loss

        # Resistive line dissipates positive real power.
        lossy = calculate_line_flows([1.0, 1.0], [0.1, 0.0], [line(r = 0.01)],
                                     TransformerData[], 100.0)
        @test lossy[5][1] > 0.0

        # Results scale linearly with the MVA base.
        a = calculate_line_flows([1.0, 1.0], [0.1, 0.0], [line(r = 0.01)],
                                 TransformerData[], 100.0)
        b = calculate_line_flows([1.0, 1.0], [0.1, 0.0], [line(r = 0.01)],
                                 TransformerData[], 200.0)
        @test b[1][1] ≈ 2 * a[1][1]
        @test b[5][1] ≈ 2 * a[5][1]
    end

    @testset "create_optimizer" begin
        # Default + named solvers return an MOI optimizer factory (no solve).
        @test create_optimizer() isa ESFEX.MOI.OptimizerWithAttributes
        @test create_optimizer(solver_name = "highs") isa
              ESFEX.MOI.OptimizerWithAttributes
        # Solver name is case-insensitive (lowercased internally).
        @test create_optimizer(solver_name = "HIGHS") isa
              ESFEX.MOI.OptimizerWithAttributes
        # User options merge in without error.
        @test create_optimizer(solver_name = "highs", threads = 1, gap = 1e-4,
                               solver_options = Dict{String,Any}("solver" => "simplex")) isa
              ESFEX.MOI.OptimizerWithAttributes
        # Unknown solver names are rejected.
        @test_throws ErrorException create_optimizer(solver_name = "no_such_solver")
    end

    @testset "_compute_year_target" begin
        f = ESFEX._compute_year_target
        # First year always returns the initial penetration.
        @test f(1, 5, 0.2, 0.8, 0.0, 1.0, 0.0) == 0.2
        # Single-year horizon (reached only when y_idx != 1) jumps to target.
        @test f(2, 1, 0.2, 0.8, 0.0, 1.0, 0.0) == 0.8
        # Unconstrained linear interpolation: midpoint of 0.2 -> 0.8 over 5y.
        @test f(3, 5, 0.2, 0.8, 0.0, 1.0, 0.35) ≈ 0.5
        # Minimum-increment floor binds (raw step 0.05 < min 0.2).
        @test f(3, 5, 0.2, 0.8, 0.2, 1.0, 0.45) ≈ 0.65
        # Maximum-increment cap binds (raw step 0.5 > max 0.1).
        @test f(3, 5, 0.2, 0.8, 0.0, 0.1, 0.0) ≈ 0.1
    end

    @testset "select_representative_days" begin
        # Three days of flat demand with a single spike inside day 2 (the peak).
        demand = ones(Float64, 72, 1)
        demand[30, 1] = 100.0  # hour 30 lies in day 2 (hours 25:48)

        # num_days = 1 -> exactly the peak day's start index (1 + 1*24 = 25).
        @test select_representative_days(demand, 1, 1, 1, 24, 72) == [25]

        # num_days = 3 -> peak day plus one per remaining segment; all three
        # day-start indices, peak first.
        sel = select_representative_days(demand, 1, 3, 1, 24, 72)
        @test length(sel) == 3
        @test sel[1] == 25                       # peak day always first
        @test Set(sel) == Set([1, 25, 49])
        @test all(i -> (i - 1) % 24 == 0, sel)   # all are day boundaries

        # Fewer timesteps than a full day -> nothing selectable.
        @test select_representative_days(ones(10, 1), 1, 3, 1, 24, 10) == Int[]
        # year_idx past the end of the demand matrix -> empty.
        @test select_representative_days(demand, 5, 1, 1, 24, 72) == Int[]
    end

    @testset "compute_upscaling_indices" begin
        # points_per_day <= 0 disables upscaling: every hour is its own point.
        idx, tmap = ESFEX.compute_upscaling_indices(24, 0)
        @test idx == collect(0:24)
        @test tmap[0] == 1 && tmap[24] == 25
        # points_per_day >= 24 also means full resolution.
        @test ESFEX.compute_upscaling_indices(24, 24)[1] == collect(0:24)

        # 6 points/day over 48h: 4-hourly sampling, endpoints always present,
        # strictly increasing, and the map sends every hour to a valid slot.
        idx6, tmap6 = ESFEX.compute_upscaling_indices(48, 6)
        @test first(idx6) == 0
        @test last(idx6) == 48                   # final hour forced in
        @test issorted(idx6)
        @test allunique(idx6)
        @test length(tmap6) == 49                # one entry per hour 0:48
        @test all(p -> 1 <= p <= length(idx6), values(tmap6))
        @test tmap6[0] == 1                       # hour 0 maps to the first slot
    end

    @testset "calculate_unit_npv" begin
        # End-of-life unit: NPV is just the (negative) decommissioning cost.
        @test calculate_unit_npv(100.0, 20.0, 5.0, 5.0, 0.0, 0.0, 50.0,
                                 0.05, 0.6, 100.0) == -50.0
        # Zero capacity contributes nothing.
        @test calculate_unit_npv(0.0, 20.0, 5.0, 5.0, 25.0, 0.0, 50.0,
                                 0.05, 0.6, 100.0) == 0.0

        # One year, no degradation/discount/decommissioning: NPV equals the
        # single-year net cash flow = generation * (lcoe - total_unit_cost).
        gen = 100.0 * 0.5 * Float64(ESFEX.HOURS_STD_YEAR)  # MWh
        expected = gen * (100.0 - (20.0 + 5.0 + 5.0))
        @test calculate_unit_npv(100.0, 20.0, 5.0, 5.0, 1.0, 0.0, 0.0,
                                 0.0, 0.5, 100.0) ≈ expected

        # A unit whose price exceeds its costs has positive NPV; flip the price
        # below cost and the NPV goes negative.
        @test calculate_unit_npv(100.0, 20.0, 5.0, 5.0, 10.0, 0.0, 0.0,
                                 0.05, 0.6, 120.0) > 0
        @test calculate_unit_npv(100.0, 60.0, 10.0, 10.0, 10.0, 0.0, 0.0,
                                 0.05, 0.6, 50.0) < 0

        # Adding a decommissioning cost can only lower NPV, never raise it.
        without = calculate_unit_npv(100.0, 20.0, 5.0, 5.0, 10.0, 0.0, 0.0,
                                     0.05, 0.6, 120.0)
        with = calculate_unit_npv(100.0, 20.0, 5.0, 5.0, 10.0, 0.0, 1.0e6,
                                  0.05, 0.6, 120.0)
        @test with < without
    end

    @testset "create_temporal_mapping" begin
        # 48 hours in 24-hour primary periods, one investment period spanning all.
        tm = create_temporal_mapping(48, 24, 48)
        @test tm.num_primary_periods == 2
        @test tm.num_investment_periods == 1
        @test tm.hours_in_primary_period[1] == collect(1:24)
        @test tm.hours_in_primary_period[2] == collect(25:48)
        @test tm.hour_to_primary_period[1] == 1
        @test tm.hour_to_primary_period[24] == 1
        @test tm.hour_to_primary_period[25] == 2
        @test tm.hour_to_primary_period[48] == 2
        @test tm.primary_to_investment_period[1] == 1
        @test tm.primary_to_investment_period[2] == 1
        @test Set(tm.primary_periods_in_investment_period[1]) == Set([1, 2])

        # The primary periods partition the hours exactly: every hour mapped,
        # no hour mapped twice.
        all_hours = vcat((tm.hours_in_primary_period[p] for p in 1:tm.num_primary_periods)...)
        @test sort(all_hours) == collect(1:48)
        @test length(tm.hour_to_primary_period) == 48

        # Non-divisible horizon uses ceiling division for the period count.
        tm2 = create_temporal_mapping(10, 3, 10)
        @test tm2.num_primary_periods == 4          # cld(10, 3)
        @test tm2.hours_in_primary_period[4] == [10] # short tail period
        @test tm2.hour_to_primary_period[10] == 4

        # Non-positive resolution collapses to a single full-horizon period.
        tm3 = create_temporal_mapping(12, 0, 0)
        @test tm3.num_primary_periods == 1
        @test tm3.hours_in_primary_period[1] == collect(1:12)
    end

    @testset "data structs" begin
        # Plain immutable data carriers: field round-trip on construction.
        @testset "TransmissionLineData" begin
            l = TransmissionLineData("L7", 3, 9, 250.0, 0.05, 0.005, 0.02,
                                     120.0, 380.0, 2, 50.0, "AC")
            @test l.line_id == "L7"
            @test l.from_node == 3 && l.to_node == 9
            @test l.capacity_mw == 250.0
            @test l.num_circuits == 2
            @test l.current_type == "AC"
        end

        @testset "BusData" begin
            b = BusData(5, 2, 220.0, 50.0, "AC", "PV", "load", 0.4)
            @test b.bus_id == 5 && b.parent_node == 2
            @test b.bus_type == "PV" && b.role == "load"
            @test b.demand_fraction == 0.4
        end

        @testset "TransformerData" begin
            t = TransformerData("T1", 1, 2, 380.0, 220.0, 500.0, 0.12, 0.01,
                                0.1196, 1.05, 0.004)
            @test t.name == "T1"
            @test t.from_voltage_kv == 380.0 && t.to_voltage_kv == 220.0
            @test t.tap_ratio == 1.05
        end

        @testset "CostSegment" begin
            cs = CostSegment(0.25, 42.0)
            @test cs.fraction == 0.25
            @test cs.marginal_cost == 42.0
        end

        @testset "TemporalConfig / PenaltyConfig / TargetConfig / SolverSettings" begin
            tc = TemporalConfig(8760, 1, 24, 0, 8760, 168, 6, 6, 4)
            @test tc.hours == 8760 && tc.reserve_resolution == 4
            pc = PenaltyConfig(1.0, 2e-5, 0.01, 0.01, 0.05)
            @test pc.loss_of_load == 1.0 && pc.co2_cost == 0.05
            tg = TargetConfig(0.8, 1.0e6, 5000.0)
            @test tg.re_penetration_target == 0.8 && tg.inertia_limit == 5000.0
            ss = SolverSettings(4, 3600.0, 0.01, false)
            @test ss.threads == 4 && ss.verbose == false
        end

        @testset "SystemNodeRange" begin
            s = SystemNodeRange("North", 1, 5, 0.3)
            @test s.name == "North" && s.first_bus == 1
            @test s.num_buses == 5 && s.initial_re == 0.3
        end

        @testset "ScenarioMultipliers / Scenario" begin
            m = ScenarioMultipliers()  # all-1.0 default
            @test all(getfield(m, f) == 1.0 for f in fieldnames(ScenarioMultipliers))
            @test m.invest_cost_renewables == 1.0 && m.carbon_price == 1.0
            # By name, or the original ten positional values (the rest stay 1.0)
            mk = ScenarioMultipliers(penalty_curtailment=2.0, bat_invest_cost_power=0.5)
            @test mk.penalty_curtailment == 2.0 && mk.bat_invest_cost_power == 0.5
            @test mk.fuel_cost == 1.0
            m10 = ScenarioMultipliers(0.5, 1.5, 2.0, 1.1, 0.8, 1.3, 0.95, 1.2, 1.4, 3.0)
            @test m10.invest_cost_renewables == 0.5 && m10.carbon_price == 3.0
            @test m10.gen_fixed_cost == 1.0 && m10.penalty_loss_of_load == 1.0
            sc = Scenario("base", 0.5, m)
            @test sc.name == "base" && sc.probability == 0.5
            @test sc.multipliers === m
        end

        @testset "InterSystemLink back-compat constructors" begin
            # 9-arg form fills cost_per_mw_km=1.0, reactance=0.01, resistance=0.001.
            l9 = InterSystemLink("A", "B", 1, 2, 100.0, 500.0, 50.0, 0.05, 300.0)
            @test l9.from_system == "A" && l9.to_system == "B"
            @test l9.cost_per_mw_km == 1.0
            @test l9.reactance_pu == 0.01
            @test l9.resistance_pu == 0.001
            # 10-arg form takes cost_per_mw_km explicitly, keeps the impedance defaults.
            l10 = InterSystemLink("A", "B", 1, 2, 100.0, 500.0, 50.0, 0.05, 300.0, 0.02)
            @test l10.cost_per_mw_km == 0.02
            @test l10.reactance_pu == 0.01
            @test l10.resistance_pu == 0.001
        end

        @testset "MGAResult back-compat constructor" begin
            # Labels auto-fill to "hsj_diversity", one per diversity objective.
            mga = MGAResult(MasterProblemResult[], 2, 0.1, 500.0,
                            [500.0, 510.0], [0.0, 5.0])
            @test mga.slack_fraction == 0.1
            @test mga.optimal_cost == 500.0
            @test mga.objective_labels == ["hsj_diversity", "hsj_diversity"]
            # Empty objective list -> empty (typed) label vector.
            mga0 = MGAResult(MasterProblemResult[], 0, 0.05, 1.0,
                             Float64[], Float64[])
            @test mga0.objective_labels == String[]
        end
    end

    # =======================================================================
    # Integration: build and SOLVE small models end to end in pure Julia.
    # Unlike the pure-function tests above, these drive the model-construction
    # core (create_power_system -> build_variables!/build_objective!/
    # add_*_constraints! -> extract_solution) and the standalone DCOPF/ACOPF
    # benchmark solvers. HiGHS solves the LPs; Ipopt solves the ACOPF NLP.
    # =======================================================================
    @testset "integration" begin

        # GeneratorConfig has 39 positional fields; this keeps the boilerplate
        # in one place. Per-bus vectors are sized to `nb`; `avail` is the
        # [hours x buses] availability matrix. All reservoir fields are zeroed
        # (no hydro), reservable=false.
        function mkgen(name, type, fuel, rated, fcost, avail; nb, H)
            z = zeros(nb)
            GeneratorConfig(
                name, type, fuel, rated, zeros(nb),
                fill(0.5, nb), fill(0.4, nb), ones(nb), ones(nb), z, z, z,
                fcost, z, z, z, z, z, avail, false,
                fill(30.0, nb), z, z, z, 50.0, "AC",
                z, z, z, z, zeros(H, nb), z, z, z, z, false, z, z, z,
            )
        end

        @testset "economic dispatch (single bus)" begin
            H = 2
            gen = mkgen("GasCC", "Non-renewable", "Gas", [100.0],
                        [3.0e-5], ones(H, 1); nb = 1, H = H)
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            input = PowerSystemInput(
                name = "ed", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = reshape([50.0, 60.0], H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                mode = "economic_dispatch", solver_name = "highs", verbose = false)

            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            res = extract_solution(model, vars, input)
            @test res.total_demand ≈ 110.0
            @test res.total_generation ≈ 110.0 atol = 1e-6   # gen serves all load
            @test res.load_shed_total ≈ 0.0 atol = 1e-6      # nothing shed
            @test isfinite(res.objective) && res.objective > 0
            @test size(res.gen_output) == (1, 1, H)
        end

        @testset "sectoral shed bounded by each sector's demand" begin
            # 100 MW of load, 40 MW of gas: 60 MW must be shed. The cheap
            # sector holds only 30 MW, so the other 30 MW come out of the
            # critical sector. The per-sector cap was dropped, and all 60 MW
            # went to the cheap sector at its multiplier.
            H = 1
            gen = mkgen("Gas", "Non-renewable", "Gas", [40.0],
                        [3.0e-5], ones(H, 1); nb = 1, H = H)
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            function shed_split(sec_dem)
                input = PowerSystemInput(
                    name = "sec", year = 2025, network = net, generators = [gen],
                    batteries = BatteryConfig[], demand = reshape([100.0], H, 1),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    sectoral_demand = Dict(k => reshape([v], H, 1) for (k, v) in sec_dem),
                    sectoral_criticality = Dict("cheap" => 1.0, "critical" => 1000.0),
                    loss_of_load_penalty = 0.01,
                    mode = "economic_dispatch", solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                res = extract_solution(model, vars, input)
                lol = Dict(k => value(v[1, 1]) for (k, v) in vars.loss_of_load_sectoral)
                return lol, res
            end
            lol, res = shed_split(Dict("cheap" => 30.0, "critical" => 70.0))
            @test res.load_shed_total ≈ 60.0 atol = 1e-6
            @test lol["cheap"] ≈ 30.0 atol = 1e-6
            @test lol["critical"] ≈ 30.0 atol = 1e-6
            @test res.cost_breakdown.load_shedding_cost ≈
                  0.01 * (30.0 * 1.0 + 30.0 * 1000.0) rtol = 1e-6
            # Sectors covering only part of the node's demand: the rest is an
            # unclassified remainder priced at VOLL, shed before the critical
            # sector; the model stays feasible.
            lol, res = shed_split(Dict("critical" => 50.0))
            @test res.load_shed_total ≈ 60.0 atol = 1e-6
            @test lol["critical"] ≈ 10.0 atol = 1e-6
            @test res.cost_breakdown.load_shedding_cost ≈
                  0.01 * (50.0 + 10.0 * 1000.0) rtol = 1e-6
        end

        @testset "master: sectoral shed is part of load shed" begin
            # One bus, 100 MW of load, 40 MW of gas and nothing to build:
            # 60 MW must be shed on each representative day. The sectoral
            # shed used to be an extra injection next to load_shed, so a day
            # could shed up to ~2x its demand and the bus's own load_shed (at
            # plain VOLL) absorbed every MWh: the sector's criticality had no
            # effect on the plan's cost.
            H = 48
            gas = mkgen("Gas", "Non-renewable", "Gas", [40.0],
                        [1.0], ones(H, 1); nb = 1, H = H)
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            function master_shed(crit)
                inp = MasterProblemInput(
                    years = [2025], network = net, generators = [gas],
                    batteries = BatteryConfig[], base_demand = fill(100.0, H, 1),
                    target_re_penetration = 0.0, initial_re_penetration = 0.0,
                    representative_days_per_year = 1, min_day_separation = 1,
                    loss_of_load_penalty = 10.0,
                    sectoral_demand = Dict("cheap" => fill(30.0, H, 1),
                                           "critical" => fill(70.0, H, 1)),
                    sectoral_criticality = Dict("cheap" => 1.0, "critical" => crit),
                    verbose = false)
                model, mvars, _ = create_master_problem(inp; use_representative_days = true)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                byname(pfx) = [v for v in all_variables(model) if startswith(name(v), pfx)]
                shed = sum(value.(byname("op_y1_d1_ll[")))
                lol_crit = sum(value.(byname("op_y1_d1_lol_critical[")))
                return objective_value(model), shed, lol_crit
            end
            obj1, shed1, _ = master_shed(1.0)
            obj2, shed2, crit2 = master_shed(1000.0)
            @test shed1 ≈ 24 * 60.0 rtol = 1e-6     # exactly the unmet load
            @test shed2 ≈ 24 * 60.0 rtol = 1e-6
            @test crit2 ≈ 24 * 30.0 rtol = 1e-6     # cheap sector holds only 30 MW
            @test obj2 > 100 * obj1                  # criticality is priced

            # A myopic year's target comes from the horizon trajectory: the
            # override replaces RE-1 (which gives year 1 its initial share).
            mk(ov) = MasterProblemInput(
                years = [2025], network = net, generators = [gas],
                batteries = BatteryConfig[], base_demand = fill(100.0, H, 1),
                target_re_penetration = 0.8, initial_re_penetration = 0.1,
                re_target_override = ov)
            @test calculate_target_ratios(mk(Dict{Tuple{Int,Int},Float64}()))[(1, 1)] ≈ 0.1
            @test calculate_target_ratios(mk(Dict((1, 1) => 0.42)))[(1, 1)] ≈ 0.42
        end

        @testset "master: each representative day reads its own inflow" begin
            # An empty reservoir is the only source; day 2 is the peak (the
            # representative) day and the only day with inflow. The day model
            # read inflow row t of the whole profile, so the representative
            # day saw day 1's zero inflow and shed all its load.
            H = 48
            inflow = zeros(H, 1)
            inflow[25:48, 1] .= 30.0
            hydro = GeneratorConfig(
                "Hydro", "Renewable", "Water", [100.0], [0.0],
                [1.0], [1.0], [100.0], [100.0], [0.0], [0.0], [0.0],
                [0.0], [0.0], [0.0], [0.0], [0.0], [0.0], ones(H, 1), true,
                [60.0], [0.0], [0.0], [0.0], 50.0, "AC",
                [500.0], [0.0], [0.0], [1.0], inflow,
                [1.0], [0.0], [0.0], [1.0], true, [0.0], [0.0], [1.0],
            )
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            demand = fill(10.0, H, 1)
            demand[25:48, 1] .= 20.0
            inp = MasterProblemInput(
                years = [2025], network = net, generators = [hydro],
                batteries = BatteryConfig[], base_demand = demand,
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                loss_of_load_penalty = 10.0, verbose = false)
            model, _, _ = create_master_problem(inp; use_representative_days = true)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            shed = sum(value(v) for v in all_variables(model)
                       if startswith(name(v), "op_y1_d1_ll["))
            @test shed ≈ 0.0 atol = 1e-6

            # The cut wraps around a shorter profile, and units without
            # profiles pass through.
            day = ESFEX.generators_for_day([hydro], 40, 12)[1]
            @test day.reservoir_inflow[:, 1] == [fill(30.0, 9); zeros(3)]
            @test size(day.availability) == (12, 1)
        end

        @testset "custom constraints (linear cap binds)" begin
            H = 2
            gen = mkgen("GasCC", "Non-renewable", "Gas", [100.0],
                        [3.0e-5], ones(H, 1); nb = 1, H = H)
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            mkinput() = PowerSystemInput(
                name = "cc", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = reshape([50.0, 60.0], H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                mode = "economic_dispatch", solver_name = "highs", verbose = false)

            # A linear cap on total generator output forces 30 MWh of shedding.
            cc = [Dict("name" => "cap_gen", "type" => "linear", "sense" => "<=",
                       "rhs" => 80.0,
                       "terms" => [Dict("variable" => "gen_output",
                                        "index" => [1, -1], "coefficient" => 1.0)])]
            inp = mkinput()
            model, vars = create_power_system(inp; custom_constraints = cc)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            res = extract_solution(model, vars, inp)
            @test res.total_generation ≈ 80.0 atol = 1e-4
            @test res.load_shed_total ≈ 30.0 atol = 1e-4

            # Unknown constraint type errors clearly.
            @test_throws ErrorException create_power_system(
                mkinput();
                custom_constraints = [Dict("name" => "x", "type" => "bogus")])
        end

        @testset "reservoir minimum release binds" begin
            # A single reservoir-hydro unit on a flat, low demand. Without a
            # minimum release it simply turbines to meet load (no spill). A
            # min release above the natural turbined flow must FORCE extra
            # water out — turbined power is capped by demand, so the surplus
            # is spilled. The constraint is gen_output/η + spill >= min_release.
            H = 4
            # 40-field GeneratorConfig: a dispatchable reservoir unit. Plenty of
            # stored water, no inflow (pure draw-down), spillage allowed.
            mkhydro(minrel) = GeneratorConfig(
                "Hydro", "Renewable", "Water", [100.0], [0.0],
                [1.0], [1.0], [100.0], [100.0], [0.0], [0.0], [0.0],
                [0.0], [0.0], [0.0], [0.0], [0.0], [0.0], ones(H, 1), true,
                [60.0], [0.0], [0.0], [0.0], 50.0, "AC",
                [500.0], [200.0], [0.0], [500.0], zeros(H, 1),
                [1.0], [0.0], [0.0], [1.0], true, [0.0], [0.0], [0.0],
                [minrel],
            )
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            demand = reshape(fill(10.0, H), H, 1)
            mkinput(g) = PowerSystemInput(
                name = "hyd", year = 2025, network = net, generators = [g],
                batteries = BatteryConfig[], demand = demand,
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                mode = "economic_dispatch", solver_name = "highs", verbose = false)

            # Baseline: no minimum release.
            m0, v0 = create_power_system(mkinput(mkhydro(0.0)))
            optimize!(m0)
            @test string(termination_status(m0)) == "OPTIMAL"
            r0 = extract_solution(m0, v0, mkinput(mkhydro(0.0)))
            @test r0.reservoir_spillage !== nothing
            spill0 = sum(r0.reservoir_spillage)
            @test spill0 ≈ 0.0 atol = 1e-6          # nothing forced out

            # Floor of 20 MW-eq > the 10 MW it would naturally turbine.
            minrel = 20.0
            m1, v1 = create_power_system(mkinput(mkhydro(minrel)))
            optimize!(m1)
            @test string(termination_status(m1)) == "OPTIMAL"
            r1 = extract_solution(m1, v1, mkinput(mkhydro(minrel)))
            # Generation is unchanged — the unit still serves only the load
            # (plus network losses) each hour; the floor forces extra water out
            # as spill, it does not raise output.
            for t in 1:H
                @test isapprox(r1.gen_output[1, 1, t], r0.gen_output[1, 1, t];
                               atol = 1e-6)
            end
            # The constraint binds every hour: gen/η_turbine + spill >= min_release.
            for t in 1:H
                @test r1.gen_output[1, 1, t] / 1.0 +
                      r1.reservoir_spillage[1, 1, t] >= minrel - 1e-6
            end
            # And it genuinely changed the solution: spillage appears only when
            # the floor is imposed.
            @test sum(r1.reservoir_spillage) > spill0 + 1.0
        end

        @testset "hydraulic cascade feeds downstream reservoir" begin
            # Two reservoirs in series on a single bus. "Up" has inflow and is
            # forced (min_release) to pass 40 MW-eq of water each hour; its low
            # turbine cap means most of that leaves as spill. "Down" has no
            # inflow of its own — only the cascade can give it water. With the
            # link Down generates and displaces the expensive gas; without it
            # Down stays dry.
            H = 4
            # 42-field reservoir generator with a cascade target.
            mkres(name, rated, init_frac, inflow_mw, minrel, downstream) =
                GeneratorConfig(
                    name, "Renewable", "Water", [rated], [0.0],
                    [1.0], [1.0], [100.0], [100.0], [0.0], [0.0], [0.0],
                    [0.0], [0.0], [0.0], [0.0], [0.0], [0.0], ones(H, 1), true,
                    [60.0], [0.0], [0.0], [0.0], 50.0, "AC",
                    [1000.0], [init_frac], [0.0], [1.0], fill(inflow_mw, H, 1),
                    [1.0], [0.0], [0.0], [1.0], true, [0.0], [0.0], [0.0],
                    [minrel], downstream, 0,
                )
            gas = mkgen("Gas", "Non-renewable", "Gas", [200.0],
                        [1.0], ones(H, 1); nb = 1, H = H)  # expensive backup
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            demand = reshape(fill(50.0, H), H, 1)
            mkinput(up, down) = PowerSystemInput(
                name = "casc", year = 2025, network = net,
                generators = [up, down, gas],
                batteries = BatteryConfig[], demand = demand,
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                mode = "economic_dispatch", solver_name = "highs", verbose = false)

            down = mkres("Down", 100.0, 0.0, 0.0, 0.0, "")  # dry, terminal
            # Up passes 40 MW-eq/h (turbine cap 10 -> ~30 spilled), inflow 40 keeps
            # it water-balanced over the cyclic period.
            up_linked = mkres("Up", 10.0, 0.5, 40.0, 40.0, "Down")
            up_isolated = mkres("Up", 10.0, 0.5, 40.0, 40.0, "")

            mc, vc = create_power_system(mkinput(up_linked, down))
            optimize!(mc)
            @test string(termination_status(mc)) == "OPTIMAL"
            rc = extract_solution(mc, vc, mkinput(up_linked, down))
            down_gen_linked = sum(rc.gen_output[2, 1, :])

            mi, vi = create_power_system(mkinput(up_isolated, down))
            optimize!(mi)
            @test string(termination_status(mi)) == "OPTIMAL"
            ri = extract_solution(mi, vi, mkinput(up_isolated, down))
            down_gen_isolated = sum(ri.gen_output[2, 1, :])

            # The cascade is the only water source for Down: it generates with
            # the link and is essentially idle without it.
            @test down_gen_linked > 100.0
            @test down_gen_isolated < 1.0
            # Letting Down run on cascade water serves load that is otherwise
            # unmet/expensive -> the linked case is strictly cheaper.
            @test rc.objective < ri.objective - 0.5
        end

        @testset "head dependence limits power at low reservoir level" begin
            # A reservoir held at a low level (10% full) with ample inflow to
            # meet demand. With no head effect the turbine reaches nameplate
            # power; with head_min_factor = 0.3 the low head caps output near
            # 0.3 + 0.7*0.1 = 0.37 of rated, so the unit cannot serve the load
            # and must spill water / lean on gas.
            H = 2
            mkhead(factor) = GeneratorConfig(
                "Hydro", "Renewable", "Water", [100.0], [0.0],
                [1.0], [1.0], [100.0], [100.0], [0.0], [0.0], [0.0],
                [0.0], [0.0], [0.0], [0.0], [0.0], [0.0], ones(H, 1), true,
                [60.0], [0.0], [0.0], [0.0], 50.0, "AC",
                [1000.0], [0.1], [0.0], [1.0], fill(80.0, H, 1),
                [1.0], [0.0], [0.0], [1.0], true, [0.0], [0.0], [0.0],
                [0.0], "", 0, [factor],  # min_release, cascade_*, head_min_factor
            )
            gas = mkgen("Gas", "Non-renewable", "Gas", [200.0],
                        [1.0], ones(H, 1); nb = 1, H = H)
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            demand = reshape(fill(80.0, H), H, 1)
            mkinput(h) = PowerSystemInput(
                name = "head", year = 2025, network = net,
                generators = [h, gas],
                batteries = BatteryConfig[], demand = demand,
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                mode = "economic_dispatch", solver_name = "highs", verbose = false)

            mf, vf = create_power_system(mkinput(mkhead(1.0)))   # no head effect
            optimize!(mf)
            @test string(termination_status(mf)) == "OPTIMAL"
            rf = extract_solution(mf, vf, mkinput(mkhead(1.0)))
            hydro_full = sum(rf.gen_output[1, 1, :])

            md, vd = create_power_system(mkinput(mkhead(0.3)))   # head-limited
            optimize!(md)
            @test string(termination_status(md)) == "OPTIMAL"
            rd = extract_solution(md, vd, mkinput(mkhead(0.3)))
            hydro_derated = sum(rd.gen_output[1, 1, :])

            # Without the head limit the unit serves the full 80 MW load each
            # hour; with it, output is throttled by the low head.
            @test hydro_full > 150.0
            # Hour 1 starts at the fixed initial level (10% full), so the cap is
            # deterministic: 0.3 + 0.7*0.1 = 0.37 of the 100 MW rating.
            @test rd.gen_output[1, 1, 1] <= 37.0 + 1e-3
            # Aggregate: head-limited hydro produces far less over the window.
            @test hydro_full > hydro_derated * 1.5
            # The throttled hydro is replaced by gas -> more expensive.
            @test rd.objective > rf.objective + 0.5
        end

        @testset "reservoir pumping draws power at its own bus (multi-bus)" begin
            # One node split into two buses; a pumped reservoir sits on bus 2.
            # The DC/AC KCL looked the reservoir up by NODE index (1) in the
            # per-bus arrays, found no reservoir on bus 1 and dropped the pump
            # from the balance: pumped water was free. With no other source
            # and an empty reservoir, nothing can be served.
            H = 4
            hydro = GeneratorConfig(
                "PumpedHydro", "Renewable", "Water", [0.0, 100.0], [0.0, 0.0],
                [1.0, 1.0], [1.0, 1.0], [100.0, 100.0], [100.0, 100.0], [0.0, 0.0],
                [0.0, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0],
                [0.0, 0.0], [0.0, 0.0], [0.0, 0.0], ones(H, 2), true,
                [60.0, 60.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0], 50.0, "AC",
                [0.0, 500.0], [0.0, 0.0], [0.0, 0.0], [0.0, 1.0], zeros(H, 2),
                [1.0, 1.0], [0.0, 0.0], [0.0, 50.0], [0.8, 0.8], true,
                [0.0, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0],
            )
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 0.0),
                     BusData(2, 1, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
            line = TransmissionLineData("L12", 1, 2, 10.0, 0.1, 0.0, 0.0,
                                        200.0, 220.0, 1, 50.0, "AC")
            net = NetworkConfig(1, 2, buses, [1, 1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.6, 1, [0.0], [0.0],
                                [line], TransformerData[], ACDCConverterData[],
                                FrequencyConverterData[], 0.1)
            input = PowerSystemInput(
                name = "pump", year = 2025, network = net, generators = [hydro],
                batteries = BatteryConfig[], demand = reshape([0.0, 10.0, 10.0, 10.0], H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                mode = "economic_dispatch", solver_name = "highs", verbose = false,
                pwl_loss_segments = 0)
            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            res = extract_solution(model, vars, input)
            @test res.load_shed_total ≈ 30.0 atol = 1e-4
        end

        @testset "two-bus transmission + renewable + battery" begin
            H = 2; N = 2
            gas  = mkgen("GasCC", "Non-renewable", "Gas", [100.0, 0.0],
                         [3.0e-5, 3.0e-5], ones(H, N); nb = N, H = H)
            wind = mkgen("Wind", "Renewable", "Wind", [0.0, 80.0],
                         [0.0, 0.0], [0.0 0.6; 0.0 0.9]; nb = N, H = H)
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
            line = TransmissionLineData("L12", 1, 2, 150.0, 0.1, 0.01, 0.0,
                                        100.0, 220.0, 1, 50.0, "AC")
            net = NetworkConfig(N, N, buses, [1, 2],
                                [0.0 150.0; 150.0 0.0], [0.0 100.0; 100.0 0.0],
                                100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                                [line], TransformerData[], ACDCConverterData[],
                                FrequencyConverterData[], 0.1)
            bat = BatteryConfig("Li1", [0.0, 40.0], [0.0, 20.0], [0.0, 20.0],
                                [0.95, 0.95], [0.95, 0.95], [0.0, 0.1], [1.0, 1.0],
                                [0.5, 0.5], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0],
                                [0.0, 0.0], [0.0, 0.0], [10.0, 10.0], [0.0, 0.0],
                                [0.0, 0.0], 0.0, 100.0, [0.0, 0.0], [0.0, 0.0],
                                false, "AC", [0.0, 0.0], [0.0, 0.0], [0.0, 0.0])
            input = PowerSystemInput(
                name = "2bus", year = 2025, network = net,
                generators = [gas, wind], batteries = [bat],
                demand = [30.0 70.0; 40.0 90.0],
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                mode = "economic_dispatch", solver_name = "highs", verbose = false)

            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            res = extract_solution(model, vars, input)
            @test res.total_demand ≈ 230.0
            @test res.load_shed_total ≈ 0.0 atol = 1e-6   # demand fully served
            # Generation need not equal demand: the battery's initial SOC can
            # supply net energy, and the resistive line adds losses.
            @test isfinite(res.total_generation) && res.total_generation > 0.0
            @test 0.0 < res.re_penetration ≤ 1.0          # wind contributes
            @test haskey(res.power_flow, (1, 2))          # line flow extracted
        end

        @testset "linear losses cost energy in either direction" begin
            # The linear model scaled the flow at the lower-index end by (1 - λ),
            # so a flow leaving bus 1 delivered more than was sent (95 MW
            # generated for 100 MW of load).
            function line_gen(gen_bus, segments)
                H = 1; N = 2
                rated = gen_bus == 1 ? [200.0, 0.0] : [0.0, 200.0]
                gas = mkgen("Gas", "Non-renewable", "Gas", rated, [3.0e-5, 3.0e-5],
                            ones(H, N); nb = N, H = H)
                buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                         BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
                line = TransmissionLineData("L12", 1, 2, 150.0, 0.1, 0.05, 0.0,
                                            100.0, 220.0, 1, 50.0, "AC")
                net = NetworkConfig(N, N, buses, [1, 2],
                                    [0.0 150.0; 150.0 0.0], [0.0 100.0; 100.0 0.0],
                                    100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                                    [line], TransformerData[], ACDCConverterData[],
                                    FrequencyConverterData[], 0.1)
                demand = gen_bus == 1 ? [0.0 100.0] : [100.0 0.0]
                input = PowerSystemInput(
                    name = "loss", year = 2025, network = net, generators = [gas],
                    batteries = BatteryConfig[], demand = demand,
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    mode = "economic_dispatch", solver_name = "highs", verbose = false,
                    pwl_loss_segments = segments)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                res = extract_solution(model, vars, input)
                @test res.load_shed_total ≈ 0.0 atol = 1e-6
                return res.total_generation
            end
            # λ = R = 0.05, half lost at each end: sent f(1 + λ/2), delivered f(1 - λ/2) = 100
            expected = 100.0 * (1 + 0.025) / (1 - 0.025)
            @test line_gen(1, -1) ≈ expected atol = 1e-6
            @test line_gen(2, -1) ≈ expected atol = 1e-6
            @test line_gen(1, 0) ≈ 100.0 atol = 1e-6

            # The master and Benders now pick the model the same way as dispatch
            t = TransmissionDC(NetworkConfig(2, 2,
                [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                 BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)], [1, 2],
                zeros(2, 2), zeros(2, 2), 100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                [TransmissionLineData("L12", 1, 2, 150.0, 0.1, 0.05, 0.0, 100.0, 220.0, 1, 50.0, "AC")],
                TransformerData[], ACDCConverterData[], FrequencyConverterData[], 0.1))
            @test ESFEX.with_loss_model(t, 2, 100.0).pwl_losses.num_segments == 2
            @test ESFEX.with_loss_model(t, -1, 100.0).pwl_losses.slopes == [[0.05]]
            @test ESFEX.with_loss_model(t, 0, 100.0).pwl_losses === nothing
        end

        @testset "end-of-year SOC floor" begin
            # Rolling windows leave the end SOC free; the year's last window
            # gets a soft floor so the battery does not end the year emptier
            # than it started (the next year restarts from soc_initial).
            H = 4
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            bat = BatteryConfig("Li1", [40.0], [20.0], [20.0], [0.95], [0.95],
                                [0.0], [1.0], [0.5], [0.0], [0.0], [0.0], [0.0],
                                [0.0], [10.0], [0.0], [0.0], 0.0, 100.0, [0.0],
                                [0.0], false, "AC", [0.0], [0.0], [0.0])
            function solve_floor(gas_mw; kw...)
                gen = mkgen("GasCC", "Non-renewable", "Gas", [gas_mw],
                            [3.0e-5], ones(H, 1); nb = 1, H = H)
                input = PowerSystemInput(;
                    name = "socfloor", year = 2025, network = net, generators = [gen],
                    batteries = [bat], demand = fill(50.0, H, 1),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    cyclic_end_soc = false,
                    mode = "economic_dispatch", solver_name = "highs", verbose = false, kw...)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                return extract_solution(model, vars, input)
            end

            # No floor: the stored 20 MWh is spent for free.
            free = solve_floor(100.0)
            @test free.bat_soc[1, 1, end] ≈ 0.0 atol = 1e-6
            # Single-window year: floor at its own initial SOC (20 MWh, 5 % tolerance).
            cyc = solve_floor(100.0; bat_soc_end_min_cyclic = true)
            @test cyc.bat_soc[1, 1, end] ≥ 19.0 - 1e-6
            # Floor above what the window can reach (52 MW gas, 50 MW load: at most
            # 2 MW × 0.95 × 4 h of charging): stays feasible, charges all it can
            # and never sheds load to refill the battery.
            short = solve_floor(52.0; bat_soc_end_min = Dict(1 => Dict(1 => 40.0)))
            @test short.load_shed_total ≈ 0.0 atol = 1e-6
            @test short.bat_soc[1, 1, end] ≈ 20.0 + 4 * 2.0 * 0.95 atol = 1e-4
        end

        @testset "battery SOC carried in MWh" begin
            # The previous window's SOC is energy: used as-is (not as a
            # fraction of a possibly degraded capacity), clamped to bounds.
            H = 2
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            # 2 %/yr degradation, 10 years old: capacity 40 × 0.98^10 ≈ 32.7 MWh.
            bat = BatteryConfig("Li1", [40.0], [20.0], [20.0], [0.95], [0.95],
                                [0.0], [1.0], [0.5], [0.0], [0.0], [0.0], [0.0],
                                [0.0], [30.0], [10.0], [0.0], 0.0, 100.0, [0.0],
                                [0.0], false, "AC", [0.02], [0.0], [0.0])
            gen = mkgen("GasCC", "Non-renewable", "Gas", [100.0],
                        [3.0e-5], ones(H, 1); nb = 1, H = H)
            function start_soc(prev)
                input = PowerSystemInput(
                    name = "socprev", year = 2025, base_year = 2025, network = net,
                    generators = [gen], batteries = [bat], demand = fill(50.0, H, 1),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    cyclic_end_soc = false, battery_soc_prev = prev,
                    mode = "economic_dispatch", solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                return extract_solution(model, vars, input).bat_soc[1, 1, 1]
            end
            @test start_soc(Dict(1 => Dict(1 => 12.0))) ≈ 12.0 atol = 1e-6
            @test start_soc(Dict(1 => Dict(1 => 50.0))) ≈ 40.0 * 0.98^10 atol = 1e-6
            @test start_soc(Dict{Int, Dict{Int, Float64}}()) ≈ 0.5 * 40.0 * 0.98^10 atol = 1e-6
        end

        @testset "ramp rate is per hour, scaled to the time step" begin
            # ramp_up = 0.2/h on 100 MW: 20 MW per 1 h step, 40 MW per 2 h step.
            H = 2
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            function step_change(tres)
                gen = mkgen("GasCC", "Non-renewable", "Gas", [100.0],
                            [3.0e-5], ones(H, 1); nb = 1, H = H)
                gen.ramp_up[1] = 0.2
                gen.ramp_down[1] = 0.2
                input = PowerSystemInput(
                    name = "ramp_tres", year = 2025, network = net, generators = [gen],
                    batteries = BatteryConfig[], demand = reshape([20.0, 90.0], H, 1),
                    temporal = TemporalConfig(H, tres, H, 0, H, H, 1, 1, 1),
                    generator_output_prev = Dict(1 => Dict(1 => 20.0)),
                    mode = "economic_dispatch", solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                res = extract_solution(model, vars, input)
                return res.gen_output[1, 1, 2] - res.gen_output[1, 1, 1]
            end
            @test step_change(1) ≈ 20.0 atol = 1e-6
            @test step_change(2) ≈ 40.0 atol = 1e-6
            @test step_change(6) > 40.0                # 0.2 × 6 ≥ 1: no limit
        end

        @testset "min up/down times are hours, not steps" begin
            # 4 h minimum at 2-h steps spans 2 steps: at t=4 only τ = 3, 4.
            H = 4
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            gen = mkgen("GasCC", "Non-renewable", "Gas", [100.0],
                        [3.0e-5], ones(H, 1); nb = 1, H = H)
            gen.min_up_time[1] = 4.0   # shares its vector with min_down_time
            input = PowerSystemInput(
                name = "minup", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = fill(50.0, H, 1),
                temporal = TemporalConfig(H, 2, H, 0, H, H, 1, 1, 1),
                mode = "unit_commitment", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            @test constraint_by_name(model, "min_up_g1_b1_t4_tau3") !== nothing
            @test constraint_by_name(model, "min_up_g1_b1_t4_tau2") === nothing
            @test constraint_by_name(model, "min_down_g1_b1_t4_tau3") !== nothing
            @test constraint_by_name(model, "min_down_g1_b1_t4_tau2") === nothing
        end

        @testset "electrolyzer limited by rated power and ramp" begin
            # A paying electrolyzer (negative variable cost) used to absorb all
            # spare generation: its rated power was never enforced.
            H = 3
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            gen = mkgen("GasCC", "Non-renewable", "Gas", [12.0],
                        [3.0e-5], ones(H, 1); nb = 1, H = H)
            # 5 MW, ramp 0.4/h → at most 2 MW change per 1-h step.
            elz = ElectrolyzerConfig([5.0], [0.7], [0.6], 50.0, [0.4], [0.4],
                                     [0.0], [0.0], [0.0], [-5.0e-4], 0.0, [20.0])
            input = PowerSystemInput(
                name = "elz", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = reshape([10.0, 5.0, 5.0], H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                electrolyzer_config = elz,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            p = value.(vars.electrolyzer_power[1, 1, :])
            @test all(p .<= 5.0 + 1e-6)
            @test p ≈ [2.0, 4.0, 5.0] atol = 1e-6   # 12 MW gas − 10 MW load, then ramp
        end

        @testset "every electrolyzer is dispatched and reported" begin
            # Only the first electrolyzer of a system reached the model. Two
            # paying electrolyzers of 5 and 3 MW on 20 MW of spare gas: both
            # run at rated power, and each reports its own power and H2.
            H = 2
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            gen = mkgen("GasCC", "Non-renewable", "Gas", [30.0],
                        [3.0e-5], ones(H, 1); nb = 1, H = H)
            elz(rated, eff) = ElectrolyzerConfig([rated], [eff], [eff], 50.0, [0.0], [0.0],
                                                 [0.0], [0.0], [0.0], [-5.0e-4], 0.0, [20.0])
            input = PowerSystemInput(
                name = "elz2", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = reshape([10.0, 10.0], H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                electrolyzers = [elz(5.0, 0.7), elz(3.0, 0.5)],
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            res = extract_solution(model, vars, input)
            @test size(res.electrolyzer_power) == (2, 1, H)
            @test res.electrolyzer_power[1, 1, :] ≈ [5.0, 5.0] atol = 1e-6
            @test res.electrolyzer_power[2, 1, :] ≈ [3.0, 3.0] atol = 1e-6
            # kg/h = MW × 1000 × eff / (kWh/kg)
            @test res.electrolyzer_h2[1, 1, 1] ≈ 5.0 * 1000 * 0.7 / 50 atol = 1e-6
            @test res.electrolyzer_h2[2, 1, 1] ≈ 3.0 * 1000 * 0.5 / 50 atol = 1e-6
            @test res.total_generation ≈ 2 * (10.0 + 8.0) atol = 1e-6
        end

        @testset "development-mode transmission is bounded and charged" begin
            # tf_inv had no upper bound and no cost: the dispatch built any line for free.
            function dev_line(inv_max, inv_cost; base_cap = 10.0)
                H = 1; N = 2
                gas = mkgen("Gas", "Non-renewable", "Gas", [200.0, 0.0], [3.0e-5, 3.0e-5],
                            ones(H, N); nb = N, H = H)
                buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                         BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
                line = TransmissionLineData("L12", 1, 2, base_cap, 0.1, 0.0, 0.0,
                                            100.0, 220.0, 1, 50.0, "AC")
                net = NetworkConfig(N, N, buses, [1, 2], [0.0 10.0; 10.0 0.0],
                                    [0.0 100.0; 100.0 0.0], 100.0, 0.4, 220.0, 0.6, 1,
                                    inv_cost, inv_max, [line], TransformerData[],
                                    ACDCConverterData[], FrequencyConverterData[], 0.1)
                input = PowerSystemInput(
                    name = "dev", year = 2025, network = net, generators = [gas],
                    batteries = BatteryConfig[], demand = [0.0 50.0],
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    mode = "development", solver_name = "highs", verbose = false,
                    pwl_loss_segments = 0)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                res = extract_solution(model, vars, input)
                inv = sum(values(res.transfer_investment); init = 0.0)
                return inv, res.load_shed_total, res.cost_breakdown.investment_cost,
                       objective_value(model)
            end
            inv, shed, _, _ = dev_line([0.0, 0.0], [0.0, 0.0])
            @test inv ≈ 0.0 atol = 1e-6
            @test shed ≈ 40.0 atol = 1e-6
            # Bounded by the smaller end (25 MW), charged at the dearer one (2e-4 M$/MW)
            inv, shed, cost, obj = dev_line([25.0, 30.0], [1.0e-4, 2.0e-4])
            @test inv ≈ 25.0 atol = 1e-6
            @test shed ≈ 15.0 atol = 1e-6
            @test cost ≈ 25.0 * 2.0e-4 atol = 1e-9
            # In the objective it is scaled by the window's share of the year,
            # like generator investment (1 h of 8760 here)
            _, _, _, obj2 = dev_line([25.0, 30.0], [1.0e-4, 4.0e-4])
            @test obj2 - obj ≈ 25.0 * 2.0e-4 / 8760 atol = 1e-10
            # A 0-MW candidate line (e.g. a development-zone link) carries what
            # is built on it; its flow used to be fixed to 0 as "negligible"
            # while the investment was still bought.
            inv, shed, _, _ = dev_line([25.0, 30.0], [1.0e-4, 2.0e-4]; base_cap = 0.0)
            @test inv ≈ 25.0 atol = 1e-6
            @test shed ≈ 25.0 atol = 1e-6
        end

        @testset "transmission corridor charged once, at its dearer end" begin
            # Both directions of a corridor are held equal; each used to be
            # charged its own end's cost, so a MW of line cost c_i + c_j.
            inv = Dict((1, 2) => :a, (2, 1) => :b, (3, 1) => :zone, (2, 3) => :c)
            costs = [0.0, 80.0, 30.0]   # bus 1 left at 0 (not set)
            terms = Dict(ESFEX.corridor_cost_terms(inv, costs))
            @test terms == Dict(:a => 80.0, :zone => 30.0, :c => 80.0)
        end

        @testset "reserves are headroom, not energy" begin
            # Reserves used to enter the power balance as consumption: the model
            # generated the dynamic reserve as real energy (70 MW for a 50 MW load).
            H = 2
            function rgen(name, rated, fcost; minp = 0.0, start = 0.0)
                z = zeros(1)
                GeneratorConfig(
                    name, "Non-renewable", "Gas", [rated], [minp],
                    [0.5], [0.4], ones(1), ones(1), z, z, [start],
                    [fcost], z, z, z, z, z, ones(H, 1), true,
                    [30.0], z, z, z, 50.0, "AC",
                    z, z, z, z, zeros(H, 1), z, z, z, z, false, z, z, z,
                )
            end
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            function solve(gens, load, rs, rd; mode = "economic_dispatch")
                input = PowerSystemInput(
                    name = "res", year = 2025, network = net, generators = gens,
                    batteries = BatteryConfig[], demand = fill(load, H, 1),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    reserve_static_requirement = rs > 0 ? Dict(1 => rs) : Dict{Int,Float64}(),
                    reserve_dynamic_requirement = rd > 0 ? Dict(1 => rd) : Dict{Int,Float64}(),
                    reserve_static_default_ratio = 0.0, dynamic_reserve_contribution = 0.5,
                    loss_of_reserve_static = 1.0, loss_of_reserve_dynamic = 1.0,
                    loss_of_load_penalty = 100.0,
                    mode = mode, solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                gen = [sum(value(vars.gen_output[g, 1, t]) for t in 1:H) / H for g in 1:length(gens)]
                short = sum(value.(vars.reserve_static_loss) .+ value.(vars.reserve_dynamic_loss)) / H
                return gen, short, value.(vars.reserve_dynamic[1, :])
            end
            cheap(cap) = rgen("A", cap, 1.0e-5)
            dear(cap; kw...) = rgen("B", cap, 1.0e-4; kw...)

            # Ample headroom: both reserves held, nothing generated beyond the load.
            gen, short, rdyn = solve([cheap(100.0), dear(100.0)], 50.0, 20.0, 20.0)
            @test sum(gen) ≈ 50.0 atol = 1e-6
            @test short ≈ 0.0 atol = 1e-6
            @test all(rdyn .<= 20.0 + 1e-6)
            # Only 5 MW of headroom: dynamic reserve cannot exceed it.
            gen, short, _ = solve([cheap(60.0), dear(50.0)], 105.0, 0.0, 20.0)
            @test sum(gen) ≈ 105.0 atol = 1e-6
            @test short ≈ 15.0 atol = 1e-6
            # Static and dynamic share the same 25 MW of headroom.
            gen, short, _ = solve([cheap(60.0), dear(50.0)], 85.0, 20.0, 20.0)
            @test sum(gen) ≈ 85.0 atol = 1e-6
            @test short ≈ 15.0 atol = 1e-6
            # Unit commitment: an offline unit holds no spinning reserve, so B
            # (minimum 20 MW) must run to keep 30 MW free.
            gen, short, _ = solve([cheap(70.0), dear(50.0; minp = 0.4, start = 1.0e-3)],
                                  60.0, 30.0, 0.0; mode = "unit_commitment")
            @test gen ≈ [40.0, 20.0] atol = 1e-6
            @test short ≈ 0.0 atol = 1e-6
        end

        @testset "preventive N-1 shortfall is priced" begin
            # The objective was built before the N-1 constraints existed, so
            # their shortfall slacks cost nothing and N-1 had no effect. The
            # constraint also required the other units' OUTPUT to cover demand,
            # which penalised every MW of the largest unit; it is headroom now.
            H = 2
            function ngen(name, rated, fcost; start = 0.0, ramp = 1.0)
                z = zeros(1)
                GeneratorConfig(
                    name, "Non-renewable", "Gas", [rated], [0.0],
                    [0.5], [0.4], [ramp], [ramp], z, z, [start],
                    [fcost], z, z, z, z, z, ones(H, 1), true,
                    [30.0], z, z, z, 50.0, "AC",
                    z, z, z, z, zeros(H, 1), z, z, z, z, false, z, z, z,
                )
            end
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            function solve_n1(gens, load; mode = "economic_dispatch", scopf = false,
                              corrective = false, rtype = "largest_unit", rpct = 0.15)
                input = PowerSystemInput(
                    name = "n1", year = 2025, network = net, generators = gens,
                    batteries = BatteryConfig[], demand = fill(load, H, 1),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    reserve_static_default_ratio = 0.0, loss_of_reserve_static = 1.0,
                    loss_of_load_penalty = 100.0,
                    n1_security_enabled = true, n1_generation_enabled = true,
                    n1_scopf_enabled = scopf, n1_corrective_enabled = corrective,
                    n1_generation_reserve_type = rtype,
                    n1_generation_reserve_percentage = rpct,
                    mode = mode, solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                res = extract_solution(model, vars, input)
                gen = [value(vars.gen_output[g, 1, 1]) for g in 1:length(gens)]
                on = vars.gen_status === nothing ? nothing :
                    [value(vars.gen_status[g, 1, 1]) for g in 1:length(gens)]
                return gen, objective_value(model), res.cost_breakdown.n1_security_shortfall_cost, on
            end

            # The cheap 100 MW unit is the largest; the 60 MW unit can replace
            # its 50 MW, so the system is secure with the cheap unit running.
            gen, _, cost, _ = solve_n1([ngen("A", 100.0, 1.0e-5), ngen("B", 60.0, 1.0e-4)], 50.0)
            @test gen ≈ [50.0, 0.0] atol = 1e-6
            @test cost ≈ 0.0 atol = 1e-6
            # A 30 MW backup leaves 20 MW short each hour, charged at
            # loss_of_reserve_static and reported in the cost breakdown.
            gen, obj, cost, _ = solve_n1([ngen("A", 100.0, 1.0e-5), ngen("B", 30.0, 1.0e-4)], 50.0)
            @test cost ≈ 20.0 * H atol = 1e-6
            @test obj >= cost
            # Unit commitment: an offline unit cannot respond, so B is started
            # (despite its start-up cost) and kept idle.
            gen, _, cost, on = solve_n1([ngen("A", 100.0, 1.0e-5), ngen("B", 60.0, 1.0e-4; start = 1.0e-3)], 50.0;
                                        mode = "unit_commitment")
            @test gen ≈ [50.0, 0.0] atol = 1e-6
            @test on[2] ≈ 1.0 atol = 1e-6
            @test cost ≈ 0.0 atol = 1e-6

            # SCOPF uses the same headroom test; its old hard form (others'
            # output >= demand) forced the largest unit to 0.
            gen, _, cost, _ = solve_n1([ngen("A", 100.0, 1.0e-5), ngen("B", 60.0, 1.0e-4)], 50.0;
                                       scopf = true)
            @test gen ≈ [50.0, 0.0] atol = 1e-6
            @test cost ≈ 0.0 atol = 1e-6
            _, _, cost, _ = solve_n1([ngen("A", 100.0, 1.0e-5), ngen("B", 30.0, 1.0e-4)], 50.0;
                                     scopf = true)
            @test cost ≈ 20.0 * H atol = 1e-6
            # Corrective: B re-dispatches within one step of its ramp (0.25 pu/h
            # of 60 MW = 15 MW), so A may carry only what B can pick up.
            gen, _, cost, _ = solve_n1([ngen("A", 100.0, 1.0e-5), ngen("B", 60.0, 1.0e-4; ramp = 0.25)],
                                       50.0; scopf = true, corrective = true)
            @test gen ≈ [15.0, 35.0] atol = 1e-6
            @test cost ≈ 0.0 atol = 1e-6

            # Reserve type "percentage" (was ignored: always largest unit).
            # One 60 MW unit serving 50 MW: losing it leaves nothing (50 MW
            # short), but a 50 % reserve only needs 25 MW of headroom, of
            # which the unit itself provides 10.
            _, _, cost, _ = solve_n1([ngen("A", 60.0, 1.0e-5)], 50.0)
            @test cost ≈ 50.0 * H atol = 1e-6
            _, _, cost, _ = solve_n1([ngen("A", 60.0, 1.0e-5)], 50.0;
                                     rtype = "percentage", rpct = 0.5)
            @test cost ≈ 15.0 * H atol = 1e-6
            _, _, cost, _ = solve_n1([ngen("A", 60.0, 1.0e-5)], 50.0;
                                     rtype = "percentage", rpct = 0.2)
            @test cost ≈ 0.0 atol = 1e-6
        end

        @testset "ACOPF line limits follow scheduled outages" begin
            # The AC thermal limits ignored the outage masks (and investment)
            # that the DC limits apply. Line at 20 % in step 1: 20 MVA of 50 MW.
            H = 2
            gen = mkgen("Gas", "Non-renewable", "Gas", [200.0, 0.0], [1.0e-5, 1.0e-5],
                        ones(H, 2); nb = 2, H = H)
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
            line = TransmissionLineData("L12", 1, 2, 100.0, 0.1, 0.01, 0.0, 100.0, 220.0, 1, 50.0, "AC")
            net2 = NetworkConfig(2, 2, buses, [1, 2], [0.0 100.0; 100.0 0.0],
                                 [0.0 100.0; 100.0 0.0], 100.0, 0.4, 220.0, 0.6, 1,
                                 [0.0, 0.0], [0.0, 0.0], [line], TransformerData[],
                                 ACDCConverterData[], FrequencyConverterData[], 0.1)
            input = PowerSystemInput(
                name = "ac", year = 2025, network = net2, generators = [gen],
                batteries = BatteryConfig[], demand = [0.0 50.0; 0.0 50.0],
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                outage_masks = Dict{String, Any}("line" => Dict("L12" => [0.2, 1.0])),
                power_flow_mode = "acopf_polar", mode = "economic_dispatch",
                solver_name = "ipopt", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) in ("LOCALLY_SOLVED", "OPTIMAL")
            res = extract_solution(model, vars, input)
            @test sum(res.load_shed[:, 1]) ≈ 30.0 atol = 0.5
            @test sum(res.load_shed[:, 2]) ≈ 0.0 atol = 0.01
        end

        @testset "transmission N-1 sees every parallel line" begin
            # Two identical 60 MW circuits carry 100 MW. N-1 limits used to read
            # only the first circuit of a bus pair (against the pair's total
            # capacity), and SCOPF was hard, forcing load shedding.
            H = 2
            gen = mkgen("Gas", "Non-renewable", "Gas", [200.0, 0.0], [1.0e-5, 1.0e-5],
                        ones(H, 2); nb = 2, H = H)
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
            lines = [TransmissionLineData(id, 1, 2, 60.0, 0.1, 0.0, 0.0, 100.0, 220.0, 1, 50.0, "AC")
                     for id in ("L1", "L2")]
            net2 = NetworkConfig(2, 2, buses, [1, 2], [0.0 120.0; 120.0 0.0],
                                 [0.0 100.0; 100.0 0.0], 100.0, 0.4, 220.0, 0.6, 1,
                                 [0.0, 0.0], [0.0, 0.0], lines, TransformerData[],
                                 ACDCConverterData[], FrequencyConverterData[], 0.1)
            function line_n1(scopf)
                input = PowerSystemInput(
                    name = "n1l", year = 2025, network = net2, generators = [gen],
                    batteries = BatteryConfig[], demand = fill(0.0, H, 2) .+ [0.0 100.0],
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    reserve_static_default_ratio = 0.0, loss_of_reserve_static = 1.0,
                    loss_of_load_penalty = 100.0, pwl_loss_segments = 0,
                    n1_security_enabled = true, n1_transmission_enabled = true,
                    n1_generation_enabled = false, n1_scopf_enabled = scopf,
                    n1_transmission_reserve_factor = 0.7,
                    mode = "economic_dispatch", solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                res = extract_solution(model, vars, input)
                return res.load_shed_total, res.cost_breakdown.n1_security_shortfall_cost
            end
            # Preventive: each circuit at most 0.7 × 60 = 42 MW, 8 MW over each
            shed, cost = line_n1(false)
            @test shed ≈ 0.0 atol = 1e-6
            @test cost ≈ 16.0 * H atol = 1e-6
            # SCOPF: losing one circuit puts all 100 MW on the other, 40 MW over
            # its 60 MW, for either outage; priced, not shed.
            shed, cost = line_n1(true)
            @test shed ≈ 0.0 atol = 1e-6
            @test cost ≈ 80.0 * H atol = 1e-6
        end

        @testset "zero ramp rate means no ramp limit" begin
            # A 0 ramp used to freeze the unit (|Δp| <= 0); Studio generators
            # defaulted to it. The unit must follow load instead.
            H = 3
            gen = mkgen("GasCC", "Non-renewable", "Gas", [100.0],
                        [3.0e-5], ones(H, 1); nb = 1, H = H)
            gen.ramp_up[1] = 0.0
            gen.ramp_down[1] = 0.0
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            input = PowerSystemInput(
                name = "ramp0", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = reshape([20.0, 80.0, 40.0], H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            res = extract_solution(model, vars, input)
            @test res.load_shed_total ≈ 0.0 atol = 1e-6
            @test vec(res.gen_output[1, 1, :]) ≈ [20.0, 80.0, 40.0] atol = 1e-6
        end

        @testset "EV driving drains the fleet battery" begin
            # Driving energy must come out of the fleet SOC: over a cyclic
            # window the grid supplies driving/η_c, and V2G cannot resell it.
            H = 4
            gen = mkgen("GasCC", "Non-renewable", "Gas", [100.0],
                        [3.0e-5], ones(H, 1); nb = 1, H = H)
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            drive = fill(2.0, H, 1)
            ev = EVConfig([1000.0], 50.0, 7.0, 7.0, 0.9, 0.9, 0.2, 1.0, 0.5,
                          ones(H, 1), drive, 0.0, 1.0e6, Float64[])
            input = PowerSystemInput(
                name = "ev", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = fill(50.0, H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                ev_config = ev,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)

            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            res = extract_solution(model, vars, input)
            @test sum(res.ev_loss) ≈ 0.0 atol = 1e-6
            @test sum(res.ev_v2g) ≈ 0.0 atol = 1e-6
            @test sum(res.ev_charging) ≈ sum(drive) / 0.9 atol = 1e-4
            @test res.total_generation ≈ 4 * 50.0 + sum(drive) / 0.9 atol = 1e-4

            # A V2G payment is a transfer, not a system saving: even a large
            # one must not make cycling the fleet (19 % round-trip loss) pay.
            ev_paid = EVConfig([1000.0], 50.0, 7.0, 7.0, 0.9, 0.9, 0.2, 1.0, 0.5,
                               ones(H, 1), drive, 1.0, 1.0e6, Float64[])
            inp_paid = PowerSystemInput(
                name = "ev_paid", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = fill(50.0, H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                ev_config = ev_paid,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model_p, vars_p = create_power_system(inp_paid)
            optimize!(model_p)
            res_p = extract_solution(model_p, vars_p, inp_paid)
            @test sum(res_p.ev_v2g) ≈ 0.0 atol = 1e-6
            @test res_p.objective ≈ res.objective rtol = 1e-6
        end

        @testset "flexible demand shifting moves energy" begin
            # Shift variables used to sit outside the power balance (shifting
            # did nothing), and a curtailment "benefit" lowered the objective
            # without changing the dispatch.
            function peak_mwh(delay; overlap = 0)
                H = 4
                cheap = mkgen("Base", "Non-renewable", "Gas", [80.0], [1.0e-5],
                              ones(H, 1); nb = 1, H = H)
                peak = mkgen("Peak", "Non-renewable", "Diesel", [100.0], [1.0e-3],
                             ones(H, 1); nb = 1, H = H)
                bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
                net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                    100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                    TransmissionLineData[], TransformerData[],
                                    ACDCConverterData[], FrequencyConverterData[], 0.1)
                input = PowerSystemInput(
                    name = "shift", year = 2025, network = net, generators = [cheap, peak],
                    batteries = BatteryConfig[], demand = reshape([100.0, 100.0, 60.0, 60.0], H, 1),
                    sectoral_demand = Dict("flex" => reshape([40.0, 40.0, 0.0, 0.0], H, 1),
                                           "rest" => reshape([60.0, 60.0, 60.0, 60.0], H, 1)),
                    sectoral_criticality = Dict("flex" => 1.0, "rest" => 1.0),
                    sectoral_flexibility_ratio = Dict("flex" => 0.5),
                    sectoral_delay_tolerance = Dict("flex" => delay),
                    temporal = TemporalConfig(H, 1, H, overlap, H, H, 1, 1, 1),
                    mode = "economic_dispatch", solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                res = extract_solution(model, vars, input)
                @test res.total_generation ≈ 320.0 atol = 1e-6   # energy is moved, not removed
                @test res.load_shed_total ≈ 0.0 atol = 1e-6
                @test res.cost_breakdown.flexible_demand_benefit ≈ 0.0 atol = 1e-12
                return sum(res.gen_output[2, :, :])
            end
            # 20 MW leave each of the first two steps for the last two: flat 80 MW
            @test peak_mwh(2) ≈ 0.0 atol = 1e-6
            @test peak_mwh(0) ≈ 40.0 atol = 1e-6
            # Steps 3-4 are the overlap of a rolling window: no shifting into them
            @test peak_mwh(2; overlap = 2) ≈ 40.0 atol = 1e-6

            # Same through the DC network balance: generation at bus 1, load at bus 2
            H = 4; N = 2
            cheap = mkgen("Base", "Non-renewable", "Gas", [80.0, 0.0], [1.0e-5, 1.0e-5],
                          ones(H, N); nb = N, H = H)
            peak = mkgen("Peak", "Non-renewable", "Diesel", [0.0, 100.0], [1.0e-3, 1.0e-3],
                         ones(H, N); nb = N, H = H)
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
            line = TransmissionLineData("L12", 1, 2, 200.0, 0.1, 0.0, 0.0,
                                        100.0, 220.0, 1, 50.0, "AC")
            net2 = NetworkConfig(N, N, buses, [1, 2], [0.0 200.0; 200.0 0.0],
                                 [0.0 100.0; 100.0 0.0], 100.0, 0.4, 220.0, 0.6, 1,
                                 [0.0, 0.0], [0.0, 0.0], [line], TransformerData[],
                                 ACDCConverterData[], FrequencyConverterData[], 0.1)
            input = PowerSystemInput(
                name = "shift_dc", year = 2025, network = net2, generators = [cheap, peak],
                batteries = BatteryConfig[], demand = [0.0 100.0; 0.0 100.0; 0.0 60.0; 0.0 60.0],
                sectoral_demand = Dict("flex" => [0.0 40.0; 0.0 40.0; 0.0 0.0; 0.0 0.0],
                                       "rest" => [0.0 60.0; 0.0 60.0; 0.0 60.0; 0.0 60.0]),
                sectoral_criticality = Dict("flex" => 1.0, "rest" => 1.0),
                sectoral_flexibility_ratio = Dict("flex" => 0.5),
                sectoral_delay_tolerance = Dict("flex" => 2),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1), pwl_loss_segments = 0,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            res = extract_solution(model, vars, input)
            @test sum(res.gen_output[2, :, :]) ≈ 0.0 atol = 1e-6
            @test res.total_generation ≈ 320.0 atol = 1e-6
        end

        @testset "fuel bought by primary energy is not charged again" begin
            # With the primary-energy block coupled, a unit's fuel is paid
            # through the block's fuel supply; the dispatch also charged its
            # fuel_cost per MWh.
            H = 2
            gen = mkgen("GasCC", "Non-renewable", "Gas", [100.0], [5.0e-5],
                        ones(H, 1); nb = 1, H = H)
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            input = PowerSystemInput(
                name = "pe", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = fill(50.0, H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            v = vars.gen_output[1, 1, 1]
            before = coefficient(objective_function(model), v)
            fuel = FuelConfig("Gas", 1.0e-5, 0.0, 10.0, 0.0, [1.0e6], [0.0], [0.0], 0.0,
                              [0.0], 0.0, 0.0, 0.0)
            pe_in = PrimaryEnergyInput(2025, 2025, 1, H, [fuel],
                Dict{String, FuelInfrastructureConfig}(), NonElectricDemandConfig[],
                TransportRoute[], Dict(1 => ("Gas", 5.0, 10.0, 0.5)), 24, 8760, 0.05,
                1.0, 1.0, "economic_dispatch", Dict{String, Any}(), nothing, false,
                nothing, Dict(1 => [100.0]), nothing)
            pe_vars, _, _ = ESFEX.create_primary_energy_model(model, pe_in)
            ESFEX.couple_primary_energy_to_power_system!(model, pe_vars, vars, pe_in;
                generators = input.generators)
            @test before - coefficient(objective_function(model), v) ≈ 5.0e-5
            @test coefficient(model.ext[:cost_expressions][:fuel_cost], v) ≈ 0.0 atol = 1e-15
        end

        @testset "primary energy: one tank per node, shipments reach it" begin
            # Node 1 buys fuel and ships it 100 km to node 2 (no supply, no
            # tank) whose 10/day non-electric demand it serves. Shipped fuel
            # used to enter only a periodic balance, never node 2's tank.
            function solve_route(transit_days)
                fuel = FuelConfig("Gas", 1.0, 0.0, 10.0, 0.0, [87600.0, 0.0], [100.0, 0.0],
                                  [0.0, 0.0], 0.0, [0.0, 0.0], 0.0, 0.0, transit_days)
                route = TransportRoute("r", 1, 2, 100.0,
                    Dict("Gas" => FuelRouteParams(50.0, 0.0, 0.0)))
                ne = NonElectricDemandConfig("Industrial", "Gas", [0.0, 3650.0], 0.0, ones(12))
                inp = PrimaryEnergyInput(2025, 2025, 2, 72, [fuel],
                    Dict{String, FuelInfrastructureConfig}(), [ne], [route],
                    Dict{Int, Tuple{String, Float64, Float64, Float64}}(), 24, 72, 0.05,
                    1000.0, 1.0, "economic_dispatch", Dict{String, Any}(), nothing, false,
                    nothing, Dict{Int, Vector{Float64}}(), nothing, 300.0, 1, 0)
                m = ESFEX.Model(create_optimizer(solver_name = "highs"))
                ESFEX.set_silent(m)
                vars, temporal, prices = ESFEX.create_primary_energy_model(m, inp)
                terms = ESFEX.get_primary_energy_objective_terms(vars, inp, temporal, prices)
                ESFEX.@objective(m, Min, sum(values(terms)))
                optimize!(m)
                short = value.(vars.ne_demand_shortfall_periodic[("Gas", "Industrial")])
                return [sum(short[:, p]) for p in 1:3],
                       sum(value.(vars.fuel_loss_of_supply_hourly["Gas"]))
            end
            short, loss = solve_route(0.0)
            @test sum(short) ≈ 0.0 atol = 1e-6
            @test loss ≈ 0.0 atol = 1e-6
            # 1 day per 100 km: day 1's demand waits for the first shipment
            short, loss = solve_route(1.0)
            @test short ≈ [10.0, 0.0, 0.0] atol = 1e-6
            @test loss ≈ 0.0 atol = 1e-6
        end

        @testset "primary energy: fuel in transit reaches the next window" begin
            # Rolling horizon of 48 steps keeping 24, 1 day of transit over
            # 100 km. Node 1 has the supply, node 2 (no tank) a 10/day
            # non-electric demand. Fuel shipped in a window's committed day
            # arrives after the commit point; it was paid and never delivered,
            # so node 2 was short on the first day of every window.
            function pe_window(init2, arrivals, offset)
                fuel = FuelConfig("Gas", 1.0, 0.0, 10.0, 0.0, [87600.0, 0.0], [100.0, 0.0],
                                  [0.0, 0.0], 0.0, [0.0, 0.0], 0.0, 0.0, 1.0)
                route = TransportRoute("r", 1, 2, 100.0,
                    Dict("Gas" => FuelRouteParams(50.0, 0.0, 0.0)))
                ne = NonElectricDemandConfig("Industrial", "Gas", [0.0, 3650.0], 0.0, ones(12))
                inp = PrimaryEnergyInput(2025, 2025, 2, 48, [fuel],
                    Dict{String, FuelInfrastructureConfig}(), [ne], [route],
                    Dict{Int, Tuple{String, Float64, Float64, Float64}}(), 24, 48, 0.05,
                    1000.0, 1.0, "economic_dispatch", Dict{String, Any}(),
                    Dict("Gas" => [0.0, init2]), false,
                    nothing, Dict{Int, Vector{Float64}}(), nothing, 300.0, 1, offset,
                    arrivals)
                m = ESFEX.Model(create_optimizer(solver_name = "highs"))
                ESFEX.set_silent(m)
                vars, temporal, prices = ESFEX.create_primary_energy_model(m, inp)
                terms = ESFEX.get_primary_energy_objective_terms(vars, inp, temporal, prices)
                ESFEX.@objective(m, Min, sum(values(terms)))
                optimize!(m)
                @test string(termination_status(m)) == "OPTIMAL"
                short = value.(vars.ne_demand_shortfall_periodic[("Gas", "Industrial")])
                level = value.(vars.fuel_storage_level_hourly["Gas"])
                return short[2, 1], level[2, 25], in_transit_arrivals(vars, inp, temporal, 24)
            end
            init2, arrivals = 0.0, nothing
            short_day1 = Float64[]
            for w in 1:4
                s, init2, arrivals = pe_window(init2, arrivals, (w - 1) * 24)
                push!(short_day1, s)
                # day 1's shipment is on the road at the commit point: 10
                # units into the tank after its 0.8 inflow efficiency
                @test sum(arrivals["Gas"]) ≈ 10.0 / 0.8 atol = 1e-6
                @test size(arrivals["Gas"]) == (2, 24)
            end
            @test short_day1[1] ≈ 10.0 atol = 1e-6       # nothing on the way yet
            @test short_day1[2:end] ≈ zeros(3) atol = 1e-6
        end

        @testset "primary energy: a unit's fuel covers all its buses at a node" begin
            # One unit at two buses of one node, 100 MWh of load at 5 MWh per
            # unit of fuel: 20 units. One constraint per bus against the same
            # node fuel bought only 10 (the largest bus).
            H = 2; nb = 2
            gen = mkgen("Gas", "Non-renewable", "Gas", [50.0, 50.0],
                        fill(5.0e-5, nb), ones(H, nb); nb = nb, H = H)
            b1 = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 0.5)
            b2 = BusData(2, 1, 220.0, 50.0, "AC", "pq", "mixed", 0.5)
            net = NetworkConfig(1, 2, [b1, b2], [1, 1], zeros(2, 2), zeros(2, 2),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            input = PowerSystemInput(
                name = "pe", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = fill(100.0, H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            fuel = FuelConfig("Gas", 1.0e-5, 0.0, 10.0, 0.0, [1.0e6], [0.0], [0.0], 0.0,
                              [0.0], 0.0, 0.0, 0.0)
            pe_in = PrimaryEnergyInput(2025, 2025, 1, H, [fuel],
                Dict{String, FuelInfrastructureConfig}(), NonElectricDemandConfig[],
                TransportRoute[], Dict(1 => ("Gas", 5.0, 10.0, 0.5)), 24, 8760, 0.05,
                1.0, 1.0, "economic_dispatch", Dict{String, Any}(), nothing, false,
                nothing, Dict(1 => [100.0]), nothing)
            pe_vars, temporal, prices = ESFEX.create_primary_energy_model(model, pe_in)
            ESFEX.couple_primary_energy_to_power_system!(model, pe_vars, vars, pe_in;
                bus_to_node = [1, 1], generators = input.generators)
            terms = ESFEX.get_primary_energy_objective_terms(pe_vars, pe_in, temporal, prices)
            ESFEX.@objective(model, Min, objective_function(model) + sum(values(terms)))
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            @test sum(value(vars.gen_output[1, b, 1]) for b in 1:2) ≈ 100.0 atol = 1e-6
            @test value(pe_vars.fuel_for_power_hourly[1][1, 1]) ≈ 20.0 atol = 1e-6
            # 40 units burnt, bought through the tank's 0.8 inflow efficiency
            @test sum(value.(pe_vars.fuel_supply_periodic["Gas"])) ≈ 40.0 / 0.8 atol = 1e-6
        end

        @testset "electrolyzer hydrogen fills the Hydrogen tank" begin
            # Step 1: 60 MW of sun, 5 MW of load, a 50 MW electrolyzer. Step 2:
            # no sun; a hydrogen turbine serves the 5 MW. The hydrogen has no
            # source but the electrolyzer: it used to be a plain load, and
            # the turbine ran on fuel shortfall.
            H = 2
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            sun = mkgen("PV", "Renewable", "Sun", [60.0], [0.0],
                        reshape([1.0, 0.0], H, 1); nb = 1, H = H)
            h2gt = mkgen("H2GT", "Non-renewable", "Hydrogen", [10.0], [0.0],
                         ones(H, 1); nb = 1, H = H)
            # 13 kg per MWh (1000 × 0.65 / 50)
            elz = ElectrolyzerConfig([50.0], [0.65], [0.65], 50.0, [0.0], [0.0],
                                     [0.0], [0.0], [0.0], [1.0e-9], 0.0, [20.0])
            input = PowerSystemInput(
                name = "h2", year = 2025, network = net, generators = [sun, h2gt],
                batteries = BatteryConfig[], demand = fill(5.0, H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                electrolyzers = [elz],
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            # Hydrogen in tonnes: 33.3 MWh/t, turbine 50 % → 16.65 MWh_e per t
            h2 = FuelConfig("Hydrogen", 0.0, 0.0, 33.3, 0.0, [0.0], [10.0], [0.0], 0.0,
                            [0.0], 0.0, 0.0, 0.0)
            pe_in = PrimaryEnergyInput(2025, 2025, 1, H, [h2],
                Dict{String, FuelInfrastructureConfig}(), NonElectricDemandConfig[],
                TransportRoute[], Dict(2 => ("Hydrogen", 16.65, 33.3, 0.5)), 24, 8760, 0.05,
                1.0, 1.0, "economic_dispatch", Dict{String, Any}(), nothing, false,
                nothing, Dict(2 => [10.0]), nothing)
            pe_vars, temporal, prices = ESFEX.create_primary_energy_model(model, pe_in)
            ESFEX.couple_primary_energy_to_power_system!(model, pe_vars, vars, pe_in;
                generators = input.generators, electrolyzers = input.electrolyzers)
            terms = ESFEX.get_primary_energy_objective_terms(pe_vars, pe_in, temporal, prices)
            ESFEX.@objective(model, Min, objective_function(model) + sum(values(terms)))
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            @test value(vars.gen_output[2, 1, 2]) ≈ 5.0 atol = 1e-6
            @test sum(value.(vars.load_shed)) ≈ 0.0 atol = 1e-6
            @test sum(value.(pe_vars.fuel_loss_of_supply_hourly["Hydrogen"])) ≈ 0.0 atol = 1e-6
            @test sum(value.(pe_vars.fuel_excess_supply_periodic["Hydrogen"])) ≈ 0.0 atol = 1e-6
            # 5 MWh / 16.65 = 0.3 t used, made in step 1 (the tank keeps 0.8
            # of what enters)
            p1 = value(vars.electrolyzer_power[1, 1, 1])
            @test p1 * 13.0 / 1000 * 0.8 ≥ 5.0 / 16.65 - 1e-6
            @test value(pe_vars.fuel_storage_level_hourly["Hydrogen"][1, 2]) ≥
                  5.0 / 16.65 - 1e-6
            # The old fixed-production inputs are rejected, not ignored
            bad = PrimaryEnergyInput(2025, 2025, 1, H, [h2],
                Dict{String, FuelInfrastructureConfig}(), NonElectricDemandConfig[],
                TransportRoute[], Dict{Int, Tuple{String, Float64, Float64, Float64}}(),
                24, 8760, 0.05, 1.0, 1.0, "economic_dispatch", Dict{String, Any}(),
                nothing, false, ones(H, 1), Dict{Int, Vector{Float64}}(), nothing)
            @test_throws ArgumentError ESFEX.create_primary_energy_model(
                ESFEX.Model(create_optimizer(solver_name = "highs")), bad)
        end

        @testset "primary energy: tank dispatch rate per hour, only where a tank is" begin
            # 2 h steps, rate 0.1 of the tank per hour. Node 1 (100-unit tank,
            # full) uses 15 per step and ships 5 per step's demand to node 2
            # (no tank). The rate capped node 1 at 0.1 × 100 = 10 per step
            # (not per hour) and node 2 at 0.1 × 0 = 0.
            function rate_short(rate)
                fuel = FuelConfig("Gas", 1.0, 0.0, 10.0, 0.0, [0.0, 0.0], [100.0, 0.0],
                                  [1.0, 0.0], 0.0, [0.0, 0.0], 0.0, 0.0, 0.0)
                route = TransportRoute("r", 1, 2, 100.0,
                    Dict("Gas" => FuelRouteParams(1000.0, 0.0, 0.0)))
                infra = Dict("Gas" => FuelInfrastructureConfig(
                    1000.0, 0.0, 0.0, 0.0, 0.0, 1.0, 20.0, 30.0, rate))
                # 15 and 5 per 2-h step: annual × 4 h / 8760 h per 2-step period
                ne = NonElectricDemandConfig("Industrial", "Gas",
                                             [15.0, 5.0] .* (2 * 8760 / 4), 0.0, ones(12))
                inp = PrimaryEnergyInput(2025, 2025, 2, 2, [fuel], infra, [ne], [route],
                    Dict{Int, Tuple{String, Float64, Float64, Float64}}(), 2, 2, 0.05,
                    1000.0, 1.0, "economic_dispatch", Dict{String, Any}(), nothing, false,
                    nothing, Dict{Int, Vector{Float64}}(), nothing, 300.0, 2, 0)
                m = ESFEX.Model(create_optimizer(solver_name = "highs"))
                ESFEX.set_silent(m)
                vars, temporal, prices = ESFEX.create_primary_energy_model(m, inp)
                terms = ESFEX.get_primary_energy_objective_terms(vars, inp, temporal, prices)
                ESFEX.@objective(m, Min, sum(values(terms)))
                optimize!(m)
                @test string(termination_status(m)) == "OPTIMAL"
                return sum(value.(vars.fuel_loss_of_supply_hourly["Gas"])) +
                       sum(value.(vars.ne_demand_shortfall_periodic[("Gas", "Industrial")])) +
                       sum(value.(vars.fuel_excess_supply_periodic["Gas"]))
            end
            @test rate_short(-1.0) ≈ 0.0 atol = 1e-6   # no rate configured
            @test rate_short(0.1) ≈ 0.0 atol = 1e-6    # 20 per step at node 1
            @test rate_short(0.05) ≈ 2 * 5.0 atol = 1e-6  # 10 per step: 5 short per step
        end

        @testset "master charges battery maintenance and scales candidate costs" begin
            # One representative day stands for 365: every operating cost of
            # the day enters the objective × 365. The battery maintenance was
            # missing from the master (the dispatch charges it on charge and
            # discharge), and the candidate renewables' curtailment penalty
            # was added once instead of × 365.
            H = 24
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-5],
                        ones(H, 1); nb = 1, H = H)
            bat = BatteryConfig("Li1", [40.0], [20.0], [20.0], [0.95], [0.95],
                                [0.0], [1.0], [0.5], [0.0], [0.0], [0.0], [0.0],
                                [0.0], [10.0], [0.0], [0.0], 0.0, 100.0, [2.0e-6],
                                [0.0], false, "AC", [0.0], [0.0], [0.0])
            o = [0.0]
            pv = TechnologyConfig("PV", "Renewable", "Sun", [1.0], [50.0], ones(H, 1),
                                  [1.0], [1.0], [1.0], [1.0], o, o, o, o, o, o, o, o,
                                  [25.0], o, o, 50.0, "AC", false, [1.0])
            li = BatteryTechnologyConfig("LiNew", [1.0], [1.0], [10.0], [40.0], 1.0, 8.0,
                                         [0.95], [0.95], o, [15.0], [0.5], [0.0], [1.0],
                                         [3.0e-6], o, [4.0e-6], false, "AC", o, [1.0])
            function master(; use_tsam = false, tres = 1)
                inp = MasterProblemInput(
                    temporal_resolution_hours = tres,
                    years = [2025], network = net, generators = [gas],
                    batteries = [bat], technologies = [pv], battery_technologies = [li],
                    base_demand = fill(50.0, H, 1),
                    target_re_penetration = 0.0, initial_re_penetration = 0.0,
                    representative_days_per_year = 1, min_day_separation = 1,
                    curtailment_cost = 7.0e-6, max_curtailment_ratio = 0.5,
                    use_tsam = use_tsam,
                    tsam_period_start_hours = use_tsam ? [[1]] : Vector{Int}[],
                    tsam_period_weights = use_tsam ? [[365.0]] : Vector{Float64}[],
                    tsam_chronological_order = use_tsam ? [[1]] : Vector{Int}[],
                    verbose = false)
                model, _, _ = create_master_problem(inp; use_representative_days = true)
                obj = objective_function(model)
                coef(pfx) = coefficient(obj,
                    only(v for v in all_variables(model) if name(v) == pfx))
                return coef
            end
            # (TSAM missed the step length on the curtailment penalty)
            for tsam in (false, true), tres in (1, 2)
                coef = master(use_tsam = tsam, tres = tres)
                k = 365 * tres
                @test coef("op_y1_d1_bat_ch[1,1,1]") ≈ k * 2.0e-6 rtol = 1e-9
                @test coef("op_y1_d1_bat_dch[1,1,1]") ≈ k * 2.0e-6 rtol = 1e-9
                @test coef("bat_tech_ch_y1_d1[1,1,1]") ≈ k * 3.0e-6 rtol = 1e-9
                @test coef("bat_tech_dis_y1_d1[1,1,1]") ≈ k * 7.0e-6 rtol = 1e-9
                # PV output earns back the curtailment it avoids, × 365 × step
                @test coef("tech_out_y1_d1[1,1,1]") ≈ -k * 7.0e-6 rtol = 1e-9
            end
        end

        @testset "CO2 factor is per MWh of fuel" begin
            # 0.2 t per MWh of fuel at 50 % rated efficiency is 0.4 t per MWh
            # of electricity; the factor used to be applied per MWh_e.
            H = 2
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-5],
                        ones(H, 1); nb = 1, H = H)
            @test gas.efficiency_rated == [0.5]
            @test ESFEX.co2_per_mwh(Dict("Gas" => 0.2), gas, 1) ≈ 0.4
            input = PowerSystemInput(
                name = "co2f", year = 2025, network = net, generators = [gas],
                batteries = BatteryConfig[], demand = fill(50.0, H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                fuel_co2 = Dict("Gas" => 0.2), co2_cost = 1.0e-5,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            res = extract_solution(model, vars, input)
            # 100 MWh served → 40 t
            @test res.total_co2 ≈ 40.0 rtol = 1e-6
            @test res.cost_breakdown.co2_emission_cost ≈ 40.0 * 1.0e-5 rtol = 1e-6
            @test sum(value.(vars.co2_emissions)) ≈ 40.0 rtol = 1e-6
        end

        @testset "energy terms use the step length (2 h steps)" begin
            H = 2
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            temporal = TemporalConfig(H, 2, H, 0, H, H, 1, 1, 1)

            # CO2 budget: the window's share is 100 t (4 h of 8760 h at
            # 219 000 t/yr). Emissions are MW × 2 h × 1 t/MWh; they used to be
            # summed in MW, letting coal emit 200 t.
            function co2_run(penalty)
                coal = mkgen("Coal", "Non-renewable", "Coal", [100.0], [1.0e-5],
                             ones(H, 1); nb = 1, H = H)
                gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4],
                            ones(H, 1); nb = 1, H = H)
                input = PowerSystemInput(
                    name = "co2", year = 2025, network = net, generators = [coal, gas],
                    batteries = BatteryConfig[], demand = fill(50.0, H, 1),
                    temporal = temporal, fuel_co2 = Dict("Coal" => 0.5),   # / η 0.5 = 1 t/MWh_e
                    co2_budget = 100.0 * 8760 / 4, co2_budget_violation_penalty = penalty,
                    mode = "economic_dispatch", solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                res = extract_solution(model, vars, input)
                return 2.0 * sum(res.gen_output[1, :, :]), res.cost_breakdown.co2_emission_cost
            end
            coal_mwh, _ = co2_run(5e-4)
            @test coal_mwh ≈ 100.0 atol = 1e-6
            # A negligible penalty: coal serves all 200 MWh and pays for the
            # 100 t over budget once, not once per hour of the step.
            coal_mwh, co2_cost = co2_run(1e-9)
            @test coal_mwh ≈ 200.0 atol = 1e-6
            @test co2_cost ≈ 100.0 * 1e-9 rtol = 1e-6

            # EV fleet: 15 MWh on board, 10 MW of driving for one 2 h step with
            # no generation to charge from, so 5 MWh (2.5 MW) goes unserved.
            # The SOC used to move by MW, as if the step were 1 h.
            gen = mkgen("GasCC", "Non-renewable", "Gas", [100.0], [3.0e-5],
                        reshape([0.0, 1.0], H, 1); nb = 1, H = H)
            ev = EVConfig([1000.0], 30.0, 20.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.5,
                          ones(H, 1), reshape([10.0, 0.0], H, 1), 0.0, 1.0e-3, [15.0])
            input = PowerSystemInput(
                name = "ev2h", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = zeros(H, 1),
                temporal = temporal, ev_config = ev,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            res = extract_solution(model, vars, input)
            @test sum(res.ev_loss) ≈ 2.5 atol = 1e-6
            @test res.ev_soc[1, 1] ≈ 15.0 atol = 1e-6   # start of each step
            @test res.ev_soc[1, 2] ≈ 0.0 atol = 1e-6

            # Result totals are energy (MWh) and mass (t) over the window:
            # step rates × 2 h. They used to be summed in MW (and t/h), so
            # with 24 h steps the yearly totals came out 24× too small.
            coal = mkgen("Coal", "Non-renewable", "Coal", [40.0], [1.0e-5],
                         ones(H, 1); nb = 1, H = H)
            input = PowerSystemInput(
                name = "tot2h", year = 2025, network = net, generators = [coal],
                batteries = BatteryConfig[], demand = fill(50.0, H, 1),
                temporal = temporal, fuel_co2 = Dict("Coal" => 0.5),   # 1 t/MWh_e
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            res = extract_solution(model, vars, input)
            @test res.total_demand ≈ 200.0 atol = 1e-6       # 50 MW × 2 steps × 2 h
            @test res.total_generation ≈ 160.0 atol = 1e-6
            @test res.load_shed_total ≈ 40.0 atol = 1e-6
            @test res.total_co2 ≈ 160.0 rtol = 1e-6
            # The node array stays a rate (t/h), like the MW arrays.
            @test sum(res.co2_emissions) * 2.0 ≈ res.total_co2 rtol = 1e-6

            solar = mkgen("Solar", "Renewable", "Solar", [100.0], [0.0],
                          ones(H, 1); nb = 1, H = H)
            input = PowerSystemInput(
                name = "curt2h", year = 2025, network = net, generators = [solar],
                batteries = BatteryConfig[], demand = fill(50.0, H, 1),
                temporal = temporal,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            res = extract_solution(model, vars, input)
            @test res.total_curtailment ≈ 200.0 atol = 1e-6  # 50 MW spilled × 2 × 2 h
            @test res.re_penetration ≈ 1.0 atol = 1e-6
        end

        @testset "one CO2 budget per system" begin
            # Two systems, one node each, no link. Each has a 40 t share of
            # its budget for this 2 h window. System A's overrun is nearly
            # free, so its coal serves all 100 MWh and A pays for 60 t; B's
            # penalty is high, so B burns 40 MWh of coal and the rest is gas.
            # A merged run used to apply one budget and one penalty to both.
            H = 2
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            net2 = NetworkConfig(2, 2, buses, [1, 2], zeros(2, 2), zeros(2, 2),
                                 100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                                 TransmissionLineData[], TransformerData[],
                                 ACDCConverterData[], FrequencyConverterData[], 0.1)
            coal = mkgen("Coal", "Non-renewable", "Coal", [100.0, 100.0], [1.0e-5, 1.0e-5],
                         ones(H, 2); nb = 2, H = H)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0, 100.0], [1.0e-4, 1.0e-4],
                        ones(H, 2); nb = 2, H = H)
            budget = 40.0 * 8760 / 2
            input = PowerSystemInput(
                name = "A_B", year = 2025, network = net2, generators = [coal, gas],
                batteries = BatteryConfig[], demand = fill(50.0, H, 2),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                fuel_co2 = Dict("Coal" => 0.5),   # / η 0.5 = 1 t/MWh_e
                co2_budget = 2 * budget, co2_budget_violation_penalty = 5e-4,
                co2_budget_groups = [CO2BudgetGroup("A", [1], budget, 1e-9),
                                     CO2BudgetGroup("B", [2], budget, 5e-4)],
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            res = extract_solution(model, vars, input)
            @test sum(res.gen_output[1, 1, :]) ≈ 100.0 atol = 1e-6
            @test sum(res.gen_output[1, 2, :]) ≈ 40.0 atol = 1e-6
            @test value(vars.co2_budget_violation[1]) ≈ 60.0 atol = 1e-6
            @test value(vars.co2_budget_violation[2]) ≈ 0.0 atol = 1e-6
            @test res.cost_breakdown.co2_emission_cost ≈ 60.0 * 1e-9 rtol = 1e-6
            @test constraint_by_name(model, "CO2_budget_constraint[A]") !== nothing
            @test constraint_by_name(model, "CO2_budget_constraint[B]") !== nothing

            # No budget for B: only A is constrained.
            input_a = PowerSystemInput(
                name = "A_B", year = 2025, network = net2, generators = [coal, gas],
                batteries = BatteryConfig[], demand = fill(50.0, H, 2),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                fuel_co2 = Dict("Coal" => 0.5),
                co2_budget_groups = [CO2BudgetGroup("A", [1], budget, 5e-4),
                                     CO2BudgetGroup("B", [2], Inf, 5e-4)],
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input_a)
            optimize!(model)
            res = extract_solution(model, vars, input_a)
            @test sum(res.gen_output[1, 1, :]) ≈ 40.0 atol = 1e-6
            @test sum(res.gen_output[1, 2, :]) ≈ 100.0 atol = 1e-6
            @test collect(keys(vars.co2_budget_violation)) == [1]
            @test constraint_by_name(model, "CO2_budget_constraint[B]") === nothing
        end

        # ── Master terms behind the stochastic multipliers (#89) ──────────────
        # One representative day stands for 365. Each master term below is
        # checked against the dispatch over the same 24 h: same operation,
        # same cost per day (the master's year cost / 365).
        H24 = 24
        bus1 = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
        net1 = NetworkConfig(1, 1, [bus1], [1], zeros(1, 1), zeros(1, 1),
                             100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                             TransmissionLineData[], TransformerData[],
                             ACDCConverterData[], FrequencyConverterData[], 0.1)
        # One master over one representative day; `mult` runs it as a single
        # stochastic scenario with those multipliers.
        function master1(inp; mult = nothing)
            if mult === nothing
                model, mvars, _ = create_master_problem(inp; use_representative_days = true)
            else
                model, mvars, _, _ = create_stochastic_master_problem(
                    StochasticMasterInput(inp, [Scenario("s", 1.0, mult)], true))
            end
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            return model, mvars
        end
        mvals(model, pfx) = [value(v) for v in all_variables(model) if startswith(name(v), pfx)]

        @testset "master: CO2 budget per year, as the dispatch" begin
            # Coal (1 t/MWh_e) is cheaper than gas; the budget allows 600 t a
            # day. The master had no CO2 budget at all.
            coal = mkgen("Coal", "Non-renewable", "Coal", [100.0], [1.0e-5],
                         ones(H24, 1); nb = 1, H = H24)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4],
                        ones(H24, 1); nb = 1, H = H24)
            budget = 600.0 * 365
            function co2_master(pen; mult = nothing, use_tsam = false)
                inp = MasterProblemInput(
                    years = [2025], network = net1, generators = [coal, gas],
                    batteries = BatteryConfig[], base_demand = fill(50.0, H24, 1),
                    target_re_penetration = 0.0, initial_re_penetration = 0.0,
                    representative_days_per_year = 1, min_day_separation = 1,
                    use_tsam = use_tsam,
                    tsam_period_start_hours = use_tsam ? [[1]] : Vector{Int}[],
                    tsam_period_weights = use_tsam ? [[365.0]] : Vector{Float64}[],
                    tsam_chronological_order = use_tsam ? [[1]] : Vector{Int}[],
                    fuel_co2 = Dict("Coal" => 0.5),   # / η 0.5 = 1 t/MWh_e
                    co2_budget_groups = [CO2BudgetGroup("A", [1], budget, pen)],
                    verbose = false)
                model, mvars = master1(inp; mult = mult)
                coal_mwh = sum(value(v) for v in all_variables(model)
                               if occursin(r"^op_y1_d\d+_gen\[1,1,", name(v)))
                viol = only(v for (k, v) in mvars.co2_budget_violation)
                return model, coal_mwh, value(viol)
            end
            function co2_dispatch(pen)
                input = PowerSystemInput(
                    name = "co2", year = 2025, network = net1, generators = [coal, gas],
                    batteries = BatteryConfig[], demand = fill(50.0, H24, 1),
                    temporal = TemporalConfig(H24, 1, H24, 0, H24, H24, 1, 1, 1),
                    fuel_co2 = Dict("Coal" => 0.5),
                    co2_budget = budget, co2_budget_violation_penalty = pen,
                    mode = "economic_dispatch", solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                res = extract_solution(model, vars, input)
                return sum(res.gen_output[1, 1, :]), value(vars.co2_budget_violation[1]),
                       res.cost_breakdown.co2_emission_cost
            end
            for tsam in (false, true)
                _, coal_mwh, viol = co2_master(5e-4; use_tsam = tsam)
                @test coal_mwh ≈ 600.0 atol = 1e-6
                @test viol ≈ 0.0 atol = 1e-6
            end
            @test co2_dispatch(5e-4)[1] ≈ 600.0 atol = 1e-6
            # Overrun nearly free: coal serves all 1200 MWh a day; the master
            # pays 365 × the dispatch day's violation, at the same penalty.
            model, coal_mwh, viol = co2_master(1e-9)
            d_coal, d_viol, d_cost = co2_dispatch(1e-9)
            @test coal_mwh ≈ d_coal ≈ 1200.0
            @test viol ≈ 365 * d_viol rtol = 1e-9
            @test d_cost ≈ 600.0 * 1e-9 rtol = 1e-6
            @test coefficient(objective_function(model),
                only(v for v in all_variables(model)
                     if startswith(name(v), "co2_budget_violation"))) ≈ 1e-9
            # penalty_co2_budget_violation scales the penalty
            _, coal_mwh, _ = co2_master(5e-4; mult = ScenarioMultipliers(
                penalty_co2_budget_violation = 1e-6))
            @test coal_mwh ≈ 1200.0 atol = 1e-6
        end

        @testset "master: LP start-ups when the dispatch commits units" begin
            # A 100 MW unit (min 50 %) runs 12 h a day at full power: one
            # start a day in the dispatch's unit commitment. The master had
            # no start-up cost.
            gen = ESFEX.with_fields(
                mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-5],
                      ones(H24, 1); nb = 1, H = H24);
                min_power = [0.5], start_up_cost = [2.0e-3])
            dem = reshape(vcat(zeros(12), fill(100.0, 12)), H24, 1)
            mk(uc) = MasterProblemInput(
                years = [2025], network = net1, generators = [gen],
                batteries = BatteryConfig[], base_demand = dem,
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                unit_commitment = uc, verbose = false)
            m0, _ = master1(mk(false))
            m1, _ = master1(mk(true))
            m3, _ = master1(mk(true); mult = ScenarioMultipliers(gen_start_up_cost = 3.0))
            @test objective_value(m1) - objective_value(m0) ≈ 365 * 2.0e-3 rtol = 1e-6
            @test objective_value(m3) - objective_value(m0) ≈ 3 * 365 * 2.0e-3 rtol = 1e-6
            input = PowerSystemInput(
                name = "uc", year = 2025, network = net1, generators = [gen],
                batteries = BatteryConfig[], demand = dem,
                temporal = TemporalConfig(H24, 1, H24, 0, H24, H24, 1, 1, 1),
                mode = "unit_commitment", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test sum(value.(vars.gen_startup)) ≈ 1.0 atol = 1e-6
            res = extract_solution(model, vars, input)
            @test res.cost_breakdown.startup_cost ≈ 2.0e-3 rtol = 1e-6
        end

        @testset "master: converters operate and can be built" begin
            # Gas at bus 1, 50 MW of load at bus 2: a 10 MW line and an AC/DC
            # converter (30 MW, up to 50 MW more) join them. The master had
            # no converter: the 40 MW it carries in the dispatch were missing.
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            line = TransmissionLineData("l1", 1, 2, 10.0, 0.1, 0.0, 0.0, 100.0, 220.0,
                                        1, 50.0, "AC")
            conv(rated) = ACDCConverterData("c1", "VSC", 1, 2, 220.0, 320.0, rated, 0.0,
                1.0, 1.0, 0.0, -50.0, 50.0, 1.0, 0.05, 0.01, 1.0e-3, 0.0, 2.0e-6, 50.0,
                30, 0, 0.0)
            net(c) = NetworkConfig(2, 2, buses, [1, 2], [0 10.0; 10.0 0], zeros(2, 2),
                100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                [line], TransformerData[], c, FrequencyConverterData[], 0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0, 0.0], [1.0e-5, 0.0],
                        ones(H24, 2); nb = 2, H = H24)
            dem = hcat(zeros(H24), fill(50.0, H24))
            inp = MasterProblemInput(
                years = [2025], network = net([conv(30.0)]), generators = [gas],
                batteries = BatteryConfig[], base_demand = dem,
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                transmission_loss_segments = 0, verbose = false)
            model, mvars = master1(inp)
            res = extract_master_solution(model, mvars, inp)
            @test res.converter_investment[1][("acdc", 1)] ≈ 10.0 atol = 1e-6
            @test sum(mvals(model, "op_y1_d1_ll")) ≈ 0.0 atol = 1e-6
            @test export_solution_to_dict(res, inp)["converter_investment"]["2025"]["c1"] ≈ 10.0
            rect = mvals(model, "acdc_rectify")
            # Same flow and variable cost as the dispatch with the built 40 MW
            input = PowerSystemInput(
                name = "conv", year = 2025, network = net([conv(40.0)]), generators = [gas],
                batteries = BatteryConfig[], demand = dem,
                temporal = TemporalConfig(H24, 1, H24, 0, H24, H24, 1, 1, 1),
                pwl_loss_segments = 0,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            dmodel, dvars = create_power_system(input)
            optimize!(dmodel)
            @test value.(dvars.acdc_rectify[1, :]) ≈ rect atol = 1e-6
            # (the dispatch's converter cost term, per step)
            @test value(ESFEX.add_converter_objective_terms(dvars, input)) ≈
                  sum(rect) * 2.0e-6 rtol = 1e-6
            obj = objective_function(model)
            @test coefficient(obj, only(v for v in all_variables(model)
                                        if name(v) == "acdc_rectify[1,1]")) ≈ 365 * 2.0e-6
            @test coefficient(obj, mvars.converter_investment[1][("acdc", 1)]) ≈ 1.0e-3
            # converter_invest_cost: a dear converter is not built (load is shed)
            model, mvars = master1(inp; mult = ScenarioMultipliers(converter_invest_cost = 1.0e9))
            @test value(mvars.converter_investment[1][("acdc", 1)]) ≈ 0.0 atol = 1e-6
        end

        @testset "master: flow cost of inter-system links" begin
            # 10 MW flow every hour on a line charging 3e-6 per MWh: the plan
            # costs 10 × 24 × 365 × 3e-6 more; the multiplier doubles it.
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            line = TransmissionLineData("l1", 1, 2, 10.0, 0.1, 0.0, 0.0, 100.0, 220.0,
                                        1, 50.0, "AC")
            net2 = NetworkConfig(2, 2, buses, [1, 2], [0 10.0; 10.0 0], zeros(2, 2),
                100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                [line], TransformerData[], ACDCConverterData[], FrequencyConverterData[], 0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0, 100.0], [1.0e-5, 1.0e-3],
                        ones(H24, 2); nb = 2, H = H24)
            mk(cost) = MasterProblemInput(
                years = [2025], network = net2, generators = [gas],
                batteries = BatteryConfig[], base_demand = fill(50.0, H24, 2),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                transmission_loss_segments = 0, line_flow_cost = cost, verbose = false)
            m0, _ = master1(mk(Dict{Tuple{Int,Int},Float64}()))
            m1, _ = master1(mk(Dict((1, 2) => 3.0e-6)))
            m2, _ = master1(mk(Dict((1, 2) => 3.0e-6));
                            mult = ScenarioMultipliers(transmission_cost_per_mw_km = 2.0))
            @test objective_value(m1) - objective_value(m0) ≈ 10 * 24 * 365 * 3.0e-6 rtol = 1e-6
            @test objective_value(m2) - objective_value(m0) ≈ 2 * 10 * 24 * 365 * 3.0e-6 rtol = 1e-6
        end

        @testset "scenario_master_input: start-up, battery maintenance, converter, link, CO2 multipliers" begin
            gen = ESFEX.with_fields(
                mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-5],
                      ones(H24, 1); nb = 1, H = H24);
                start_up_cost = [2.0], maintenance_cost = [1.0])
            bat = BatteryConfig("Li1", [40.0], [20.0], [20.0], [0.95], [0.95],
                                [0.0], [1.0], [0.5], [0.0], [0.0], [0.0], [0.0],
                                [0.0], [10.0], [0.0], [0.0], 0.0, 100.0, [2.0],
                                [0.0], false, "AC", [0.0], [0.0], [0.0])
            o = [0.0]
            li = BatteryTechnologyConfig("LiNew", [1.0], [1.0], [10.0], [40.0], 1.0, 8.0,
                                         [0.95], [0.95], o, [15.0], [0.5], [0.0], [1.0],
                                         [3.0], o, [4.0], false, "AC", o, [1.0])
            c = ACDCConverterData("c1", "VSC", 1, 1, 220.0, 320.0, 10.0, 0.0, 1.0, 1.0,
                                  0.0, -50.0, 50.0, 1.0, 0.05, 0.01, 1.0, 0.0, 5.0, 0.0, 30, 0, 0.0)
            net = ESFEX.with_fields(net1; acdc_converters = [c])
            inp = MasterProblemInput(
                years = [2025], network = net, generators = [gen], batteries = [bat],
                battery_technologies = [li], base_demand = fill(50.0, H24, 1),
                co2_budget_groups = [CO2BudgetGroup("A", [1], 10.0, 7.0)],
                line_flow_cost = Dict((1, 2) => 4.0), verbose = false)
            si = ESFEX.scenario_master_input(inp, ScenarioMultipliers(
                maintenance_cost = 2.0, bat_maintenance_cost = 3.0, gen_start_up_cost = 5.0,
                converter_variable_cost = 6.0, transmission_cost_per_mw_km = 7.0,
                penalty_co2_budget_violation = 8.0), 1)
            @test si.generators[1].maintenance_cost == [2.0]
            @test si.generators[1].start_up_cost == [10.0]
            @test si.batteries[1].maintenance_cost == [12.0]
            @test si.battery_technologies[1].maintenance_cost == [18.0]
            @test si.network.acdc_converters[1].variable_cost == 30.0
            @test si.line_flow_cost[(1, 2)] == 28.0
            @test si.co2_budget_groups[1].violation_penalty == 56.0
            @test si.co2_budget_groups[1].budget == 10.0
        end

        @testset "solve_dcopf benchmark" begin
            # Cheap gen at bus 1 supplies a bus-2 load across one line.
            r = solve_dcopf(num_buses = 2, demand = [0.0, 100.0],
                            gen_bus = [1, 2], gen_cost = [10.0, 50.0],
                            gen_max = [200.0, 200.0], line_from = [1],
                            line_to = [2], line_x = [0.1], line_cap = [150.0],
                            slack_bus = 1, s_base = 100.0)
            @test r["status"] == "OPTIMAL"
            @test r["total_cost"] ≈ 1000.0           # 100 MW * $10
            @test r["line_flows_mw"][1] ≈ 100.0      # all load flows over the line
            @test r["gen_dispatch_list"][1] ≈ 100.0  # cheap unit covers it
            @test r["gen_dispatch_list"][2] ≈ 0.0 atol = 1e-6
        end

        @testset "solve_acopf benchmark" begin
            r = solve_acopf(num_buses = 2, demand_p = [0.0, 80.0],
                            demand_q = [0.0, 20.0], shunt_g = [0.0, 0.0],
                            shunt_b = [0.0, 0.0], gen_bus = [1],
                            gen_cost = [20.0], gen_pmax = [200.0],
                            gen_pmin = [0.0], gen_qmax = [100.0],
                            gen_qmin = [-100.0], line_from = [1], line_to = [2],
                            line_r = [0.01], line_x = [0.1], line_b = [0.0],
                            line_cap = [200.0], slack_bus = 1, base_mva = 100.0)
            @test r["status"] in ("LOCALLY_SOLVED", "OPTIMAL")
            @test r["total_cost"] > 0
            @test length(r["vm_pu"]) == 2
            @test all(0.85 .≤ r["vm_pu"] .≤ 1.15)    # within voltage bounds
            @test r["gen_dispatch_list"][1] > 0       # slack gen supplies load
        end
    end

end
