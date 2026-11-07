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
             coefficient, objective_function, all_variables, name, fix
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

        @testset "Dead multi-system master removed" begin
            # Python merges the systems into one MasterProblemInput; the
            # separate multi-system master was never built and is gone.
            for name in (:MultiSystemMasterInput, :InterSystemLink, :SystemConfig,
                         :ExtendedMasterVariables, :ExtendedMasterResult,
                         :create_multi_system_master_problem,
                         :add_inter_system_constraints!, :build_multi_system_objective!)
                @test !isdefined(ESFEX, name)
            end
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

        @testset "committed steps of a rolling window" begin
            # A window keeps its first `keep` steps: the totals, the cost
            # breakdown and the objective cover those, not keep/H of the window.
            H, keep = 4, 2
            gen = mkgen("GasCC", "Non-renewable", "Gas", [100.0],
                        [3.0e-5], ones(H, 1); nb = 1, H = H)
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            input = PowerSystemInput(
                name = "ed", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = reshape([20.0, 40.0, 60.0, 80.0], H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            full = extract_solution(model, vars, input)
            cs = ESFEX.committed_steps(vars, H, keep)
            part = extract_solution(model, vars, input; committed = cs)
            @test part.total_demand ≈ 60.0
            @test part.total_generation ≈ 60.0 atol = 1e-6
            @test part.cost_breakdown.fuel_cost ≈ 3.0e-5 * 60.0 rtol = 1e-6
            @test part.objective ≈ part.cost_breakdown.total
            @test part.objective < full.objective * keep / H   # growing load
            @test size(part.gen_output) == (1, 1, H)           # arrays: whole window
            all_kept = ESFEX.committed_steps(vars, H, H)
            @test extract_solution(model, vars, input; committed = all_kept).objective ≈
                  full.objective rtol = 1e-12
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
            # unclassified remainder, priced at the sectors' demand-weighted
            # mean multiplier (it was VOLL × 1, cheaper than every sector);
            # the model stays feasible.
            lol, res = shed_split(Dict("cheap" => 20.0, "critical" => 30.0))
            m = (20.0 * 1.0 + 30.0 * 1000.0) / 50.0
            @test res.load_shed_total ≈ 60.0 atol = 1e-6
            @test lol["cheap"] ≈ 20.0 atol = 1e-6       # cheapest first
            @test lol["critical"] ≈ 0.0 atol = 1e-6     # remainder (40 of 50) before it
            @test res.cost_breakdown.load_shedding_cost ≈
                  0.01 * (20.0 * 1.0 + 40.0 * m) rtol = 1e-6
            lol, res = shed_split(Dict("critical" => 50.0))
            @test res.load_shed_total ≈ 60.0 atol = 1e-6
            @test res.cost_breakdown.load_shedding_cost ≈ 0.01 * 60.0 * 1000.0 rtol = 1e-6
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

            # Unclassified remainder (40 of 100 MW) at the sectors' weighted
            # mean (≈ 683), as in the dispatch: sectors A (κ 2) and C (κ 100)
            # are shed before it. At plain VOLL it went first.
            gas70 = mkgen("Gas", "Non-renewable", "Gas", [70.0],
                          [1.0], ones(H, 1); nb = 1, H = H)
            inp = MasterProblemInput(
                years = [2025], network = net, generators = [gas70],
                batteries = BatteryConfig[], base_demand = fill(100.0, H, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                loss_of_load_penalty = 10.0,
                sectoral_demand = Dict("A" => fill(10.0, H, 1), "C" => fill(10.0, H, 1),
                                       "B" => fill(40.0, H, 1)),
                sectoral_criticality = Dict("A" => 2.0, "C" => 100.0, "B" => 1000.0),
                verbose = false)
            model, _, _ = create_master_problem(inp; use_representative_days = true)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            byname(pfx) = [v for v in all_variables(model) if startswith(name(v), pfx)]
            @test sum(value.(byname("op_y1_d1_ll["))) ≈ 24 * 30.0 rtol = 1e-6
            @test sum(value.(byname("op_y1_d1_lol_A["))) ≈ 24 * 10.0 rtol = 1e-6
            @test sum(value.(byname("op_y1_d1_lol_C["))) ≈ 24 * 10.0 rtol = 1e-6
            @test sum(value.(byname("op_y1_d1_lol_B["))) ≈ 0.0 atol = 1e-6

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

        @testset "line flow cost is charged on |flow|" begin
            # Inter-system links carry distance × cost_per_mw_km per MWh; the
            # dispatch charged nothing. A cheap unit at the far end serves the
            # load over the line while the flow cost stays below the price gap.
            function flow_run(cost; cheap_bus = 1)
                H = 2; N = 2
                fc = cheap_bus == 1 ? [1.0e-5, 1.0e-4] : [1.0e-4, 1.0e-5]
                gas = mkgen("Gas", "Non-renewable", "Gas", [200.0, 200.0], fc,
                            ones(H, N); nb = N, H = H)
                buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                         BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
                line = TransmissionLineData("link_A_B_0", 1, 2, 150.0, 0.1, 0.0, 0.0,
                                            100.0, 220.0, 1, 50.0, "AC", cost)
                net = NetworkConfig(N, N, buses, [1, 2],
                                    [0.0 150.0; 150.0 0.0], [0.0 100.0; 100.0 0.0],
                                    100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                                    [line], TransformerData[], ACDCConverterData[],
                                    FrequencyConverterData[], 0.1)
                demand = cheap_bus == 1 ? [0.0 100.0; 0.0 100.0] : [100.0 0.0; 100.0 0.0]
                input = PowerSystemInput(
                    name = "flowcost", year = 2025, network = net, generators = [gas],
                    batteries = BatteryConfig[], demand = demand,
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    mode = "economic_dispatch", solver_name = "highs", verbose = false,
                    pwl_loss_segments = 0)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                res = extract_solution(model, vars, input)
                imported = sum(value(vars.gen_output[1, cheap_bus, t]) for t in 1:H)
                return imported, res.cost_breakdown.line_flow_cost
            end
            imported, cost = flow_run(5.0e-5)
            @test imported ≈ 200.0 atol = 1e-6
            @test cost ≈ 200.0 * 5.0e-5 rtol = 1e-6
            imported, cost = flow_run(5.0e-5; cheap_bus = 2)    # reverse direction
            @test imported ≈ 200.0 atol = 1e-6
            @test cost ≈ 200.0 * 5.0e-5 rtol = 1e-6
            imported, cost = flow_run(2.0e-4)                   # dearer than the gap
            @test imported ≈ 0.0 atol = 1e-6
            @test cost ≈ 0.0 atol = 1e-9
            # Default constructor: no flow cost
            @test TransmissionLineData("L", 1, 2, 1.0, 0.1, 0.0, 0.0, 1.0, 220.0,
                                       1, 50.0, "AC").flow_cost == 0.0
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

            # Scheduled outage (was accepted but ignored): out in step 2, half in step 3
            input = PowerSystemInput(
                name = "elz", year = 2025, network = net, generators = [gen],
                batteries = BatteryConfig[], demand = reshape([10.0, 5.0, 5.0], H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                electrolyzer_config = elz,
                outage_masks = Dict{String, Any}("electrolyzer" => [1.0 0.0 0.5]),
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test value.(vars.electrolyzer_power[1, 1, :]) ≈ [2.0, 0.0, 2.0] atol = 1e-6
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

            # n1_security_cost = objective with N-1 − objective without it
            # (was always 0). Corrective N-1 moves 35 MW from A to the dearer
            # B in both steps; a short backup only adds the shortfall penalty.
            function n1_security(gens; kw...)
                input = PowerSystemInput(;
                    name = "n1", year = 2025, network = net, generators = gens,
                    batteries = BatteryConfig[], demand = fill(50.0, H, 1),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    reserve_static_default_ratio = 0.0, loss_of_reserve_static = 1.0,
                    loss_of_load_penalty = 100.0,
                    n1_security_enabled = true, n1_generation_enabled = true,
                    mode = "economic_dispatch", solver_name = "highs", verbose = false,
                    kw...)
                model, vars = create_power_system(input)
                optimize!(model)
                before = extract_solution(model, vars, input).n1_security_cost
                c = compute_n1_security_cost!(model, input)
                res = extract_solution(model, vars, input)
                return before, c, res.n1_security_cost, value(vars.gen_output[1, 1, 1])
            end
            before, c, stored, a_out = n1_security(
                [ngen("A", 100.0, 1.0e-5), ngen("B", 60.0, 1.0e-4; ramp = 0.25)];
                n1_scopf_enabled = true, n1_corrective_enabled = true)
            @test before == 0.0                       # not computed: stays 0
            @test c ≈ 35.0 * (1.0e-4 - 1.0e-5) * H rtol = 1e-6
            @test stored ≈ c
            @test a_out ≈ 15.0 atol = 1e-6            # original solution untouched
            _, c, _, _ = n1_security([ngen("A", 100.0, 1.0e-5), ngen("B", 30.0, 1.0e-4)])
            @test c ≈ 20.0 * H rtol = 1e-6
            # N-1 off: nothing to compute
            _, c, stored, _ = n1_security([ngen("A", 100.0, 1.0e-5)];
                                          n1_security_enabled = false)
            @test c == 0.0 && stored == 0.0
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
                @test !hasfield(ESFEX.CostBreakdown, :flexible_demand_benefit)
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
                1.0, "economic_dispatch", Dict{String, Any}(), nothing, false,
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
                    1000.0, "economic_dispatch", Dict{String, Any}(), nothing, false,
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

        @testset "primary energy: scheduled tank and route outages, no-tank nodes" begin
            # One day per period, 48 hourly steps, a 10/day (or 30/day)
            # non-electric demand. Returns the fuel shortfall of the window.
            function pe_short(; cap = 0.0, init = 0.0, avail = 87600.0, ne = 3650.0,
                              disrupt = (0, 0), masks = Dict{String, Any}(),
                              route_cap = 0.0)
                two = route_cap > 0
                n = two ? 2 : 1
                fuel = FuelConfig("Gas", 1.0, 0.0, 10.0, 0.0,
                                  two ? [avail, 0.0] : [avail], two ? [0.0, cap] : [cap],
                                  two ? [0.0, init] : [init], 0.0, zeros(n), 0.0, 0.0, 0.0,
                                  disrupt[1], disrupt[2], 0.0)
                routes = two ? [TransportRoute("r1", 1, 2, 100.0,
                                Dict("Gas" => FuelRouteParams(route_cap, 0.0, 0.0)))] :
                               TransportRoute[]
                ned = NonElectricDemandConfig("Industrial", "Gas",
                                              two ? [0.0, ne] : [ne], 0.0, ones(12))
                inp = PrimaryEnergyInput(2025, 2025, n, 48, [fuel],
                    Dict{String, FuelInfrastructureConfig}(), [ned], routes,
                    Dict{Int, Tuple{String, Float64, Float64, Float64}}(), 24, 48, 0.05,
                    1000.0, "economic_dispatch", Dict{String, Any}(), nothing, false,
                    nothing, Dict{Int, Vector{Float64}}(), nothing, 300.0, 1, 0)
                m = ESFEX.Model(create_optimizer(solver_name = "highs"))
                ESFEX.set_silent(m)
                vars, temporal, prices = ESFEX.create_primary_energy_model(m, inp;
                                                                           outage_masks = masks)
                terms = ESFEX.get_primary_energy_objective_terms(vars, inp, temporal, prices)
                ESFEX.@objective(m, Min, sum(values(terms)))
                optimize!(m)
                @test string(termination_status(m)) == "OPTIMAL"
                short = sum(value.(vars.ne_demand_shortfall_periodic[("Gas", "Industrial")])) +
                        sum(value.(vars.fuel_loss_of_supply_hourly["Gas"]))
                return short, value.(vars.fuel_storage_level_hourly["Gas"])
            end

            # #121: supply is cut on day 2. A node without a tank could keep
            # day 1's fuel for day 2 (its level had no upper bound); now only
            # a real tank carries fuel over.
            short, level = pe_short(disrupt = (24, 48))
            @test short ≈ 10.0 atol = 1e-6
            @test maximum(level) ≈ 0.0 atol = 1e-9
            short, _ = pe_short(cap = 100.0, disrupt = (24, 48))
            @test short ≈ 0.0 atol = 1e-6

            # Tank outage (fuel_storage): a full, 100-unit tank and no supply
            # serving 30/day for 2 days. Out of service, its level is frozen.
            tank(a) = Dict{String, Any}("fuel_storage" =>
                Dict("Gas" => fill(a, 1, 48)))
            short, _ = pe_short(cap = 100.0, init = 1.0, avail = 0.0, ne = 3.0 * 3650.0)
            @test short ≈ 0.0 atol = 1e-6
            short, level = pe_short(cap = 100.0, init = 1.0, avail = 0.0, ne = 3.0 * 3650.0,
                                    masks = tank(0.0))
            @test short ≈ 60.0 atol = 1e-6
            @test all(abs.(level .- 100.0) .< 1e-6)
            # Half out of service: 50 locked, 50 usable
            short, _ = pe_short(cap = 100.0, init = 1.0, avail = 0.0, ne = 3.0 * 3650.0,
                                masks = tank(0.5))
            @test short ≈ 10.0 atol = 1e-6

            # Route outage (fuel_route): node 1 ships up to 15/day to node 2's
            # 10/day demand (12 usable after the 0.8 inflow efficiency). At
            # half capacity on day 1 only 7.5 × 0.8 = 6 arrive.
            short, _ = pe_short(route_cap = 15.0)
            @test short ≈ 0.0 atol = 1e-6
            route = Dict{String, Any}("fuel_route" =>
                Dict("r1" => vcat(fill(0.5, 24), ones(24))))
            short, _ = pe_short(route_cap = 15.0, masks = route)
            @test short ≈ 4.0 atol = 1e-6
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
                    1000.0, "economic_dispatch", Dict{String, Any}(),
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
                1.0, "economic_dispatch", Dict{String, Any}(), nothing, false,
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
                1.0, "economic_dispatch", Dict{String, Any}(), nothing, false,
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
            # pe_electrolyzer_cost reports the electrolyzer cost charged (was 0)
            cb = extract_solution(model, vars, input).cost_breakdown
            @test cb.electrolyzer_cost > 0
            @test cb.pe_electrolyzer_cost ≈ cb.electrolyzer_cost rtol = 1e-9
            # The old fixed-production inputs are rejected, not ignored
            bad = PrimaryEnergyInput(2025, 2025, 1, H, [h2],
                Dict{String, FuelInfrastructureConfig}(), NonElectricDemandConfig[],
                TransportRoute[], Dict{Int, Tuple{String, Float64, Float64, Float64}}(),
                24, 8760, 0.05, 1.0, "economic_dispatch", Dict{String, Any}(),
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
                    1000.0, "economic_dispatch", Dict{String, Any}(), nothing, false,
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

        @testset "primary energy: supply limit per period counts step hours" begin
            # 8760 units a year of supply and of demand: 24 per 24 h period at
            # any step length. The supply limit counted steps, not hours, so
            # 2 h steps got 12 and 3 h steps 8.
            for tres in (1, 2, 3)
                H = 24 ÷ tres
                gasf = FuelConfig("Gas", 1.0, 0.0, 10.0, 0.0, [8760.0], [0.0], [0.0], 0.0,
                                  [0.0], 0.0, 0.0, 0.0)
                ne = NonElectricDemandConfig("Industrial", "Gas", [8760.0], 0.0, ones(12))
                pe = PrimaryEnergyInput(2025, 2025, 1, H, [gasf],
                    Dict{String, FuelInfrastructureConfig}(), [ne], TransportRoute[],
                    Dict{Int, Tuple{String, Float64, Float64, Float64}}(), H, H, 0.05, 1.0,
                    "economic_dispatch", Dict{String, Any}(), nothing, false, nothing,
                    Dict{Int, Vector{Float64}}(), nothing, 1.0, tres, 0)
                tm = ESFEX.create_temporal_mapping(H, H, H)
                _, ms, ned = ESFEX.prepare_fuel_data(pe, tm)
                @test ms["Gas"][1][1] ≈ 24.0 rtol = 1e-12
                @test ned[("Gas", "Industrial")][1][1] ≈ 24.0 rtol = 1e-12
            end
        end

        # ── M2 (#89): fuel block of the master ──────────────────────────────
        # Gas at 3e-5 per unit (+ 0.5e-5 import), 5 MWh_e per unit; an
        # existing 30 MW electrolyzer (variable 2e-5 per MWh, water 1e-7 per
        # kg, 13 kg/MWh) must make the 0.01 t/h of hydrogen demand, since a
        # fuel shortfall costs 1.0 per unit.
        function fuel_case(tres; elz_rated = 30.0, elz_max = 0.0)
            H = 24 ÷ tres
            gas = mkgen("Gas", "Non-renewable", "Gas", [200.0], [5.0e-5], ones(H, 1);
                        nb = 1, H = H)
            elz = ElectrolyzerConfig([elz_rated], [0.65], [0.65], 50.0, [0.0], [0.0],
                                     [1.0e-3], [elz_max], [1.0e-6], [2.0e-5], 1.0e-7, [20.0])
            gasf = FuelConfig("Gas", 3.0e-5, 0.0, 10.0, 0.0, [1.0e6], [0.0], [0.0],
                              0.0, [0.5e-5], 0.0, 0.0, 0.0)
            h2 = FuelConfig("Hydrogen", 0.0, 0.0, 33.3, 0.0, [0.0], [0.0], [0.0], 0.0,
                            [0.0], 0.0, 0.0, 0.0)
            ne_gas = NonElectricDemandConfig("Industrial", "Gas", [8760.0], 0.0, ones(12))
            ne_h2 = NonElectricDemandConfig("Industrial", "Hydrogen", [87.6], 0.0, ones(12))
            pe = PrimaryEnergyInput(2025, 2025, 1, H, [gasf, h2],
                Dict{String, FuelInfrastructureConfig}(), [ne_gas, ne_h2],
                TransportRoute[], Dict(1 => ("Gas", 5.0, 10.0, 0.5)), H, H, 0.05, 1.0, "economic_dispatch", Dict{String, Any}(), nothing, false, nothing,
                Dict{Int, Vector{Float64}}(), nothing, 1.0, tres, 0)
            return H, gas, elz, pe
        end
        single_bus() = NetworkConfig(1, 1, [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)],
            [1], zeros(1, 1), zeros(1, 1), 100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
            TransmissionLineData[], TransformerData[], ACDCConverterData[],
            FrequencyConverterData[], 0.1)

        @testset "master fuel block charges what the dispatch charges" begin
            # One representative day of the master and a 24 h dispatch window
            # of the same system: the same fuel bought, the same penalties,
            # the same electrolyzer cost (water included), at 1 h and 2 h steps.
            # The master used to charge the unit its flat fuel_cost and had no
            # electrolyzer; the dispatch had no water cost.
            for tres in (1, 2)
                H, gas, elz, pe = fuel_case(tres)
                net = single_bus()
                inp = MasterProblemInput(years = [2025], network = net, generators = [gas],
                    batteries = BatteryConfig[], base_demand = fill(50.0, H, 1),
                    target_re_penetration = 0.0, initial_re_penetration = 0.0,
                    representative_days_per_year = 1, min_day_separation = 1,
                    temporal_resolution_hours = tres, verbose = false,
                    primary_energy = MasterFuelInput(pe, [elz]))
                model, mv, _ = create_master_problem(inp; use_representative_days = false)
                ps = create_day_ps_vars!(model, inp, 1, 1, H)
                tech_out, _ = add_day_operational_constraints!(model, ps, mv, inp, 1, 1,
                    inp.base_demand[1:H, :], 1)
                fc = add_master_fuel_day!(model, ps, inp, 1, 1, H, tech_out; extra = k -> 0.0)
                _, mterms = model.ext[:master_fuel_last_day]
                ESFEX.@objective(model, Min,
                    tres * ESFEX.day_operational_cost(ps, inp, H; tech_output = tech_out) + fc)
                ESFEX.set_silent(model)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"

                input = PowerSystemInput(name = "d", year = 2025, network = net,
                    generators = [gas], batteries = BatteryConfig[],
                    demand = fill(50.0, H, 1),
                    temporal = TemporalConfig(H, tres, H, 0, H, H, 1, 1, 1),
                    electrolyzers = [elz], mode = "economic_dispatch",
                    solver_name = "highs", verbose = false)
                dmodel, dv = create_power_system(input)
                pvd, temporal, prices = ESFEX.create_primary_energy_model(dmodel, pe)
                ESFEX.couple_primary_energy_to_power_system!(dmodel, pvd, dv, pe;
                    generators = input.generators, electrolyzers = input.electrolyzers,
                    resolution_hours = Float64(tres))
                dterms = ESFEX.get_primary_energy_objective_terms(pvd, pe, temporal, prices)
                ESFEX.@objective(dmodel, Min, objective_function(dmodel) + sum(values(dterms)))
                ESFEX.set_silent(dmodel)
                optimize!(dmodel)
                @test string(termination_status(dmodel)) == "OPTIMAL"

                for k in (:pe_supply_cost, :pe_loss_cost, :pe_excess_cost, :pe_transport_cost)
                    @test value(mterms[k]) ≈ value(dterms[k]) atol = 1e-9
                end
                @test value(mterms[:pe_supply_cost]) > 0
                Pm = sum(value.(ps.electrolyzer_power))
                Pd = sum(value.(dv.electrolyzer_power))
                @test Pm ≈ Pd rtol = 1e-6
                @test Pm * tres * 13.0 / 1000 * 0.8 ≈ 0.01 * 24 rtol = 1e-6
                elz_m = tres * (2.0e-5 + 1.0e-7 * 13.0) * Pm
                # The dispatch's bucket also holds the fixed cost of the 30 MW
                # for the window's 24 h; the master charges it per year
                fixed_day = 30.0 * 1.0e-6 * 24
                @test elz_m + fixed_day ≈
                      tres * value(dmodel.ext[:cost_expressions][:electrolyzer_cost]) rtol = 1e-6
                @test value(ESFEX.master_fuel_annual_cost(mv, inp, 1)) ≈ fixed_day * 365 rtol = 1e-9
                # The day's fuel cost replaces the unit's flat fuel_cost
                gm = sum(value(ps.gen_output[1, 1, t]) for t in 1:H)
                @test value(fc) ≈ sum(value(v) for v in values(mterms)) + elz_m -
                                  tres * 5.0e-5 * gm rtol = 1e-9
                @test value(dmodel.ext[:cost_expressions][:fuel_cost]) ≈ 0.0 atol = 1e-12
            end
        end

        # Node 1: the power system, a gas tank of 10 (may grow by 20) drawn at
        # most 0.5 of its capacity per hour. Node 2: gas with no electricity,
        # a 100 km route to node 1 of 100 units/day that may grow 3-fold.
        # Hydrogen demand at node 1, and an electrolyzer that must be built.
        # Two years of one 24 h day each.
        function fuel_invest_input(; transit = 0.0)
            H = 24
            gas = mkgen("Gas", "Non-renewable", "Gas", [200.0], [5.0e-5], ones(H, 1);
                        nb = 1, H = H)
            elz = ElectrolyzerConfig([0.0], [0.65], [0.65], 50.0, [0.0], [0.0],
                                     [1.0e-3], [50.0], [1.0e-6], [2.0e-5], 1.0e-7, [20.0])
            gasf = FuelConfig("Gas", 3.0e-5, 0.0, 10.0, 0.0, [0.0, 1.0e6], [10.0, 0.0],
                              [1.0, 0.0], 0.0, [5.0e-4, 1.0e-5], 0.0, 0.0, transit)
            h2 = FuelConfig("Hydrogen", 0.0, 0.0, 33.3, 0.0, [0.0, 0.0], [0.0, 0.0],
                            [0.0, 0.0], 0.0, [0.0, 0.0], 0.0, 0.0, 0.0)
            infra = Dict("Gas" => FuelInfrastructureConfig(100.0, 1.0e-6, 3.0, 1.0e-4, 2.0,
                                                          1.0, 20.0, 30.0, 0.5))
            route = TransportRoute("r21", 2, 1, 100.0,
                Dict("Gas" => FuelRouteParams(100.0, 1.0e-7, 0.0)))
            ne_h2 = NonElectricDemandConfig("Industrial", "Hydrogen", [87.6, 0.0], 0.0, ones(12))
            pe = PrimaryEnergyInput(2025, 2025, 2, H, [gasf, h2], infra, [ne_h2], [route],
                Dict(1 => ("Gas", 5.0, 10.0, 0.5)), H, H, 0.05, 1.0, "economic_dispatch", Dict{String, Any}(), nothing, false, nothing,
                Dict{Int, Vector{Float64}}(), nothing, 1.0, 1, 0)
            return MasterProblemInput(years = [2025, 2026], network = single_bus(),
                generators = [gas], batteries = BatteryConfig[],
                base_demand = fill(50.0, 2H, 1), target_re_penetration = 0.0,
                initial_re_penetration = 0.0, representative_days_per_year = 1,
                min_day_separation = 1, hours_per_year = [24, 24], slack_penalty = 0.0,
                verbose = false, primary_energy = MasterFuelInput(pe, [elz]))
        end

        @testset "master builds fuel storage, transport and electrolyzers" begin
            inp = fuel_invest_input()
            model, mv, _ = create_master_problem(inp; use_representative_days = true)
            ESFEX.set_silent(model)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            fi = extract_master_solution(model, mv, inp).fuel_investment
            # 50 MW + the electrolyzer burn gas shipped from node 2: the
            # route grows from 100 units/day to what is burnt
            elz_mw = 0.01 / (13.0 / 1000 * 0.8)          # hydrogen demand / output
            gas_day = (50.0 + elz_mw) * 24 / 5.0
            @test fi["electrolyzer"][1][1][1] ≈ elz_mw rtol = 1e-6
            @test fi["transport"][1]["Gas"][1] ≈ gas_day - 100.0 rtol = 1e-6
            # Tank draw ≤ 0.5 × capacity per hour: the hourly burn needs 2×
            @test fi["storage"][1]["Gas"][1] ≈ 2 * gas_day / 24 - 10.0 rtol = 1e-6
            # Built once, in service both years
            @test fi["storage_capacity"][2]["Gas"][1] ≈ fi["storage"][1]["Gas"][1]
            @test fi["electrolyzer_capacity"][2][1][1] ≈ elz_mw rtol = 1e-6

            # Benders reaches the same plan (the cuts carry the fuel capacities)
            br = run_benders_decomposition(inp; max_iterations = 60, tolerance = 1e-6,
                                           verbose_benders = false)
            @test br.objective ≈ objective_value(model) rtol = 1e-6
            bf = br.solution.fuel_investment
            @test bf["storage_capacity"][1]["Gas"][1] ≈ fi["storage_capacity"][1]["Gas"][1] rtol = 1e-4
            @test bf["transport_capacity"][1]["Gas"][1] ≈ fi["transport_capacity"][1]["Gas"][1] rtol = 1e-4
            @test bf["electrolyzer_capacity"][1][1][1] ≈ elz_mw rtol = 1e-4
            # The fuel builds count in the year's investment total (they
            # were left out); no other build in this case
            res = extract_master_solution(model, mv, inp)
            @test res.total_investment_by_year[1] > 0
            @test sum(res.total_investment_by_year) ≈
                  value(ESFEX.master_fuel_investment_cost(mv, inp; discount_rate = 0.0)) rtol = 1e-9

            # A route transit delay changes nothing in the cyclic day: the
            # shipments of the day's last steps used to arrive after its end
            # (paid for, never delivered), so the master bought a fuel
            # shortfall or over-sized the route.
            for transit in (0.25, 1.0)
                tinp = fuel_invest_input(; transit = transit)
                tm, tv, _ = create_master_problem(tinp; use_representative_days = true)
                ESFEX.set_silent(tm)
                optimize!(tm)
                @test string(termination_status(tm)) == "OPTIMAL"
                @test objective_value(tm) ≈ objective_value(model) rtol = 1e-6
                tfi = extract_master_solution(tm, tv, tinp).fuel_investment
                @test tfi["transport"][1]["Gas"][1] ≈ fi["transport"][1]["Gas"][1] rtol = 1e-6
                pv, _ = tm.ext[:master_fuel_last_day]
                @test sum(value.(pv.fuel_loss_of_supply_hourly["Gas"])) ≈ 0.0 atol = 1e-6
            end
        end

        @testset "stochastic fuel and electrolyzer multipliers" begin
            function solve_with(m::ScenarioMultipliers)
                inp = fuel_invest_input()
                sin = StochasticMasterInput(inp, [Scenario("s", 1.0, m)], true)
                model, mv, _, _ = ESFEX.create_stochastic_master_problem(sin)
                ESFEX.set_silent(model)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                return objective_value(model), extract_master_solution(model, mv, inp).fuel_investment
            end
            obj0, fi0 = solve_with(ScenarioMultipliers())
            @test fi0["storage"][1]["Gas"][1] > 1.0
            # Dearer builds are not made (a shortfall costs less)
            _, fi = solve_with(ScenarioMultipliers(fuel_storage_invest_cost = 1.0e5,
                fuel_transport_cost = 1.0e5, electrolyzer_invest_cost = 1.0e5))
            @test fi["storage"][1]["Gas"][1] ≈ 0.0 atol = 1e-6
            @test fi["transport"][1]["Gas"][1] ≈ 0.0 atol = 1e-6
            @test fi["electrolyzer"][1][1][1] ≈ 0.0 atol = 1e-6
            # Operating multipliers raise the cost of the same plan
            for kw in ((fuel_cost = 2.0,), (fuel_import_cost = 2.0,), (fuel_price_growth = 2.0,),
                       (electrolyzer_variable_cost = 2.0,), (electrolyzer_water_cost = 2.0,),
                       (electrolyzer_fixed_cost = 2.0,))
                obj, _ = solve_with(ScenarioMultipliers(; kw...))
                @test obj > obj0 + 1e-9
            end
            # A cheaper fuel shortfall replaces the route expansion
            _, fi = solve_with(ScenarioMultipliers(penalty_loss_of_fuel_supply = 1.0e-6))
            @test fi["transport"][1]["Gas"][1] ≈ 0.0 atol = 1e-6
        end

        @testset "the dispatch builds no fuel infrastructure" begin
            # Fuel storage and routes used to be built in every window that
            # invested; now only the master builds them.
            _, _, _, pe = fuel_case(1)
            infra = Dict("Gas" => FuelInfrastructureConfig(100.0, 1.0e-9, 3.0, 1.0e-9, 2.0,
                                                          1.0, 20.0, 30.0, 0.5))
            pe2 = ESFEX.with_fields(pe; infrastructure = infra)
            m = ESFEX.Model(create_optimizer(solver_name = "highs"))
            vars, _, _ = ESFEX.create_primary_energy_model(m, pe2)
            @test isempty(vars.storage_capacity_investment)
            @test isempty(vars.transport_capacity_investment)
        end
        # ── end M2 ────────────────────────────────────────────────────────────

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

        @testset "master prices candidate technologies with their offer curve" begin
            # 100 MW of load, 100 MW of diesel at 5e-5 and up to 100 MW of a
            # cheap-to-build gas technology. Its flat fuel_cost is 1e-5, but
            # its offer curve prices the upper half at 1e-4, above diesel, so
            # the dispatch runs only the lower half. The master built the
            # curve and then charged the flat 1e-5 on every MWh.
            H = 24
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            diesel = mkgen("Diesel", "Non-renewable", "Diesel", [100.0], [5.0e-5],
                           ones(H, 1); nb = 1, H = H)
            o = [0.0]
            gas = TechnologyConfig("GasNew", "Non-renewable", "Gas", [1.0e-9], [100.0],
                                   ones(H, 1), [1.0], [1.0], [1.0], [1.0], o, o, o,
                                   [1.0e-5], o, o, o, o, [25.0], o, o, 50.0, "AC",
                                   false, [1.0])
            curve = Dict(1 => Dict(1 => [CostSegment(0.5, 1.0e-5),
                                         CostSegment(0.5, 1.0e-4)]))
            mkinput(curves) = MasterProblemInput(
                years = [2025], network = net, generators = [diesel],
                batteries = BatteryConfig[], technologies = [gas],
                base_demand = fill(100.0, H, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                max_curtailment_ratio = 1.0, tech_cost_curves = curves,
                verbose = false)
            function tech_energy(inp)
                model, _, _ = create_master_problem(inp; use_representative_days = true)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                return sum(value(v) for v in all_variables(model)
                           if startswith(name(v), "tech_out_y1_d1["))
            end
            @test tech_energy(mkinput(Dict{Int, Dict{Int, Vector{CostSegment}}}())) ≈
                  24 * 100.0 rtol = 1e-6
            @test tech_energy(mkinput(curve)) ≈ 24 * 50.0 rtol = 1e-6
            # A flat curve (one segment) prices the output at its own price:
            # at 1e-4, above diesel, the gas does not run. A single segment
            # used to be ignored (priced at the flat fuel_cost 1e-5).
            flat = Dict(1 => Dict(1 => [CostSegment(1.0, 1.0e-4)]))
            @test tech_energy(mkinput(flat)) ≈ 0.0 atol = 1e-6
            # Same rule for an existing generator, in the master and the dispatch
            gas_gen = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-5],
                            ones(H, 1); nb = 1, H = H)
            for curves in (Dict{Int, Dict{Int, Vector{CostSegment}}}(),
                           Dict(2 => Dict(1 => [CostSegment(1.0, 1.0e-4)])))
                ginp = MasterProblemInput(
                    years = [2025], network = net, generators = [diesel, gas_gen],
                    batteries = BatteryConfig[], base_demand = fill(100.0, H, 1),
                    target_re_penetration = 0.0, initial_re_penetration = 0.0,
                    representative_days_per_year = 1, min_day_separation = 1,
                    gen_cost_curves = curves, verbose = false)
                gm, _, _ = create_master_problem(ginp; use_representative_days = true)
                optimize!(gm)
                @test string(termination_status(gm)) == "OPTIMAL"
                gas_mwh = sum(value(v) for v in all_variables(gm)
                              if startswith(name(v), "op_y1_d1_gen[2,"))
                dinput = PowerSystemInput(
                    name = "flat", year = 2025, network = net,
                    generators = [diesel, gas_gen], batteries = BatteryConfig[],
                    demand = fill(100.0, H, 1),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    gen_cost_curves = curves,
                    mode = "economic_dispatch", solver_name = "highs", verbose = false)
                dm, dv = create_power_system(dinput)
                optimize!(dm)
                @test string(termination_status(dm)) == "OPTIMAL"
                expected = isempty(curves) ? 24 * 100.0 : 0.0
                @test gas_mwh ≈ expected atol = 1e-6
                @test sum(value.(dv.gen_output[2, 1, :])) ≈ expected atol = 1e-6
            end

            # Benders: the subproblem prices the segments too, and the cut
            # gradient includes the segment caps (fraction × capacity), so it
            # matches the cost change of one more MW of gas. At 60 MW the two
            # 30 MW segments (1e-5 and 3e-5) both run and displace diesel.
            curve2 = Dict(1 => Dict(1 => [CostSegment(0.5, 1.0e-5),
                                          CostSegment(0.5, 3.0e-5)]))
            inp = mkinput(curve2)
            _, bvars, _, _, _ = ESFEX.create_benders_master(inp)
            empty_caps() = Dict{Tuple{Int,Int}, Float64}()
            function sp_solve(cap)
                caps = ESFEX.FixedCapacities(Dict((1, 1) => 100.0), empty_caps(),
                    empty_caps(), Dict((1, 1) => cap), empty_caps(), empty_caps(),
                    Dict{Int, Float64}(), [0.0])
                sp = ESFEX.create_benders_subproblem(inp, 1, 1, fill(100.0, H, 1), 1,
                                                     caps, bvars)
                optimize!(sp.model)
                @test string(termination_status(sp.model)) == "OPTIMAL"
                td, _, _ = ESFEX.extract_subproblem_duals(sp, inp)
                return objective_value(sp.model), get(td, (1, 1), 0.0), sp.scaling_factor
            end
            q60, grad60, sf = sp_solve(60.0)
            q61, _, _ = sp_solve(61.0)
            @test grad60 ≈ q61 - q60 rtol = 1e-6
            # Each MW of gas runs half at 1e-5 and half at 3e-5 and displaces
            # diesel at 5e-5: 3e-5 saved per MWh (the flat 1e-5 would save 4e-5)
            @test grad60 ≈ -24 * sf * 3.0e-5 rtol = 1e-6
            q20, _, _ = sp_solve(20.0)
            @test q20 - q60 ≈ 40 * 24 * sf * 3.0e-5 rtol = 1e-6
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

        @testset "master reservoir builds raise the reservoir's level bounds" begin
            # 50 MW hydro with 25 MWh/h of inflow and a 100 MWh reservoir
            # (starts and ends at 50 MWh); no load for 12 h, then 50 MW for
            # 12 h, with diesel at 1e-3 as the alternative. Storing the
            # first half's 300 MWh needs 350 MWh: the master builds 250. The
            # build used to be charged but never added to the reservoir.
            hydro = ESFEX.with_fields(
                mkgen("Hydro", "Renewable", "Water", [50.0], [0.0], ones(H24, 1);
                      nb = 1, H = H24);
                reservoir_capacity = [100.0], reservoir_initial_level = [0.5],
                reservoir_min_level = [0.0], reservoir_max_level = [1.0],
                reservoir_inflow = fill(25.0, H24, 1), reservoir_turbine_efficiency = [1.0],
                reservoir_evaporation_rate = [0.0], reservoir_pump_capacity = [0.0],
                reservoir_pump_efficiency = [1.0], reservoir_spillage_allowed = true,
                reservoir_invest_cost = [1.0e-6], reservoir_invest_max = [1000.0])
            diesel = mkgen("Diesel", "Non-renewable", "Diesel", [100.0], [1.0e-3],
                           ones(H24, 1); nb = 1, H = H24)
            dem = reshape(vcat(zeros(12), fill(50.0, 12)), H24, 1)
            inp = MasterProblemInput(
                years = [2025, 2026], network = net1, generators = [hydro, diesel],
                batteries = BatteryConfig[], base_demand = vcat(dem, dem),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                hours_per_year = [24, 24], verbose = false)
            model, mvars = master1(inp)
            res = extract_master_solution(model, mvars, inp)
            @test res.reservoir_investment[1][1][1] ≈ 250.0 rtol = 1e-6
            # Built once: year 2 already has it
            @test get(res.reservoir_investment[2], 1, [0.0])[1] ≈ 0.0 atol = 1e-6
            @test res.total_investment_by_year[1] ≈ 250.0 * 1.0e-6 rtol = 1e-6
            # Year 2 still has it: the hydro covers all 600 MWh of the load
            @test sum(mvals(model, "op_y2_d1_gen[1,")) ≈ 600.0 rtol = 1e-6
        end

        @testset "total_investment_by_year counts transmission builds" begin
            # 10 MW line to the load bus: the master adds 40 MW, charged at
            # the dearer end (2e-4 per MW); it was left out of the total.
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            line = TransmissionLineData("l1", 1, 2, 10.0, 0.1, 0.0, 0.0, 100.0, 220.0,
                                        1, 50.0, "AC")
            net = NetworkConfig(2, 2, buses, [1, 2], [0 10.0; 10.0 0], zeros(2, 2),
                100.0, 0.4, 220.0, 0.6, 1, [1.0e-4, 2.0e-4], [100.0, 100.0],
                [line], TransformerData[], ACDCConverterData[], FrequencyConverterData[], 0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0, 100.0], [1.0e-5, 1.0e-3],
                        ones(H24, 2); nb = 2, H = H24)
            inp = MasterProblemInput(
                years = [2025], network = net, generators = [gas],
                batteries = BatteryConfig[], base_demand = hcat(zeros(H24), fill(50.0, H24)),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                transmission_loss_segments = 0, verbose = false)
            model, mvars = master1(inp)
            res = extract_master_solution(model, mvars, inp)
            built = maximum(values(res.transfer_investment[1]))
            @test built ≈ 40.0 rtol = 1e-6
            @test res.total_investment_by_year[1] ≈ 40.0 * 2.0e-4 rtol = 1e-6
        end

        @testset "invest_cost_conventional and converter_invest_cost scale their own builds" begin
            # converter_invest_cost overwrote invest_cost_conventional, so
            # conventional technologies were priced with the converter one.
            o = [0.0]
            gt = TechnologyConfig("GT", "Non-renewable", "Gas", [1.0], [50.0], ones(H24, 1),
                                  [1.0], [1.0], [1.0], [1.0], o, o, o, o, o, o, o, o,
                                  [25.0], o, o, 50.0, "AC", false, [1.0])
            c = ACDCConverterData("c1", "VSC", 1, 1, 220.0, 320.0, 10.0, 0.0, 1.0, 1.0,
                                  0.0, -50.0, 50.0, 1.0, 0.05, 0.01, 2.0, 0.0, 0.0, 20.0,
                                  30, 0, 0.0)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-5], ones(H24, 1);
                        nb = 1, H = H24)
            inp = MasterProblemInput(years = [2025],
                network = ESFEX.with_fields(net1; acdc_converters = [c]),
                generators = [gas], batteries = BatteryConfig[], technologies = [gt],
                base_demand = fill(50.0, H24, 1), target_re_penetration = 0.0,
                initial_re_penetration = 0.0, representative_days_per_year = 1,
                min_day_separation = 1, verbose = false)
            model = ESFEX.Model()
            vars = ESFEX.build_master_variables!(model, inp)
            vt = vars.tech_investment[1][1][1]
            vc = vars.converter_investment[1][("acdc", 1)]
            function coefs(m)
                e = ESFEX.build_investment_cost_expression(vars, inp; mult = m)
                return coefficient(e, vt), coefficient(e, vc)
            end
            @test coefs(ScenarioMultipliers()) == (1.0, 2.0)
            @test coefs(ScenarioMultipliers(invest_cost_conventional = 3.0)) == (3.0, 2.0)
            @test coefs(ScenarioMultipliers(converter_invest_cost = 7.0)) == (1.0, 14.0)
        end

        @testset "master: flow cost of inter-system links" begin
            # 10 MW flow every hour on a line charging 3e-6 per MWh: the plan
            # costs 10 × 24 × 365 × 3e-6 more; the multiplier doubles it.
            # A parallel line and a transformer on the same bus pair carry
            # 10 MW each and charge nothing, as in the dispatch: the master
            # used to charge the pair's cost on all three.
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            link(cost) = TransmissionLineData("l1", 1, 2, 10.0, 0.1, 0.0, 0.0, 100.0, 220.0,
                                              1, 50.0, "AC", cost)
            other = TransmissionLineData("l2", 1, 2, 10.0, 0.1, 0.0, 0.0, 100.0, 220.0,
                                         1, 50.0, "AC")
            trafo = TransformerData("T1", 1, 2, 220.0, 220.0, 10.0, 0.1, 0.0, 0.1, 1.0, 0.0)
            net2(cost) = NetworkConfig(2, 2, buses, [1, 2], [0 20.0; 20.0 0], zeros(2, 2),
                100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                [link(cost), other], [trafo], ACDCConverterData[], FrequencyConverterData[],
                0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0, 100.0], [1.0e-5, 1.0e-3],
                        ones(H24, 2); nb = 2, H = H24)
            mk(cost) = MasterProblemInput(
                years = [2025], network = net2(cost), generators = [gas],
                batteries = BatteryConfig[], base_demand = fill(50.0, H24, 2),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                transmission_loss_segments = 0, verbose = false)
            m0, _ = master1(mk(0.0))
            m1, _ = master1(mk(3.0e-6))
            m2, _ = master1(mk(3.0e-6);
                            mult = ScenarioMultipliers(transmission_cost_per_mw_km = 2.0))
            @test objective_value(m1) - objective_value(m0) ≈ 10 * 24 * 365 * 3.0e-6 rtol = 1e-6
            @test objective_value(m2) - objective_value(m0) ≈ 2 * 10 * 24 * 365 * 3.0e-6 rtol = 1e-6
            # Same per-line charge in the dispatch over the day
            input = PowerSystemInput(
                name = "flow", year = 2025, network = net2(3.0e-6), generators = [gas],
                batteries = BatteryConfig[], demand = fill(50.0, H24, 2),
                temporal = TemporalConfig(H24, 1, H24, 0, H24, H24, 1, 1, 1),
                pwl_loss_segments = 0,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            dmodel, _ = create_power_system(input)
            optimize!(dmodel)
            @test value(dmodel.ext[:cost_expressions][:line_flow_cost]) ≈ 10 * 24 * 3.0e-6 rtol = 1e-6
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
            ln = TransmissionLineData("l1", 1, 1, 10.0, 0.1, 0.0, 0.0, 100.0, 220.0,
                                      1, 50.0, "AC", 4.0)
            net = ESFEX.with_fields(net1; acdc_converters = [c], transmission_lines = [ln])
            inp = MasterProblemInput(
                years = [2025], network = net, generators = [gen], batteries = [bat],
                battery_technologies = [li], base_demand = fill(50.0, H24, 1),
                co2_budget_groups = [CO2BudgetGroup("A", [1], 10.0, 7.0)],
                verbose = false)
            si = ESFEX.scenario_master_input(inp, ScenarioMultipliers(
                maintenance_cost = 2.0, bat_maintenance_cost = 3.0, gen_start_up_cost = 5.0,
                converter_variable_cost = 6.0, transmission_cost_per_mw_km = 7.0,
                penalty_co2_budget_violation = 8.0), 1)
            @test si.generators[1].maintenance_cost == [2.0]
            @test si.generators[1].start_up_cost == [10.0]
            @test si.batteries[1].maintenance_cost == [12.0]
            @test si.battery_technologies[1].maintenance_cost == [18.0]
            @test si.network.acdc_converters[1].variable_cost == 30.0
            @test si.network.transmission_lines[1].flow_cost == 28.0
            @test si.co2_budget_groups[1].violation_penalty == 56.0
            @test si.co2_budget_groups[1].budget == 10.0
        end

        @testset "Benders reaches the monolithic optimum" begin
            # The Benders subproblems used to miss converters (no power, no
            # build), the CO2 budget, the line flow cost, LP start-ups and the
            # candidates' curtailment cost, and the cuts had no gradient for
            # line builds, life extension or the RE ratio.
            function both(inp)
                model, mvars = master1(inp)
                br = run_benders_decomposition(inp; max_iterations = 40, tolerance = 1e-7,
                                               verbose_benders = false)
                @test br.gap < 1e-6
                @test br.objective ≈ objective_value(model) rtol = 1e-6
                return extract_master_solution(model, mvars, inp), br.solution
            end
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            link_line(fc) = TransmissionLineData("l1", 1, 2, 10.0, 0.1, 0.0, 0.0, 100.0, 220.0,
                                                 1, 50.0, "AC", fc)
            net2(; conv = ACDCConverterData[], tcost = [0.0, 0.0], tmax = [0.0, 0.0],
                 flow_cost = 0.0) =
                NetworkConfig(2, 2, buses, [1, 2], [0 10.0; 10.0 0], zeros(2, 2),
                    100.0, 0.4, 220.0, 0.6, 1, tcost, tmax, [link_line(flow_cost)],
                    TransformerData[], conv, FrequencyConverterData[], 0.1)

            # Converter: 30 MW + 10 MW built carry the 40 MW the line cannot
            c1 = ACDCConverterData("c1", "VSC", 1, 2, 220.0, 320.0, 30.0, 0.0,
                1.0, 1.0, 0.0, -50.0, 50.0, 1.0, 0.05, 0.01, 1.0e-3, 0.0, 2.0e-6, 50.0,
                30, 0, 0.0)
            gas2 = mkgen("Gas", "Non-renewable", "Gas", [100.0, 0.0], [1.0e-5, 0.0],
                         ones(H24, 2); nb = 2, H = H24)
            mono, bend = both(MasterProblemInput(
                years = [2025], network = net2(conv = [c1]), generators = [gas2],
                batteries = BatteryConfig[], base_demand = hcat(zeros(H24), fill(50.0, H24)),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                transmission_loss_segments = 0, verbose = false))
            @test mono.converter_investment[1][("acdc", 1)] ≈ 10.0 atol = 1e-6
            @test bend.converter_investment[1][("acdc", 1)] ≈ 10.0 atol = 1e-6

            # Line build and flow cost: 40 MW more on the corridor beat the
            # dear unit at bus 2, net of the 3e-6 per MWh the line charges
            gas_ab = mkgen("Gas", "Non-renewable", "Gas", [100.0, 100.0], [1.0e-5, 1.0e-3],
                           ones(H24, 2); nb = 2, H = H24)
            mono, bend = both(MasterProblemInput(
                years = [2025],
                network = net2(tcost = [0.05, 0.05], tmax = [100.0, 100.0], flow_cost = 3.0e-6),
                generators = [gas_ab], batteries = BatteryConfig[],
                base_demand = hcat(zeros(H24), fill(50.0, H24)),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                transmission_loss_segments = 0, verbose = false))
            @test mono.transfer_investment[1][(1, 2)] ≈ 40.0 atol = 1e-6
            @test bend.transfer_investment[1][(1, 2)] ≈ 40.0 atol = 1e-4

            # Converter fixed cost: at 10 per MW-year the 10 MW build no longer
            # pays off (the Benders master did not charge it and built 10 MW)
            c1_fixed = ACDCConverterData("c1", "VSC", 1, 2, 220.0, 320.0, 30.0, 0.0,
                1.0, 1.0, 0.0, -50.0, 50.0, 1.0, 0.05, 0.01, 1.0e-3, 10.0, 2.0e-6, 50.0,
                30, 0, 0.0)
            mono, bend = both(MasterProblemInput(
                years = [2025], network = net2(conv = [c1_fixed]), generators = [gas_ab],
                batteries = BatteryConfig[], base_demand = hcat(zeros(H24), fill(50.0, H24)),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                transmission_loss_segments = 0, verbose = false))
            @test mono.converter_investment[1][("acdc", 1)] ≈ 0.0 atol = 1e-6
            @test bend.converter_investment[1][("acdc", 1)] ≈ 0.0 atol = 1e-6

            # CO2 budget over two representative days (50 and 80 MW of load;
            # coal emits 1 t/MWh_e and has 60 MW): 2000 t for 2640 t of coal.
            # Dear overrun → gas; cheap overrun → violation.
            coal = mkgen("Coal", "Non-renewable", "Coal", [60.0], [1.0e-5],
                         ones(48, 1); nb = 1, H = 48)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4],
                        ones(48, 1); nb = 1, H = 48)
            for pen in (5.0e-4, 1.0e-5)
                both(MasterProblemInput(
                    years = [2025], network = net1, generators = [coal, gas],
                    batteries = BatteryConfig[],
                    base_demand = reshape(vcat(fill(50.0, 24), fill(80.0, 24)), 48, 1),
                    hours_per_year = [48], target_re_penetration = 0.0,
                    initial_re_penetration = 0.0, representative_days_per_year = 2,
                    min_day_separation = 1, fuel_co2 = Dict("Coal" => 0.5),
                    co2_budget_groups = [CO2BudgetGroup("A", [1], 2000.0, pen)],
                    verbose = false))
            end

            # A binding CO2 budget over 6 and 12 days of different load: with
            # one cut per year (not per day) Benders stopped at the iteration
            # cap 0.16 % / 0.39 % above the optimum
            for nd in (6, 12)
                Hd = 24 * nd
                dem = reshape(vcat([fill(40.0 + 5.0 * d, 24) for d in 1:nd]...), Hd, 1)
                coal_d = mkgen("Coal", "Non-renewable", "Coal", [60.0], [1.0e-5],
                               ones(Hd, 1); nb = 1, H = Hd)
                gas_d = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4],
                              ones(Hd, 1); nb = 1, H = Hd)
                both(MasterProblemInput(
                    years = [2025, 2026], network = net1, generators = [coal_d, gas_d],
                    batteries = BatteryConfig[], base_demand = vcat(dem, dem),
                    hours_per_year = [Hd, Hd], target_re_penetration = 0.0,
                    initial_re_penetration = 0.0, discount_rate = 0.05,
                    representative_days_per_year = nd, min_day_separation = 1,
                    fuel_co2 = Dict("Coal" => 0.5),
                    co2_budget_groups = [CO2BudgetGroup("A", [1], 0.6 * sum(min.(dem, 60.0)),
                                                        5.0e-4)],
                    verbose = false))
            end

            # Warm-start cuts at the degraded capacity of an existing unit:
            # at the rated 100 MW of a wind farm that has lost 18 % the cut
            # bounded the cost from above (LB 731.78 > UB 730.18, no convergence)
            wind_old = ESFEX.with_fields(
                mkgen("Wind", "Renewable", "Wind", [100.0], [0.0],
                      reshape(vcat(ones(12), zeros(12)), H24, 1); nb = 1, H = H24);
                degradation_rate = [0.02], initial_age = [10.0])
            coal24 = mkgen("Coal", "Non-renewable", "Coal", [100.0], [1.0e-5],
                           ones(H24, 1); nb = 1, H = H24)
            gas24 = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4],
                          ones(H24, 1); nb = 1, H = H24)
            both(MasterProblemInput(
                years = [2025], network = net1, generators = [wind_old, coal24, gas24],
                batteries = BatteryConfig[], base_demand = fill(50.0, H24, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                max_curtailment_ratio = 1.0, fuel_co2 = Dict("Coal" => 0.5),
                co2_budget_groups = [CO2BudgetGroup("A", [1], 100000.0, 5.0e-4)],
                verbose = false))

            # LP start-ups with unit commitment in the dispatch
            uc_gen = ESFEX.with_fields(
                mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-5],
                      ones(H24, 1); nb = 1, H = H24);
                min_power = [0.5], start_up_cost = [2.0e-3])
            both(MasterProblemInput(
                years = [2025], network = net1, generators = [uc_gen],
                batteries = BatteryConfig[],
                base_demand = reshape(vcat(zeros(12), fill(100.0, 12)), H24, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                unit_commitment = true, verbose = false))

            # Life extension: an expired cheap unit is kept for 50 MW of load
            old = ESFEX.with_fields(
                mkgen("Coal", "Non-renewable", "Coal", [100.0], [1.0e-5],
                      ones(H24, 1); nb = 1, H = H24);
                life_time = [25.0], initial_age = [30.0], invest_cost = [1.0])
            diesel = mkgen("Diesel", "Non-renewable", "Diesel", [100.0], [1.0e-3],
                           ones(H24, 1); nb = 1, H = H24)
            mono, bend = both(MasterProblemInput(
                years = [2025], network = net1, generators = [old, diesel],
                batteries = BatteryConfig[], base_demand = fill(50.0, H24, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1, verbose = false))
            @test mono.cumulative_gen_capacity[1][1][1] ≈ 50.0 atol = 1e-6
            @test bend.cumulative_gen_capacity[1][1][1] ≈ 50.0 atol = 1e-4

            # Reservoir build: storing the dry half-day's 300 MWh of inflow
            # needs 350 MWh of reservoir, 250 more than the 100 it has (see
            # "master reservoir builds raise the reservoir's level bounds")
            hydro = ESFEX.with_fields(
                mkgen("Hydro", "Renewable", "Water", [50.0], [0.0], ones(H24, 1);
                      nb = 1, H = H24);
                reservoir_capacity = [100.0], reservoir_initial_level = [0.5],
                reservoir_min_level = [0.0], reservoir_max_level = [1.0],
                reservoir_inflow = fill(25.0, H24, 1), reservoir_turbine_efficiency = [1.0],
                reservoir_evaporation_rate = [0.0], reservoir_pump_capacity = [0.0],
                reservoir_pump_efficiency = [1.0], reservoir_spillage_allowed = true,
                reservoir_invest_cost = [1.0e-6], reservoir_invest_max = [1000.0])
            dem = reshape(vcat(zeros(12), fill(50.0, 12)), H24, 1)
            mono, bend = both(MasterProblemInput(
                years = [2025, 2026], network = net1, generators = [hydro, diesel],
                batteries = BatteryConfig[], base_demand = vcat(dem, dem),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                hours_per_year = [24, 24], verbose = false))
            @test mono.reservoir_investment[1][1][1] ≈ 250.0 rtol = 1e-6
            @test bend.reservoir_investment[1][1][1] ≈ 250.0 rtol = 1e-4

            # Curtailment cost on candidate PV: at 2e-5 per MWh curtailed the
            # 16.7 MW that would saturate the peak hours no longer pay off
            o = [0.0]
            sun = reshape(vcat(zeros(6), [0.25, 0.5, 0.75], ones(6), [0.75, 0.5, 0.25],
                               zeros(6)), H24, 1)
            pv = TechnologyConfig("PV", "Renewable", "Sun", [0.08], [100.0], sun,
                                  [1.0], [1.0], [1.0], [1.0], o, o, o, o, o, o, o, o,
                                  [25.0], o, o, 50.0, "AC", false, [1.0])
            gas1 = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4],
                         ones(H24, 1); nb = 1, H = H24)
            for (curt, built) in ((0.0, 200 / 3), (2.0e-5, 50.0))
                mono, bend = both(MasterProblemInput(
                    years = [2025], network = net1, generators = [gas1],
                    batteries = BatteryConfig[], technologies = [pv],
                    base_demand = fill(50.0, H24, 1),
                    target_re_penetration = 0.0, initial_re_penetration = 0.0,
                    representative_days_per_year = 1, min_day_separation = 1,
                    curtailment_cost = curt, max_curtailment_ratio = 0.99, verbose = false))
                @test mono.tech_investment[1][1][1] ≈ built rtol = 1e-6
                @test bend.tech_investment[1][1][1] ≈ built rtol = 1e-4
            end

            # No Benders-only investment rules: with a 20 % RE target already
            # met and dear PV and batteries, nothing is built (the Benders
            # master forced 0.07 MW of PV and 0.35 MWh of battery)
            pv_dear = ESFEX.with_fields(pv; invest_cost = [1.0])
            li_dear = BatteryTechnologyConfig("LiNew", [1.0], [1.0], [100.0], [400.0], 1.0, 8.0,
                [0.95], [0.95], o, [15.0], [0.5], [0.0], [1.0], o, o, o, false, "AC", o, [1.0])
            for bts in (BatteryTechnologyConfig[], [li_dear])
                mono, bend = both(MasterProblemInput(
                    years = [2025], network = net1, generators = [gas1],
                    batteries = BatteryConfig[], technologies = [pv_dear],
                    battery_technologies = bts, base_demand = fill(50.0, H24, 1),
                    target_re_penetration = 0.2, initial_re_penetration = 0.2,
                    representative_days_per_year = 1, min_day_separation = 1, verbose = false))
                @test bend.tech_investment[1][1][1] ≈ 0.0 atol = 1e-6
                isempty(bts) || @test bend.bat_tech_capacity_investment[1][1][1] ≈ 0.0 atol = 1e-6
            end

            # Hard curtailment limit, as in the monolithic master: cheap PV
            # stops at 700/13 MW, where 5 % of its output is curtailed (the
            # soft Benders limit let it build 66.7 MW). The subproblems of
            # larger builds cannot meet the limit and add feasibility cuts.
            li_cheap = BatteryTechnologyConfig("LiNew", [0.05], [0.02], [100.0], [800.0],
                1.0, 8.0, [0.95], [0.95], o, [15.0], [0.5], [0.0], [1.0], o, o, o, false,
                "AC", o, [1.0])
            pv_cheap = ESFEX.with_fields(pv; invest_cost = [0.001], invest_max = [1000.0])
            gas_dear = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-3],
                             ones(H24, 1); nb = 1, H = H24)
            for (ratio, bts, target) in ((0.05, BatteryTechnologyConfig[], 0.0),
                                         (0.2, [li_cheap], 0.3))
                mono, bend = both(MasterProblemInput(
                    years = [2025], network = net1, generators = [gas_dear],
                    batteries = BatteryConfig[], technologies = [pv_cheap],
                    battery_technologies = bts, base_demand = fill(50.0, H24, 1),
                    target_re_penetration = target, initial_re_penetration = target,
                    max_curtailment_ratio = ratio, curtailment_cost = 1.0e-5,
                    representative_days_per_year = 1, min_day_separation = 1, verbose = false))
                @test bend.tech_investment[1][1][1] ≈ mono.tech_investment[1][1][1] rtol = 1e-6
                isempty(bts) && @test mono.tech_investment[1][1][1] ≈ 700 / 13 rtol = 1e-6
            end

            # Stopped at the iteration cap, Benders returns the plan of its
            # best iteration with that iteration's cost: the plan costs
            # exactly the reported objective (it returned the next master
            # solution with the best iteration's cost)
            li_ok = BatteryTechnologyConfig("LiNew", [0.01], [0.01], [100.0], [800.0], 1.0, 8.0,
                [0.95], [0.95], o, [15.0], [0.5], [0.0], [1.0], o, o, o, false, "AC", o, [1.0])
            pv_ok = ESFEX.with_fields(pv; invest_cost = [0.02], invest_max = [200.0])
            inp = MasterProblemInput(
                years = [2025], network = net1, generators = [gas1], batteries = BatteryConfig[],
                technologies = [pv_ok], battery_technologies = [li_ok],
                base_demand = fill(50.0, H24, 1), target_re_penetration = 0.0,
                initial_re_penetration = 0.0, max_curtailment_ratio = 0.99,
                representative_days_per_year = 1, min_day_separation = 1, verbose = false)
            for iters in (2, 3)
                br = run_benders_decomposition(inp; max_iterations = iters, tolerance = 1e-9,
                                               verbose_benders = false)
                @test br.iterations == iters && br.gap > 1e-9
                model, mvars, _ = create_master_problem(inp; use_representative_days = true)
                fix(mvars.tech_investment[1][1][1], br.solution.tech_investment[1][1][1];
                    force = true)
                fix(mvars.bat_tech_power_investment[1][1][1],
                    br.solution.bat_tech_power_investment[1][1][1]; force = true)
                fix(mvars.bat_tech_capacity_investment[1][1][1],
                    br.solution.bat_tech_capacity_investment[1][1][1]; force = true)
                optimize!(model)
                @test objective_value(model) ≈ br.objective rtol = 1e-6
                @test br.solution.objective ≈ br.objective rtol = 1e-9
            end
        end

        @testset "dispatch charges converter costs and full start-ups" begin
            # Converter variable cost: build_objective! ran before the
            # converter variables existed, so the term was always empty.
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            line = TransmissionLineData("l1", 1, 2, 10.0, 0.1, 0.0, 0.0, 100.0, 220.0,
                                        1, 50.0, "AC")
            conv = ACDCConverterData("c1", "VSC", 1, 2, 220.0, 320.0, 40.0, 0.0,
                1.0, 1.0, 0.0, -50.0, 50.0, 1.0, 0.05, 0.01, 1.0e-3, 0.0, 2.0e-6, 50.0,
                30, 0, 0.0)
            net2 = NetworkConfig(2, 2, buses, [1, 2], [0 10.0; 10.0 0], zeros(2, 2),
                100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                [line], TransformerData[], [conv], FrequencyConverterData[], 0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0, 0.0], [1.0e-5, 0.0],
                        ones(H24, 2); nb = 2, H = H24)
            input = PowerSystemInput(
                name = "conv", year = 2025, network = net2, generators = [gas],
                batteries = BatteryConfig[], demand = hcat(zeros(H24), fill(50.0, H24)),
                temporal = TemporalConfig(H24, 1, H24, 0, H24, H24, 1, 1, 1),
                pwl_loss_segments = 0,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            rect = value.(vars.acdc_rectify[1, :])
            @test sum(rect) ≈ 40.0 * H24 atol = 1e-6
            res = extract_solution(model, vars, input)
            @test res.cost_breakdown.converter_cost ≈ sum(rect) * 2.0e-6 rtol = 1e-6
            @test coefficient(objective_function(model), vars.acdc_rectify[1, 1]) ≈ 2.0e-6

            # Start-ups are events: one start costs start_up_cost, not
            # start_up_cost × window/8760 (the objective prorated it).
            gen = ESFEX.with_fields(
                mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-5],
                      ones(H24, 1); nb = 1, H = H24);
                min_power = [0.5], start_up_cost = [2.0e-3])
            input = PowerSystemInput(
                name = "uc", year = 2025, network = net1, generators = [gen],
                batteries = BatteryConfig[],
                demand = reshape(vcat(zeros(12), fill(100.0, 12)), H24, 1),
                temporal = TemporalConfig(H24, 1, H24, 0, H24, H24, 1, 1, 1),
                mode = "unit_commitment", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test coefficient(objective_function(model), vars.gen_startup[1, 1, 13]) ≈ 2.0e-3
            res = extract_solution(model, vars, input)
            @test objective_value(model) ≈ res.cost_breakdown.fuel_cost +
                res.cost_breakdown.fixed_om_cost + res.cost_breakdown.maintenance_cost +
                res.cost_breakdown.startup_cost + res.cost_breakdown.co2_emission_cost +
                res.cost_breakdown.load_shedding_cost + res.cost_breakdown.fre_penetration_cost +
                res.cost_breakdown.reserve_static_cost + res.cost_breakdown.reserve_dynamic_cost +
                res.cost_breakdown.inertia_cost rtol = 1e-6
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
