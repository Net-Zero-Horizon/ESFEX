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
using JuMP: optimize!, termination_status, value, constraint_by_name, variable_by_name, objective_value,
             coefficient, objective_function, all_variables, name, fix, constant, is_binary,
             list_of_constraint_types, all_constraints, constraint_object, AffExpr,
             normalized_coefficient
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

    @testset "CallbackLogger hands records to the host" begin
        got = Tuple{Int, String, Bool}[]
        lg = CallbackLogger((l, m, ms) -> push!(got, (l, m, ms)))
        Base.CoreLogging.with_logger(lg) do
            @info "plain" k = 1
            @warn "careful"
            ESFEX.eval(:(@debug "esfex debug"))
            @debug "foreign debug"
            ESFEX.eval(:(@info "Benders iteration 1" _group = :benders))
            for _ in 1:3
                @info "once" maxlog = 1
            end
        end
        @test got == [(20, "plain\n  k = 1", false), (30, "careful", false),
                      (10, "esfex debug", false), (20, "Benders iteration 1", true),
                      (20, "once", false)]
        # Records from another thread wait for the logger's own thread
        empty!(got)
        lg.thread = -1
        Base.CoreLogging.with_logger(() -> @info("queued"), lg)
        @test isempty(got)
        lg.thread = Threads.threadid()
        @test flush_log_queue(lg) == 1
        @test got == [(20, "queued", false)]
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

        # A line's r, x, b are those of all its circuits together: the
        # circuit count does not rescale them (it used to halve them again).
        Yp = build_ybus([line(x = 0.2, r = 0.02, b = 0.06, circuits = 2)],
                        TransformerData[], 2)
        @test Yp[1, 2] ≈ -1.0 / complex(0.02, 0.2)
        @test Yp[1, 1] ≈ 1.0 / complex(0.02, 0.2) + im * 0.03

        # Same for the DC network: capacity and reactance as given
        dc_lines = [line(x = 0.05, circuits = 2), line(from = 2, to = 3, x = 0.1)]
        dc_net = NetworkConfig(3, 3, BusData[], [1, 2, 3], zeros(3, 3), zeros(3, 3),
                               100.0, 0.4, 220.0, 0.6, 1, zeros(3), zeros(3), dc_lines,
                               TransformerData[], ACDCConverterData[],
                               FrequencyConverterData[], 0.1)
        _, dc_caps, dc_x = ESFEX.build_from_line_data(dc_net)
        @test dc_caps == [100.0, 100.0]
        @test dc_x == [0.05, 0.1]

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

        # solver.time_limit 0 = no limit: a model solves instead of stopping
        # at once (HiGHS read 0 s as stop now: TIME_LIMIT on this LP)
        m = ESFEX.Model(create_optimizer(solver_name = "highs", time_limit = 0.0))
        x = ESFEX.JuMP.@variable(m, [1:200], lower_bound = 0, upper_bound = 10)
        ESFEX.JuMP.@constraint(m, [i in 1:199], x[i] + 2x[i+1] >= 1 + mod(i, 7))
        ESFEX.JuMP.@objective(m, Min, sum((1 + mod(i, 5)) * x[i] for i in 1:200))
        optimize!(m)
        @test termination_status(m) == ESFEX.MOI.OPTIMAL
        attrs(o) = Dict(p.first.name => p.second for p in o.params
                        if p.first isa ESFEX.MOI.RawOptimizerAttribute)
        @test !haskey(attrs(create_optimizer(solver_name = "highs", time_limit = 0.0)),
                      "time_limit")
        @test attrs(create_optimizer(solver_name = "highs", time_limit = 60.0))["time_limit"] == 60.0
        @test !ESFEX.has_time_limit(0.0) && !ESFEX.has_time_limit(Inf)
        @test ESFEX.has_time_limit(1.0)
        # Limits ESFEX sets below solver.time_limit (Benders) keep their cap
        @test ESFEX.capped_time_limit(0.0, 600.0) == 600.0
        @test ESFEX.capped_time_limit(100.0, 600.0) == 100.0
        @test ESFEX.capped_time_limit(7200.0, 600.0) == 600.0
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

    @testset "no forced replacement in the dispatch" begin
        # The dispatch's forced-replacement variables had a cost but no
        # constraint (always 0) and nothing set their inputs: removed.
        for f in (:unit_npv, :replacement_needed, :bat_unit_npv,
                  :bat_replacement_needed, :force_replacement_threshold,
                  :decommissioning_cost_gen, :decommissioning_cost_bat,
                  :discount_rate, :max_decommission_cost_per_mw,
                  :max_npv_penalty_per_mw)
            @test !hasfield(PowerSystemInput, f)
        end
        @test !hasfield(PowerSystemVariables, :gen_forced_replacement)
        @test !hasfield(PowerSystemVariables, :bat_forced_replacement)
        @test !hasfield(ESFEX.CostBreakdown, :npv_penalty_cost)
        @test !isdefined(ESFEX, :update_npv_from_results!)
        for f in (:max_npv_penalty_per_mw, :max_decommission_cost_per_mw,
                  :force_replacement_threshold)
            @test !hasfield(MasterProblemInput, f)
        end
        # The master's NPV retirement helpers stay (Julia API)
        @test hasfield(MasterProblemInput, :base_lcoe)
        @test hasfield(MasterProblemInput, :npv_annual_return_rate)
        @test isempty(methods(calculate_unit_npv, (Float64, Float64, Float64, Float64,
            Float64, Float64, Float64, Float64, Float64, Float64)))
        @test !isempty(methods(calculate_unit_npv,
            (MasterProblemResult, MasterProblemInput, String, Int, Int)))
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

        # GeneratorConfig has 38 positional fields; this keeps the boilerplate
        # in one place. Per-bus vectors are sized to `nb`; `avail` is the
        # [hours x buses] availability matrix. All reservoir fields are zeroed
        # (no hydro), reservable=false, risk_coefficient 1 (the schema
        # default: the unit's capacity counts in the master's adequacy).
        function mkgen(name, type, fuel, rated, fcost, avail; nb, H)
            z = zeros(nb)
            GeneratorConfig(
                name, type, fuel, rated, zeros(nb),
                fill(0.5, nb), fill(0.4, nb), ones(nb), ones(nb), z, z, z,
                fcost, z, z, z, z, avail, false,
                fill(30.0, nb), z, z, z, 50.0, "AC",
                z, z, z, z, zeros(H, nb), z, z, z, z, false, z, z, ones(nb),
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

            # Operating budget (COST-1) over the committed step 1 only: half
            # of its fuel (50 MW × 30 $/MWh). A MWh over the budget would
            # cost 30 × 1001 $ against a VOLL of 10 000 $/MWh, so half the
            # load is shed; step 2 (the overlap tail) is not capped.
            bmodel, bvars = create_power_system(input)
            cs = ESFEX.committed_steps(bvars, H, 1)
            add_operating_budget!(bmodel, 0.5 * 50.0 * 3.0e-5; committed = cs)
            optimize!(bmodel)
            bres = extract_solution(bmodel, bvars, input; committed = cs)
            @test bres.gen_output[1, 1, :] ≈ [25.0, 60.0] atol = 1e-6
            @test bres.cost_breakdown.operating_budget_excess ≈ 0.0 atol = 1e-12
            @test bres.cost_breakdown.fuel_cost ≈ 0.5 * 50.0 * 3.0e-5 rtol = 1e-9
            # A budget below what cannot be avoided: the excess is reported
            # and priced, and the objective is the sum of the components
            fmodel, fvars = create_power_system(input)
            add_operating_budget!(fmodel, -1.0)   # clamped to 0
            fix(fvars.gen_output[1, 1, 1], 10.0; force = true)
            optimize!(fmodel)
            fres = extract_solution(fmodel, fvars, input)
            cb = fres.cost_breakdown
            @test cb.operating_budget_excess ≈ 10.0 * 3.0e-5 rtol = 1e-9
            @test cb.operating_budget_penalty ≈ ESFEX.OPERATING_BUDGET_PENALTY * 10.0 * 3.0e-5 rtol = 1e-9
            parts = sum(getfield(cb, f) for f in fieldnames(typeof(cb))
                        if f ∉ (:total, :pe_electrolyzer_cost, :v2g_compensation,
                                 :operating_budget_excess))
            @test parts ≈ cb.total rtol = 1e-9
        end

        @testset "units retire at the end of life_time, degraded or not" begin
            # Only degraded units were retired: with degradation 0 a unit past
            # its life_time kept its full capacity in the dispatch (the master
            # retires it), and was charged fixed O&M on it.
            H = 2
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            function run_old(age; deg = 0.0, year = 2025)
                old = ESFEX.with_fields(
                    mkgen("Old", "Non-renewable", "Gas", [100.0], [3.0e-5],
                          ones(H, 1); nb = 1, H = H);
                    initial_age = [age], degradation_rate = [deg], fixed_cost = [1.0e-3])
                input = PowerSystemInput(
                    name = "eol", year = year, base_year = 2025, network = net,
                    generators = [old], batteries = BatteryConfig[],
                    demand = reshape([50.0, 50.0], H, 1),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    mode = "economic_dispatch", solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                res = extract_solution(model, vars, input)
                return res.total_generation, res.load_shed_total,
                       res.cost_breakdown.fixed_om_cost
            end
            # life_time 30 (mkgen): within it the unit serves the load
            gen, shed, fom = run_old(29.0)
            @test gen ≈ 100.0 atol = 1e-6
            @test shed ≈ 0.0 atol = 1e-6
            @test fom > 0
            # past it, without degradation: retired, no fixed O&M
            gen, shed, fom = run_old(31.0)
            @test gen ≈ 0.0 atol = 1e-6
            @test shed ≈ 100.0 atol = 1e-6
            @test fom ≈ 0.0 atol = 1e-12
            # it ages with the simulation year: 29 + 2 years
            gen, _, _ = run_old(29.0; year = 2027)
            @test gen ≈ 0.0 atol = 1e-6
            # with degradation (as before)
            gen, _, _ = run_old(31.0; deg = 0.01)
            @test gen ≈ 0.0 atol = 1e-6
            # Batteries alike
            yr = (year = 2025, base_year = 2025)
            bat(age) = BatteryConfig("Li1", [40.0], [20.0], [20.0], [0.95], [0.95],
                                     [0.0], [1.0], [0.5], [0.0], [0.0], [0.0],
                                     [10.0], [age], [0.0], 0.0, 100.0, [0.0],
                                     [0.0], false, "AC", [0.0], [0.0], [0.0])
            @test ESFEX.battery_power_limits(bat(9.0), 1, yr) == (40.0, 20.0, 20.0)
            @test ESFEX.battery_power_limits(bat(10.0), 1, yr) == (0.0, 0.0, 0.0)
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

        @testset "master: RE increment bounds stop at 100 %" begin
            # The minimum increment chained the credited share (at most 1)
            # from the anchor: an anchor above 1 - min_inc (a myopic year
            # anchored at last year's achieved share of 107 %, Cuba), or a
            # long chain, made the master infeasible in presolve. The floor
            # is now a priced target: E + slack ≥ floor × D.
            H = 48
            gas = mkgen("Gas", "Non-renewable", "Gas", [150.0],
                        [1.0], ones(H, 1); nb = 1, H = H)
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            function credited(years, init, lo, hi)
                inp = MasterProblemInput(
                    years = years, network = net, generators = [gas],
                    batteries = BatteryConfig[],
                    base_demand = fill(100.0, length(years) * 8760, 1),
                    target_re_penetration = 0.0, initial_re_penetration = init,
                    min_re_increment = lo, max_re_increment = hi,
                    representative_days_per_year = 1, min_day_separation = 1,
                    # RE-1 off (its year 1 is the initial share): the floor alone
                    re_target_override = Dict((1, y) => 0.0 for y in eachindex(years)),
                    loss_of_load_penalty = 10.0, verbose = false)
                model, mvars, _ = create_master_problem(inp; use_representative_days = true)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                # The share is renewable over total generation (here all gas)
                rec = model.ext[:master_re]
                return [(value(rec[(0, 1, y)][1]) + value(mvars.slack_re_target[(0, 1, y)])) /
                        value(rec[(0, 1, y)][2]) for y in eachindex(years)]
            end
            # One-year master anchored above 100 %: the floor stays at 1
            @test credited([2028], 1.0748, 0.01, 0.10)[1] ≈ 1.0 atol = 1e-6
            # Anchor 0.995: the last step is 0.005, not 0.01
            @test credited([2028], 0.995, 0.01, 0.10)[1] ≈ 1.0 atol = 1e-6
            # Perfect foresight: 0.8 + 0.15 per year passes 1 in year 2; the
            # credited share (paid as shortfall, no renewables) is held at
            # the floor 0.95, 1, 1
            r = credited([2025, 2026, 2027], 0.8, 0.15, 0.5)
            @test r ≈ [0.95, 1.0, 1.0] atol = 1e-6
            # A minimum increment above the maximum is no longer infeasible:
            # the floor is priced, the cap holds the actual share
            @test credited([2025], 0.0, 0.2, 0.1)[1] ≈ 0.2 atol = 1e-6
            @test ESFEX.re_increment_floor(0.8, 0.15, 0) ≈ 0.8
            @test ESFEX.re_increment_floor(0.8, 0.15, 1) ≈ 0.95
            @test ESFEX.re_increment_floor(0.8, 0.15, 2) == 1.0
            @test ESFEX.re_increment_floor(1.2, 0.01, 0) == 1.0
            # The RE-1 path never asks for more than 100 %
            mk2(init) = MasterProblemInput(
                years = [2025, 2026, 2027], network = net, generators = [gas],
                batteries = BatteryConfig[], base_demand = fill(100.0, H, 1),
                target_re_penetration = 1.0, initial_re_penetration = init,
                min_re_increment = 0.3, max_re_increment = 1.0)
            @test [calculate_target_ratios(mk2(0.6))[(1, y)] for y in 1:3] ≈ [0.6, 0.9, 1.0]

            # A Benders master that fails (a candidate whose build limit is
            # below 0) writes its LP only into debug_lp_dir; it used to go to
            # the temp folder on every verbose run.
            o = [0.0]
            broken = TechnologyConfig("PV", "Renewable", "Sun", [1.0], [-1.0], ones(8760, 1),
                                      [1.0], [1.0], [1.0], [1.0], o, o, o, o, o, o, o, o,
                                      [25.0], o, o, 50.0, "AC", false, [1.0])
            bad = MasterProblemInput(
                years = [2025], network = net, generators = [gas],
                batteries = BatteryConfig[], technologies = [broken],
                base_demand = fill(100.0, 8760, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                loss_of_load_penalty = 10.0, verbose = false)
            dir, tmp = mktempdir(), mktempdir()
            withenv("TMPDIR" => tmp) do
                @test_throws ErrorException run_benders_decomposition(
                    bad; max_iterations = 3, debug_lp_dir = dir)
                @test_throws ErrorException run_benders_decomposition(bad; max_iterations = 3)
            end
            @test isfile(joinpath(dir, "benders_master_failed_iter1.lp"))
            @test isempty(readdir(tmp))

            # The conflict of an infeasible master names the two bounds; a
            # model that was not found infeasible has none
            model, _, _ = create_master_problem(bad; use_representative_days = true)
            optimize!(model)
            @test sort(ESFEX.master_conflict(model)) == [
                "tech_inv_y1_t1[1] ≤ -1", "tech_inv_y1_t1[1] ≥ 0"]
            ok, _, _ = create_master_problem(MasterProblemInput(
                years = [2025], network = net, generators = [gas],
                batteries = BatteryConfig[], base_demand = fill(100.0, 8760, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                verbose = false); use_representative_days = true)
            optimize!(ok)
            @test isempty(ESFEX.master_conflict(ok))
        end

        @testset "master: RE growth capped on the actual share (RE-3)" begin
            # Cheap PV against 100 $/MWh gas, initial share 5 %, at most
            # +10 points a year. The increments bounded only the credited
            # share (≤ renewable energy + shortfall), never the renewable
            # energy itself: the master went 5 % → 100 % in year 1 (Cuba:
            # 5 % → 89 %).
            years = [2025, 2026, 2027]
            Hy = length(years) * 8760
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4],
                        ones(Hy, 1); nb = 1, H = Hy)
            o = [0.0]
            pv = TechnologyConfig("PV", "Renewable", "Sun", [1.0], [500.0], ones(Hy, 1),
                                  [1.0], [1.0], [1.0], [1.0], o, o, o, o, o, o, o, o,
                                  [25.0], o, o, 50.0, "AC", false, [1.0])
            g20(; existing_pv = 0.0, kw...) = MasterProblemInput(; years = years,
                network = net,
                generators = existing_pv > 0 ?
                    [gas, mkgen("PVold", "Renewable", "Sun", [existing_pv], [0.0],
                                ones(Hy, 1); nb = 1, H = Hy)] : [gas],
                batteries = BatteryConfig[], technologies = [pv],
                base_demand = fill(50.0, Hy, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.05,
                min_re_increment = 0.0, max_re_increment = 0.10,
                representative_days_per_year = 1, min_day_separation = 1,
                loss_of_load_penalty = 0.01, max_curtailment_ratio = 0.05,
                verbose = false, kw...)
            function solve_g20(inp)
                m, v, _ = create_master_problem(inp; use_representative_days = true)
                ESFEX.set_silent(m)
                optimize!(m)
                @test string(termination_status(m)) == "OPTIMAL"
                return m, v, extract_master_solution(m, v, inp)
            end
            cap = [0.15, 0.25, 0.35]

            inp = g20()
            m, v, res = solve_g20(inp)
            @test res.re_penetration_by_year ≈ cap atol = 1e-6
            @test [value(v.re_energy_ratio[(1, y)]) for y in 1:3] ≈ cap atol = 1e-6
            @test all(value(x) < 1e-6 for x in values(m.ext[:re_cap_slack]))
            # The cap is measured from the previous year's actual share: no
            # sun in 2025 (share 0) lets 2026 reach 10 %, not 25 %
            dark = vcat(zeros(8760, 1), ones(2 * 8760, 1))
            dinp = g20(technologies = [ESFEX.with_fields(pv; availability = dark)])
            dm, _, dres = solve_g20(dinp)
            @test dres.re_penetration_by_year ≈ [0.0, 0.10, 0.20] atol = 1e-6
            dbr = run_benders_decomposition(dinp; max_iterations = 60, tolerance = 1e-7,
                                            verbose_benders = false)
            @test dbr.objective ≈ objective_value(dm) rtol = 1e-6
            @test dbr.solution.re_penetration_by_year ≈ [0.0, 0.10, 0.20] atol = 1e-4
            # TSAM periods
            tinp = g20(use_tsam = true,
                       tsam_period_start_hours = [[1], [8761], [17521]],
                       tsam_period_weights = [[365.0], [365.0], [365.0]])
            tm, tv, tres = solve_g20(tinp)
            @test tres.re_penetration_by_year ≈ cap atol = 1e-6
            # Benders: the same plan and cost
            br = run_benders_decomposition(inp; max_iterations = 60, tolerance = 1e-7,
                                           verbose_benders = false)
            @test br.objective ≈ objective_value(m) rtol = 1e-6
            @test br.solution.re_penetration_by_year ≈ cap atol = 1e-4
            # Stochastic: per scenario, each on its own renewable energy
            sc = [Scenario("lo", 0.5, ScenarioMultipliers(fuel_cost = 1.0)),
                  Scenario("hi", 0.5, ScenarioMultipliers(fuel_cost = 2.0))]
            sm, sv, _, _ = create_stochastic_master_problem(StochasticMasterInput(inp, sc, true))
            ESFEX.set_silent(sm)
            optimize!(sm)
            @test string(termination_status(sm)) == "OPTIMAL"
            for k in 1:2, y in 1:3
                e, g, _ = sm.ext[:master_re][(k, 1, y)]
                @test value(e) / value(g) <= cap[y] + 1e-6
            end
            @test [value(sv.re_energy_ratio[(1, y)]) for y in 1:3] ≈ cap atol = 1e-6
            # A myopic year measures the step from the share the year before
            # achieved (re_share_anchor), not from the initial share
            minp = g20(years = [2027], base_demand = fill(50.0, 8760, 1),
                       re_share_anchor = [0.30],
                       generators = [mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4],
                                           ones(8760, 1); nb = 1, H = 8760)],
                       technologies = [ESFEX.with_fields(pv; availability = ones(8760, 1))])
            _, _, mres = solve_g20(minp)
            @test mres.re_penetration_by_year[1] ≈ 0.40 atol = 1e-6
            # The anchor is clamped to [0, 1]
            @test ESFEX.re_share_anchor(ESFEX.with_fields(minp; re_share_anchor = [1.07]), 1) == 1.0
            @test ESFEX.re_share_anchor(inp, 1) ≈ 0.05

            # Existing renewables above the cap (30 MW = 60 % of demand,
            # curtailment limit 5 %): the excess is curtailed beyond the
            # limit (it does not count toward the share) and pays
            # curtailment_cost, not the cap slack at VOLL (round 24: 8.8 G$
            # a year in the Cuba myopic run). No PV is built; the share
            # follows the cap.
            xinp = g20(existing_pv = 30.0)
            xm, xv, xres = solve_g20(xinp)
            @test all(sum(xres.tech_investment[y][1]) < 1e-6 for y in 1:3)
            @test xres.re_penetration_by_year ≈ cap atol = 1e-6
            @test all(value(x) < 1e-6 for x in values(xm.ext[:re_cap_slack]))
            @test ESFEX.diagnose_infeasibility(xm, xv, xinp)["total_re_cap_slack"] < 1e-6
            θ, fl = re_cap_exemption(xinp, calculate_target_ratios(xinp), 1,
                                     [(1, 24, 365.0)], nothing)
            @test θ[1] ≈ 1 - 1.05 * 0.15 / 0.6
            @test fl[1] ≈ 0.15
            # The curtailed MWh (30 MW less the cap's share of 50 MW) pay
            # curtailment_cost, discounted: the objective moves by exactly that
            x0m, _, _ = solve_g20(g20(existing_pv = 30.0, curtailment_cost = 0.0))
            curtailed = [(30.0 - c * 50.0) * 8760 for c in cap]
            df = [1.05^-(y - 1) for y in 1:3]
            @test objective_value(xm) - objective_value(x0m) ≈
                  2e-5 * sum(df .* curtailed) rtol = 1e-6
            xbr = run_benders_decomposition(xinp; max_iterations = 60, tolerance = 1e-7,
                                            verbose_benders = false)
            @test xbr.objective ≈ objective_value(xm) rtol = 1e-6
            @test xbr.solution.re_penetration_by_year ≈ cap atol = 1e-4
            # TSAM periods, stochastic scenarios and a myopic year too
            xtm, _, xtres = solve_g20(g20(existing_pv = 30.0, use_tsam = true,
                tsam_period_start_hours = [[1], [8761], [17521]],
                tsam_period_weights = [[365.0], [365.0], [365.0]]))
            @test xtres.re_penetration_by_year ≈ cap atol = 1e-6
            @test all(value(x) < 1e-6 for x in values(xtm.ext[:re_cap_slack]))
            xsm, xsv, _, _ = create_stochastic_master_problem(
                StochasticMasterInput(xinp, sc, true))
            ESFEX.set_silent(xsm)
            optimize!(xsm)
            @test string(termination_status(xsm)) == "OPTIMAL"
            @test [value(xsv.re_energy_ratio[(1, y)]) for y in 1:3] ≈ cap atol = 1e-6
            @test all(value(x) < 1e-6 for x in values(xsm.ext[:re_cap_slack]))
            xmy = g20(years = [2027], base_demand = fill(50.0, 8760, 1),
                      re_share_anchor = [0.30],
                      generators = [mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4],
                                          ones(8760, 1); nb = 1, H = 8760),
                                    mkgen("PVold", "Renewable", "Sun", [30.0], [0.0],
                                          ones(8760, 1); nb = 1, H = 8760)],
                      technologies = [ESFEX.with_fields(pv; availability = ones(8760, 1))])
            xmm, _, xmres = solve_g20(xmy)
            @test xmres.re_penetration_by_year[1] ≈ 0.40 atol = 1e-6
            @test value(xmm.ext[:re_cap_slack][(0, 1, 1)]) < 1e-6
            # Building above the cap still does not pay: with only 5 MW of
            # existing PV (10 %), the master builds just up to the cap
            # (curtailing new PV is held to the 5 % limit)
            _, _, lres = solve_g20(g20(existing_pv = 5.0))
            @test lres.re_penetration_by_year ≈ cap atol = 1e-6
            @test sum(lres.tech_investment[1][1]) <= (0.15 * 50 - 5.0) * 1.05 + 1e-6

            # The annual investment budget binds in every master: 4 M$ a
            # year buys 4 MW of PV (no RE cap)
            binp = g20(max_re_increment = 1.0, max_annual_investment = 4.0)
            bm, bv, bres = solve_g20(binp)
            @test bres.total_investment_by_year ≈ fill(4.0, 3) rtol = 1e-6
            @test all(value(bv.slack_budget[y]) < 1e-6 for y in 1:3)
            bbr = run_benders_decomposition(binp; max_iterations = 60, tolerance = 1e-7,
                                            verbose_benders = false)
            @test all(bbr.solution.total_investment_by_year .<= 4.0 + 1e-4)
            @test bbr.objective ≈ objective_value(bm) rtol = 1e-6
            bsm, bsv, _, _ = create_stochastic_master_problem(StochasticMasterInput(binp, sc, true))
            ESFEX.set_silent(bsm)
            optimize!(bsm)
            @test extract_master_solution(bsm, bsv, binp).total_investment_by_year ≈
                  fill(4.0, 3) rtol = 1e-6

            # budget_scope total (round 41): the year's operating cost counts
            # against the same budget. Gas at 200 $/MWh: a MW of PV (1 M$)
            # saves 1.75 M$ of fuel in its first year, so building lowers the
            # year's spending. Investment scope: 40 M$ buys 40 MW in 2025, the
            # other 10 MW in 2026. Total scope: 50 MW at once (50 M$ and no
            # fuel, the least the year can spend), 10 M$ over the budget. No
            # static reserve here (no unit is reservable); with it, below,
            # its shortfall stays out of the budget row.
            gas200 = mkgen("Gas", "Non-renewable", "Gas", [100.0], [2.0e-4],
                           ones(Hy, 1); nb = 1, H = Hy)
            tb(total; kw...) = g20(max_re_increment = 1.0, max_annual_investment = 40.0,
                                   budget_total = total, generators = [gas200],
                                   reserve_static_default_ratio = 0.0; kw...)
            ibm, ibv, ibres = solve_g20(tb(false))
            @test ibres.total_investment_by_year ≈ [40.0, 10.0, 0.0] atol = 1e-6
            @test all(value(ibv.slack_budget[y]) < 1e-6 for y in 1:3)
            tbm, tbv, tbres = solve_g20(tb(true))
            @test tbres.total_investment_by_year ≈ [50.0, 0.0, 0.0] atol = 1e-6
            @test value(tbv.slack_budget[1]) ≈ 10.0 atol = 1e-6
            op = ESFEX.budget_operating_cost(tbm, tbv, tb(true))
            @test value(op[1]) ≈ 0.0 atol = 1e-6
            @test objective_value(tbm) > objective_value(ibm)
            # The budget row holds the year's operating cost as the objective
            # values it (2025 is undiscounted)
            gv = first(v for v in all_variables(tbm) if startswith(name(v), "op_y1_d1_gen"))
            @test normalized_coefficient(constraint_by_name(tbm, "budget_y1"), gv) ≈
                  coefficient(objective_function(tbm), gv) rtol = 1e-9
            @test coefficient(objective_function(tbm), gv) > 0
            # Only money in the budget row: with the static reserve on (no
            # unit here is reservable, so it is short all year) and a CO2
            # price, the reserve shortfall and the carbon price are in the
            # objective but not in the row; the fuel is
            pinp = tb(true; reserve_static_default_ratio = 0.15, co2_cost = 1.0e-4,
                      fuel_co2 = Dict("Gas" => 0.2))
            pm, _, _ = solve_g20(pinp)
            row = constraint_by_name(pm, "budget_y1")
            rl = first(v for v in all_variables(pm) if startswith(name(v), "op_y1_d1_rs_loss"))
            @test value(rl) > 0
            @test coefficient(objective_function(pm), rl) > 0
            @test normalized_coefficient(row, rl) == 0.0
            pg = first(v for v in all_variables(pm) if startswith(name(v), "op_y1_d1_gen"))
            @test normalized_coefficient(row, pg) > 0
            @test normalized_coefficient(row, pg) < coefficient(objective_function(pm), pg)
            @test normalized_coefficient(row, pg) ≈
                  coefficient(objective_function(tbm), gv) rtol = 1e-9   # fuel only
            # TSAM periods and stochastic scenarios too
            _, _, tbtres = solve_g20(tb(true; use_tsam = true,
                tsam_period_start_hours = [[1], [8761], [17521]],
                tsam_period_weights = [[365.0], [365.0], [365.0]]))
            @test tbtres.total_investment_by_year ≈ [50.0, 0.0, 0.0] atol = 1e-6
            tsm, tsv, _, _ = create_stochastic_master_problem(StochasticMasterInput(tb(true), sc, true))
            ESFEX.set_silent(tsm)
            optimize!(tsm)
            @test extract_master_solution(tsm, tsv, tb(true)).total_investment_by_year ≈
                  [50.0, 0.0, 0.0] atol = 1e-6
            @test count(c -> startswith(name(c), "budget_y"),
                        all_constraints(tsm; include_variable_in_set_constraints = false)) == 3
        end

        @testset "master: conditioning and capacity noise (round 36)" begin
            # The RE ratio was defined by a row `demand × ratio == energy`:
            # the year's demand (MWh) as a coefficient next to the day
            # weights. Cuba: 2.2e7, the widest range of every master.
            years = [2025, 2026]
            Hy = length(years) * 8760
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4],
                        ones(Hy, 1); nb = 1, H = Hy)
            o = [0.0]
            pv = TechnologyConfig("PV", "Renewable", "Sun", [1.0], [500.0], ones(Hy, 1),
                                  [1.0], [1.0], [1.0], [1.0], o, o, o, o, o, o, o, o,
                                  [25.0], o, o, 50.0, "AC", false, [1.0])
            inp = MasterProblemInput(; years = years, network = net, generators = [gas],
                batteries = BatteryConfig[], technologies = [pv],
                base_demand = fill(50.0, Hy, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.05,
                min_re_increment = 0.0, max_re_increment = 0.10,
                representative_days_per_year = 1, min_day_separation = 1,
                loss_of_load_penalty = 0.01, verbose = false)
            function coef_range(m)
                lo, hi = Inf, 0.0
                for (F, S) in list_of_constraint_types(m)
                    F <: AffExpr || continue
                    for c in all_constraints(m, F, S), (_, a) in constraint_object(c).func.terms
                        a == 0 && continue
                        lo, hi = min(lo, abs(a)), max(hi, abs(a))
                    end
                end
                return lo, hi
            end
            m, v, _ = create_master_problem(inp; use_representative_days = true)
            lo, hi = coef_range(m)
            # 50 MW all year is 438 000 MWh: that coefficient is gone
            @test hi < 1.0e3
            @test !any(startswith(name(x), "re_ratio") for x in all_variables(m))
            ESFEX.set_silent(m)
            optimize!(m)
            res = extract_master_solution(m, v, inp)
            @test [value(v.re_energy_ratio[(1, y)]) for y in 1:2] ≈
                  res.re_penetration_by_year atol = 1e-6
            sm, sv, _, _ = create_stochastic_master_problem(StochasticMasterInput(inp,
                [Scenario("a", 0.5, ScenarioMultipliers()),
                 Scenario("b", 0.5, ScenarioMultipliers())], true))
            @test coef_range(sm)[2] < 1.0e3

            # A barrier solve without crossover leaves capacity it did not
            # mean to build (1e-6 MW at a bus); carried into the next myopic
            # year or the dispatch it made rows like `gen ≤ 2e-9`. The
            # master reports at most CAPACITY_TOL as 0.
            @test ESFEX.CAPACITY_TOL == 1.0e-3
            nm, nv, _ = create_master_problem(inp; use_representative_days = true)
            fix(nv.tech_investment[1][1][1], 5.0e-4; force = true)
            ESFEX.set_silent(nm)
            optimize!(nm)
            nres = extract_master_solution(nm, nv, inp)
            @test value(nv.tech_investment[1][1][1]) ≈ 5.0e-4
            @test nres.tech_investment[1][1][1] == 0.0
            @test nres.cumulative_tech_capacity[1][1][1] == 0.0
            fix(nv.tech_investment[1][1][1], 5.0e-2; force = true)
            optimize!(nm)
            @test extract_master_solution(nm, nv, inp).tech_investment[1][1][1] ≈ 5.0e-2
        end

        @testset "master: one more solve after NUMERICAL_ERROR (round 36)" begin
            # Cuba myopic TSAM 2027: Gurobi's barrier ended NUMERICAL_ERROR
            # and the run stopped. The master is solved once more with the
            # solver's retry parameters, which are set back afterwards.
            MOIU = ESFEX.MOI.Utilities
            mock = MOIU.MockOptimizer(MOIU.UniversalFallback(MOIU.Model{Float64}()))
            m = ESFEX.JuMP.direct_model(mock)
            x = ESFEX.JuMP.@variable(m, lower_bound = 0)
            ESFEX.JuMP.@objective(m, Min, x)
            P = ESFEX.MOI.RawOptimizerAttribute("P")
            ESFEX.JuMP.set_attribute(m, "P", 0)
            during = Ref{Any}(nothing)
            fail(mk) = MOIU.mock_optimize!(mk, ESFEX.MOI.NUMERICAL_ERROR)
            ok(mk) = (during[] = ESFEX.MOI.get(mk, P);
                      MOIU.mock_optimize!(mk, ESFEX.MOI.OPTIMAL,
                                          (ESFEX.MOI.FEASIBLE_POINT, [0.0])))
            MOIU.set_mock_optimize!(mock, fail, ok)
            ESFEX.optimize_master!(m; retry = ["P" => 1])
            @test termination_status(m) == ESFEX.MOI.OPTIMAL
            @test during[] == 1
            @test ESFEX.JuMP.get_attribute(m, "P") == 0
            @test value(x) == 0.0
            # Gurobi's SUBOPTIMAL (LOCALLY_SOLVED: the barrier stalled) is
            # retried too (round 37)
            stalled(mk) = MOIU.mock_optimize!(mk, ESFEX.MOI.LOCALLY_SOLVED,
                                              (ESFEX.MOI.FEASIBLE_POINT, [1.0]))
            during[] = nothing
            MOIU.set_mock_optimize!(mock, stalled, ok)
            ESFEX.optimize_master!(m; retry = ["P" => 1])
            @test termination_status(m) == ESFEX.MOI.OPTIMAL
            @test during[] == 1
            @test ESFEX.JuMP.get_attribute(m, "P") == 0
            # No retry parameters (HiGHS, or none given): the status stands
            MOIU.set_mock_optimize!(mock, fail)
            ESFEX.optimize_master!(m; retry = nothing)
            @test termination_status(m) == ESFEX.MOI.NUMERICAL_ERROR
            @test ESFEX.NUMERICAL_RETRY_PARAMS["Gurobi"] == ["BarHomogeneous" => 1]
            @test !haskey(ESFEX.NUMERICAL_RETRY_PARAMS, "HiGHS")
        end

        @testset "master: representative days chosen once for every year" begin
            # Year 1 peaks on day 10, year 2 on day 200. Each year picked its
            # own peak days, so the weather sample, and the share existing
            # renewables give, changed from year to year without any
            # capacity change (Cuba myopic: 49 → 59 → 54 → 67 %). The days
            # now come from the horizon-mean demand: day 200 in both years,
            # each with its own demand.
            H2 = 2 * 8760
            D = fill(50.0, H2, 1)
            D[9 * 24 .+ (1:24), 1] .= 80.0
            D[8760 + 199 * 24 .+ (1:24), 1] .= 90.0
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [150.0], [1.0e-4],
                        ones(H2, 1); nb = 1, H = H2)
            inp = MasterProblemInput(years = [2025, 2026], network = net,
                generators = [gas], batteries = BatteryConfig[], base_demand = D,
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                loss_of_load_penalty = 10.0, verbose = false)
            d200 = 199 * 24
            @test master_rep_day_offsets(inp) == [d200]
            @test [master_rep_days(inp, y) for y in 1:2] == [[d200 + 1], [8760 + d200 + 1]]
            @test ESFEX.benders_rep_days(inp) == Dict(1 => [d200 + 1], 2 => [8760 + d200 + 1])
            # The monolithic master models those days: year 1's is a 50 MW
            # day, not its own 80 MW peak
            m, _, _ = create_master_problem(inp; use_representative_days = true)
            @test m.ext[:master_re][(0, 1, 1)][3] ≈ 50.0 * 8760
            @test m.ext[:master_re][(0, 1, 2)][3] ≈ 90.0 * 8760
            # A myopic year's one-year master takes the days chosen for the
            # horizon (rep_day_offsets), not its own year's peak (day 10)
            my = ESFEX.with_fields(inp; years = [2025], hours_per_year = [8760],
                                   base_demand = D[1:8760, :], rep_day_offsets = [d200])
            @test master_rep_days(my, 1) == [d200 + 1]
            mm, _, _ = create_master_problem(my; use_representative_days = true)
            @test mm.ext[:master_re][(0, 1, 1)][3] ≈ 50.0 * 8760
            @test representative_day_offsets(D, 1, 1, 1, 24, 8760) == [9 * 24]
            # Demand that does not reach a year gives it no days, as before
            @test master_rep_days(ESFEX.with_fields(inp; base_demand = D[1:8760, :]), 2) == Int[]
        end

        @testset "master: representative days match the year's demand and RE energy" begin
            # The high days are both the high-demand and the sunny/windy
            # days. The peak-based choice (peak day + highest peak of each
            # segment) gave the master, which weighs each day as 365/n days,
            # a year with far more demand, sun and wind than the real one
            # (Cuba: PV ×1.14, wind ×1.45, demand ×1.06).
            a = [((d * 97) % 365) / 364 for d in 0:364]
            shape = [1.0 + 0.2 * sin(2π * (h - 8) / 24) for h in 0:23]
            sun = [max(sin(π * (h - 6) / 12), 0.0) for h in 0:23]
            total = reduce(vcat, [100.0 * (1.0 + x) .* shape for x in a])
            D = hcat(0.6 .* total, 0.4 .* total)
            pv = reduce(vcat, [(0.5 + x) * 0.4 .* sun for x in a])
            wind = repeat(0.2 .+ 0.6 .* a; inner = 24)
            ratio(s, offs) = sum(sum(s[o .+ (1:24)]) for o in offs) / (24 * length(offs)) /
                             (sum(s) / length(s))

            old = select_representative_days(D, 1, 5, 5, 24, 8760) .- 1
            @test ratio(pv, old) > 1.3 && ratio(wind, old) > 1.4 && ratio(total, old) > 1.2

            offs = representative_day_offsets(D, 1, 5, 5, 24, 8760; profiles = [pv, wind])
            @test length(offs) == 5
            @test offs[1] == old[1]                       # the peak day stays
            days = sort(offs .÷ 24)
            @test minimum(diff(days)) >= 5
            @test all(abs(ratio(s, offs) - 1) < 0.03 for s in (total, pv, wind))
            @test representative_day_ratios(D, 1, offs, 24, 8760; profiles = [pv, wind]) ≈
                  [ratio(s, offs) for s in (total, pv, wind)]
            # Equal features on every day: the peak-based choice
            @test representative_day_offsets(fill(1.0, 8760, 1), 1, 3, 1, 24, 8760) ==
                  [0, 121 * 24, 242 * 24]

            # Julia fallback (no rep_day_offsets): the master's renewable
            # generators and candidate technologies give the profiles
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            solar = mkgen("Solar", "Renewable", "Sun", [50.0], [0.0],
                          reshape(pv, :, 1); nb = 1, H = 8760)
            gas = mkgen("Gas", "Non-renewable", "Gas", [150.0], [1.0e-4],
                        reshape(wind, :, 1); nb = 1, H = 8760)
            inp = MasterProblemInput(years = [2025], network = net,
                generators = [solar, gas], batteries = BatteryConfig[],
                base_demand = D[:, 1:1] .+ D[:, 2:2],
                representative_days_per_year = 5, min_day_separation = 5,
                verbose = false)
            @test ESFEX.master_re_profiles(inp) == [pv]
            @test master_rep_day_offsets(inp) ==
                  representative_day_offsets(inp.base_demand, 1, 5, 5, 24, 8760;
                                             profiles = [pv])
            @test abs(ratio(pv, master_rep_day_offsets(inp)) - 1) < 0.03
        end

        @testset "master: critical days of lowest renewable supply (round 33)" begin
            # Days matched to the year's mean energy never include a dark,
            # windless day, on which the dispatch shed load (Cuba myopic,
            # Oct-Dec nights) while every master saw no shortage. Each master
            # now also models the days with the largest net load, one day of
            # the year each, the representative days reweighted so the year
            # keeps their demand and renewable energy.
            a = [((d * 97) % 365) / 364 for d in 0:364]
            shape = [1.0 + 0.2 * sin(2π * (h - 8) / 24) for h in 0:23]
            sun = [max(sin(π * (h - 6) / 12), 0.0) for h in 0:23]    # dark 18-6 h
            dark = 291                                                 # 1-based day
            level = 0.6 .+ 0.4 .* a; level[dark] = 0.1
            wlevel = 0.3 .+ 0.4 .* a; wlevel[dark] = 0.02
            total = reduce(vcat, [100.0 * (1.0 + 0.3x) .* shape for x in a])
            D = reshape(total, :, 1)
            pv = reduce(vcat, [0.8l .* sun for l in level])
            wind = repeat(wlevel; inner = 24)

            crit, hours = critical_day_plan(D, 1, 2, 5, 24, 8760;
                profiles = [pv, wind], capacities = [100.0, 50.0])
            @test crit[1] == (dark - 1) * 24 && length(crit) == 2
            @test abs(crit[2] - crit[1]) >= 5 * 24
            @test hours == 13.0                   # the night, 18 h to 6 h
            @test critical_day_plan(D, 1, 1, 5, 24, 8760; profiles = [pv, wind],
                capacities = [100.0, 50.0], exclude = [(dark - 1) * 24])[1] != [(dark - 1) * 24]
            @test critical_day_plan(D, 1, 1, 5, 24, 8760; profiles = [wind])[2] == 24.0
            @test critical_day_plan(D, 1, 1, 5, 24, 8760)[2] == ESFEX.DEFAULT_STORAGE_FIRM_HOURS

            rep = representative_day_offsets(D, 1, 5, 5, 24, 8760; profiles = [pv, wind])
            w = representative_day_weights(D, 1, rep, crit, 24, 8760; profiles = [pv, wind])
            @test sum(w) ≈ 365 && w[6:7] == [1.0, 1.0]
            alone = representative_day_ratios(D, 1, rep, 24, 8760; profiles = [pv, wind])
            both = representative_day_ratios(D, 1, vcat(rep, crit), 24, 8760;
                                             profiles = [pv, wind], weights = w)
            naive = representative_day_ratios(D, 1, vcat(rep, crit), 24, 8760;
                profiles = [pv, wind], weights = vcat(fill(363 / 5, 5), [1.0, 1.0]))
            @test maximum(abs.(both .- alone)) < 2e-3
            @test naive[2] < alone[2] - 1e-3      # equal shares: a darker year
            @test representative_day_weights(D, 1, rep, Int[], 24, 8760) == fill(73.0, 5)

            # The master models them (Julia picks the days itself here)
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            solar = mkgen("Solar", "Renewable", "Sun", [100.0], [0.0],
                          reshape(pv, :, 1); nb = 1, H = 8760)
            gas = mkgen("Gas", "Non-renewable", "Gas", [200.0], [1.0e-4],
                        ones(8760, 1); nb = 1, H = 8760)
            mk(n) = MasterProblemInput(years = [2025], network = net,
                generators = [solar, gas], batteries = BatteryConfig[], base_demand = D,
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 5, min_day_separation = 5,
                critical_days = n, verbose = false)
            offs, wts, nrep = master_day_plan(mk(1))
            @test nrep == 5 && length(offs) == 6 && offs[6] == (dark - 1) * 24
            @test sum(wts) ≈ 365 && wts[6] == 1.0
            @test master_day_plan(mk(0)) == (master_rep_day_offsets(mk(0)), fill(73.0, 5), 5)
            m0, _, _ = create_master_problem(mk(0); use_representative_days = true)
            m1, _, _ = create_master_problem(mk(1); use_representative_days = true)
            @test length(m1.ext[:master_days]) == 6
            @test sort([d.scale for d in m1.ext[:master_days]])[1] == 1.0   # Δt × 1 day
            # The year's demand energy is kept
            @test m1.ext[:master_re][(0, 1, 1)][3] ≈ m0.ext[:master_re][(0, 1, 1)][3] rtol = 2e-3
            # Given by the runner: its days and weights are used
            given = ESFEX.with_fields(mk(0); rep_day_offsets = rep, critical_day_offsets = crit,
                                      rep_day_weights = w)
            @test master_day_plan(given) == (vcat(rep, crit), w, 5)
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
                [0.0], [0.0], [0.0], [0.0], [0.0], ones(H, 1), true,
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
                [0.0], [0.0], [0.0], [0.0], [0.0], ones(H, 1), true,
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
                    [0.0], [0.0], [0.0], [0.0], [0.0], ones(H, 1), true,
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
                [0.0], [0.0], [0.0], [0.0], [0.0], ones(H, 1), true,
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
                [0.0, 0.0], [0.0, 0.0], ones(H, 2), true,
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
                                [0.5, 0.5], [0.0, 0.0], [0.0, 0.0],
                                [0.0, 0.0], [10.0, 10.0], [0.0, 0.0],
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

        @testset "parallel lines: pair flow sums every line" begin
            # Two identical 60 MW lines carry 100 MW from bus 1 to bus 2. The
            # pair flow (results, custom constraints) kept only the first
            # line's 50 MW.
            H = 1; N = 2
            gas = mkgen("Gas", "Non-renewable", "Gas", [200.0, 0.0], [3.0e-5, 3.0e-5],
                        ones(H, N); nb = N, H = H)
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
            mkline(id, i, j) = TransmissionLineData(id, i, j, 60.0, 0.1, 0.0, 0.0,
                                                    100.0, 220.0, 1, 50.0, "AC")
            # The second line is entered 2 → 1: still the same pair
            net = NetworkConfig(N, N, buses, [1, 2],
                                [0.0 120.0; 120.0 0.0], [0.0 100.0; 100.0 0.0],
                                100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                                [mkline("La", 1, 2), mkline("Lb", 2, 1)], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            mkinput() = PowerSystemInput(
                name = "par", year = 2025, network = net, generators = [gas],
                batteries = BatteryConfig[], demand = [0.0 100.0],
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                mode = "economic_dispatch", solver_name = "highs", verbose = false,
                pwl_loss_segments = 0)
            inp = mkinput()
            model, vars = create_power_system(inp)
            optimize!(model)
            res = extract_solution(model, vars, inp)
            @test res.load_shed_total ≈ 0.0 atol = 1e-6
            @test collect(keys(res.power_flow)) == [(1, 2)]
            @test res.power_flow[(1, 2)][1] ≈ 100.0 atol = 1e-6
            @test sum(f[1] for f in res.power_flow_by_line) ≈ 100.0 atol = 1e-6

            # A custom cap on the pair binds on both lines together; the pair
            # may be named in either direction (2 → 1 flow = -100 MW).
            for (idx, sense, rhs) in (([1, 2, 1], "<=", 70.0), ([2, 1, 1], ">=", -70.0))
                cc = [Dict("name" => "cap_pair", "type" => "linear", "sense" => sense,
                           "rhs" => rhs,
                           "terms" => [Dict("variable" => "power_flow",
                                            "index" => idx, "coefficient" => 1.0)])]
                inp = mkinput()
                model, vars = create_power_system(inp; custom_constraints = cc)
                optimize!(model)
                res = extract_solution(model, vars, inp)
                @test res.power_flow[(1, 2)][1] ≈ 70.0 atol = 1e-6
                @test res.load_shed_total ≈ 30.0 atol = 1e-6
            end
            @test_throws ErrorException create_power_system(mkinput(); custom_constraints = [
                Dict("name" => "x", "type" => "linear", "sense" => "<=", "rhs" => 1.0,
                     "terms" => [Dict("variable" => "power_flow", "index" => [1, 1, 1])])])
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
                # Line losses in the results: none without a loss model
                @test (res.line_losses === nothing) == (segments == 0)
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

        @testset "lines lose no more than the PWL loss of their flow" begin
            # 130 MW of PV at bus 1, 100 MW of load at bus 2, curtailment at
            # $20/MWh. The LP ran both flow directions (and the steep segments)
            # at once to "lose" the surplus on the line for free: 30 MW of
            # losses on a ~103 MW flow, no curtailment. Losses must be the PWL
            # loss of the net flow and the surplus curtailed.
            function sink_run(segments; pv = 130.0, H = 2)
                N = 2
                pvg = mkgen("PV", "Renewable", "Solar", [pv, 0.0], [0.0, 0.0],
                            ones(H, N); nb = N, H = H)
                gas = mkgen("Gas", "Non-renewable", "Gas", [0.0, 200.0],
                            [3.0e-5, 3.0e-5], ones(H, N); nb = N, H = H)
                buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                         BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
                line = TransmissionLineData("L12", 1, 2, 200.0, 0.1, 0.05, 0.0,
                                            100.0, 220.0, 1, 50.0, "AC")
                net = NetworkConfig(N, N, buses, [1, 2],
                                    [0.0 200.0; 200.0 0.0], [0.0 100.0; 100.0 0.0],
                                    100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                                    [line], TransformerData[], ACDCConverterData[],
                                    FrequencyConverterData[], 0.1)
                input = PowerSystemInput(
                    name = "sink", year = 2025, network = net, generators = [pvg, gas],
                    batteries = BatteryConfig[], demand = repeat([0.0 100.0], H),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    mode = "economic_dispatch", solver_name = "highs", verbose = false,
                    pwl_loss_segments = segments, curtailment_cost = 2e-5)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                res = extract_solution(model, vars, input)
                pwl = ESFEX.with_loss_model(ESFEX.TransmissionDC(net), segments,
                                            100.0).pwl_losses
                for t in 1:H
                    f = abs(value(vars.power_flow_by_line[1][t]))
                    # PWL loss of |f|, segments filled in order
                    rem, exact = f, 0.0
                    for (w, m) in zip(pwl.segment_widths[1], pwl.slopes[1])
                        exact += m * min(rem, w); rem = max(rem - w, 0.0)
                    end
                    @test value(vars.line_losses[1][t]) ≈ exact rtol = 0.02 atol = 1e-3
                    # Results: half of the line's loss at each end bus
                    @test res.line_losses[1, t] ≈ value(vars.line_losses[1][t]) / 2
                    @test res.line_losses[2, t] ≈ res.line_losses[1, t]
                end
                @test res.total_losses ≈ sum(res.line_losses)
                # A linearised line-step carries its flow on one segment
                for b in model.ext[:pwl_loss_repair].blocks
                    b.dir == 0 && continue
                    seg = b.dir > 0 ? b.dp : b.dn
                    @test sum(value.(seg[2:end]); init = 0.0) ≈ 0.0 atol = 1e-9
                    @test value(b.ploss) ≈ b.alpha * abs(value(b.pf)) atol = 1e-6
                end
                return res, model, vars
            end
            for segments in (3, 2, -1)
                res, _, _ = sink_run(segments)
                @test res.load_shed_total ≈ 0.0 atol = 1e-6
                # ~103 MW sent for 100 MW served: losses ~3-6 MW per step,
                # the rest of the 130 MW curtailed
                @test res.total_generation < 2 * 107.0
                @test res.total_curtailment > 2 * 23.0
                @test res.total_generation + res.total_curtailment ≈ 2 * 130.0 atol = 1e-4
            end
            # Re-solving starts again from the PWL model, and repairs it again
            res, model, vars = sink_run(3)
            obj = objective_value(model)
            optimize!(model)
            @test objective_value(model) ≈ obj rtol = 1e-6
            @test value(vars.line_losses[1][1]) < 6.0
            # With no surplus energy has a value: the PWL model is exact as
            # built and nothing is repaired (marginal losses kept)
            res, model, vars = sink_run(3; pv = 90.0)
            @test res.total_curtailment ≈ 0.0 atol = 1e-6
            reg = model.ext[:pwl_loss_repair]
            @test all(b -> b.dir == 0, reg.blocks)

            # A repaired model is re-solved with the LP method of its first
            # solve: Gurobi and CPLEX ignore the previous basis (which they
            # would restart from with simplex, even under barrier) unless the
            # user chose simplex
            params(m) = name -> Dict("Method" => m, "CPXPARAM_LPMethod" => m)[name]
            @test ESFEX.lp_warm_start_off("Gurobi", params(-1)) == ("LPWarmStart" => 0)
            @test ESFEX.lp_warm_start_off("Gurobi", params(2)) == ("LPWarmStart" => 0)
            @test ESFEX.lp_warm_start_off("Gurobi", params(1)) === nothing
            @test ESFEX.lp_warm_start_off("CPLEX", params(4)) == ("CPXPARAM_Advance" => 0)
            @test ESFEX.lp_warm_start_off("CPLEX", params(2)) === nothing
            @test ESFEX.lp_warm_start_off("HiGHS", params(2)) === nothing
            # The parameter holds for the re-solves only, and the repaired
            # solution stays readable (HiGHS stands in for Gurobi)
            ESFEX.LP_WARM_START_PARAMS["HiGHS"] = ("presolve", "off", "solver", ("simplex",))
            try
                res, model, _ = sink_run(3)       # repaired: tests OPTIMAL
                @test any(b -> b.dir != 0, model.ext[:pwl_loss_repair].blocks)
                @test ESFEX.JuMP.get_attribute(model, "presolve") == "choose"
            finally
                delete!(ESFEX.LP_WARM_START_PARAMS, "HiGHS")
            end
            # A solve that is not optimal is returned as it is, unrepaired:
            # Cuba's master on HiGHS ended "no progress" after 12.8 h and its
            # repair started a re-solve as long from that point
            res, model, vars = sink_run(3)
            for (k, v) in (("presolve", "off"), ("solver", "simplex"),
                           ("simplex_iteration_limit", 1))
                ESFEX.JuMP.set_attribute(model, k, v)
            end
            @test_logs (:warn, r"not repaired, the solve ended ITERATION_LIMIT") match_mode = :any optimize!(model)
            @test string(termination_status(model)) == "ITERATION_LIMIT"
            @test all(b -> b.dir == 0, model.ext[:pwl_loss_repair].blocks)

            # The master builds the same loss model on each representative
            # day: there too a line loses only the PWL loss of its flow
            H = 48
            pvg = mkgen("PV", "Renewable", "Solar", [130.0, 0.0], [0.0, 0.0],
                        ones(H, 2); nb = 2, H = H)
            gas = mkgen("Gas", "Non-renewable", "Gas", [0.0, 200.0], [3.0e-5, 3.0e-5],
                        ones(H, 2); nb = 2, H = H)
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
            line = TransmissionLineData("L12", 1, 2, 200.0, 0.1, 0.05, 0.0,
                                        100.0, 220.0, 1, 50.0, "AC")
            net = NetworkConfig(2, 2, buses, [1, 2],
                                [0.0 200.0; 200.0 0.0], [0.0 100.0; 100.0 0.0],
                                100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                                [line], TransformerData[], ACDCConverterData[],
                                FrequencyConverterData[], 0.1)
            inp = MasterProblemInput(
                years = [2025], network = net, generators = [pvg, gas],
                batteries = BatteryConfig[], base_demand = repeat([0.0 100.0], H),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                max_curtailment_ratio = 1.0, curtailment_cost = 2e-5,
                transmission_loss_segments = 2, verbose = false)
            model, mv, _ = create_master_problem(inp; use_representative_days = true)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            reg = model.ext[:pwl_loss_repair]
            @test length(reg.blocks) == 24
            for b in reg.blocks
                exact = ESFEX.pwl_loss_value(b, abs(value(b.pf)))
                @test value(b.ploss) ≈ exact rtol = 0.02 atol = 1e-3
                @test value(b.ploss) < 10.0     # it lost 40 MW (both directions full)
            end
            # The line cannot be expanded (transference_invest_max 0), so its
            # loss segments are bounded both ways. Every line of a master
            # with a corridor variable got an open last segment: in Cuba's
            # (no line may be built) f_pos and f_neg ran to 10⁶ MW in the
            # barrier iterates and the solve ended NUMERICAL_ERROR (round 36)
            @test all(b -> !b.open_last &&
                           all(ESFEX.JuMP.has_upper_bound, vcat(b.dp, b.dn)), reg.blocks)
            w = ESFEX.with_loss_model(ESFEX.TransmissionDC(net), 2, 100.0).pwl_losses.segment_widths[1]
            xinp = ESFEX.with_fields(inp; network = ESFEX.with_fields(net;
                transference_invest_max = [50.0, 50.0]))
            xm, xv, _ = create_master_problem(xinp; use_representative_days = true)
            @test ESFEX.transfer_expansion_max(xv, 1, 2, 1) == 50.0
            xb = xm.ext[:pwl_loss_repair].blocks
            @test length(xb) == 24
            # An expandable line's last segment spans the MW it can gain
            @test all(b -> !b.open_last && b.widths[end] ≈ w[end] + 50.0 &&
                           ESFEX.JuMP.upper_bound(b.dn[end]) ≈ w[end] + 50.0, xb)
            optimize!(xm)
            @test string(termination_status(xm)) == "OPTIMAL"
            # So Benders' operating bound lets the system draw beyond its
            # load at most the line's loss at full flow in one direction
            # (C28; it allowed both), on a line the master cannot expand
            @test ESFEX.benders_power_sink(mv, inp) == [Inf]
            fixed = ESFEX.with_fields(inp; network = ESFEX.with_fields(net; connections = zeros(2, 2)))
            _, fv, _ = create_master_problem(fixed; use_representative_days = false)
            full = ESFEX.with_loss_model(ESFEX.TransmissionDC(net), 2, 100.0).pwl_losses
            @test ESFEX.benders_power_sink(fv, fixed) ≈
                  [sum(full.slopes[1] .* full.segment_widths[1])]
        end

        @testset "loss repair: a stalled re-solve is retried or undone (round 37)" begin
            # Cuba myopic 2037: the third repair round's barrier stalled
            # (Gurobi SUBOPTIMAL = LOCALLY_SOLVED) on a model that differed
            # from the optimal round 2 by 50 loss coefficients. The repair
            # read 85 "over-losing" steps off that point, the last round
            # stalled too, and the master (which takes OPTIMAL only) stopped
            # the run. A mock that solves with HiGHS stands in for Gurobi and
            # reports the status `status_of(model)` gives.
            MOI = ESFEX.MOI
            MOIU = MOI.Utilities
            BH = MOI.RawOptimizerAttribute("BarHomogeneous")
            function sink_model(status_of)
                H, N = 2, 2
                pvg = mkgen("PV", "Renewable", "Solar", [130.0, 0.0], [0.0, 0.0],
                            ones(H, N); nb = N, H = H)
                gas = mkgen("Gas", "Non-renewable", "Gas", [0.0, 200.0],
                            [3.0e-5, 3.0e-5], ones(H, N); nb = N, H = H)
                buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                         BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
                line = TransmissionLineData("L12", 1, 2, 200.0, 0.1, 0.05, 0.0,
                                            100.0, 220.0, 1, 50.0, "AC")
                net = NetworkConfig(N, N, buses, [1, 2],
                                    [0.0 200.0; 200.0 0.0], [0.0 100.0; 100.0 0.0],
                                    100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                                    [line], TransformerData[], ACDCConverterData[],
                                    FrequencyConverterData[], 0.1)
                input = PowerSystemInput(
                    name = "sink", year = 2025, network = net, generators = [pvg, gas],
                    batteries = BatteryConfig[], demand = repeat([0.0 100.0], H),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    mode = "economic_dispatch", solver_name = "highs", verbose = false,
                    pwl_loss_segments = 3, curtailment_cost = 2e-5)
                model, vars = create_power_system(input)
                mock = MOIU.MockOptimizer(MOIU.UniversalFallback(MOIU.Model{Float64}()))
                solves = MOI.TerminationStatusCode[]
                mock.optimize! = function (mk)
                    h = ESFEX.HiGHS.Optimizer()
                    MOI.set(h, MOI.Silent(), true)
                    map = MOI.copy_to(h, mk)
                    MOI.optimize!(h)
                    xs = MOI.get(mk, MOI.ListOfVariableIndices())
                    st = status_of(model, mk)
                    push!(solves, st)
                    MOIU.mock_optimize!(mk, st, (MOI.FEASIBLE_POINT,
                        [MOI.get(h, MOI.VariablePrimal(), map[x]) for x in xs]))
                end
                ESFEX.JuMP.set_optimizer(model, () -> mock)
                ESFEX.JuMP.set_attribute(model, "BarHomogeneous", 0)
                return model, vars, solves
            end
            linearised(model) = any(b -> b.dir != 0, model.ext[:pwl_loss_repair].blocks)
            function exact_losses(model)
                for b in model.ext[:pwl_loss_repair].blocks
                    @test value(b.ploss) ≈ ESFEX.pwl_loss_value(b, abs(value(b.pf))) rtol = 0.02 atol = 1e-3
                end
            end

            # A repaired model stalls unless solved with BarHomogeneous = 1:
            # the re-solve is retried with it, the repair finishes optimal and
            # the parameter is set back
            model, _, solves = sink_model((m, mk) ->
                linearised(m) && MOI.get(mk, BH) != 1 ? MOI.LOCALLY_SOLVED : MOI.OPTIMAL)
            @test_logs (:warn, r"ended LOCALLY_SOLVED.*solving again with BarHomogeneous = 1") match_mode = :any ESFEX.optimize_master!(model; retry = ["BarHomogeneous" => 1])
            @test termination_status(model) == MOI.OPTIMAL
            @test linearised(model)
            @test MOI.LOCALLY_SOLVED in solves
            @test ESFEX.JuMP.get_attribute(model, "BarHomogeneous") == 0
            exact_losses(model)

            # It stalls whatever the parameters: the round is undone and the
            # model of the round before (here the PWL model) solved again
            model, vars, solves = sink_model((m, mk) ->
                linearised(m) ? MOI.LOCALLY_SOLVED : MOI.OPTIMAL)
            @test_logs (:warn, r"repair round 2 ended LOCALLY_SOLVED, not proven optimal; keeping the model of round 1") match_mode = :any ESFEX.optimize_master!(model; retry = ["BarHomogeneous" => 1])
            @test termination_status(model) == MOI.OPTIMAL
            @test !linearised(model)
            @test solves == [MOI.OPTIMAL, MOI.LOCALLY_SOLVED, MOI.LOCALLY_SOLVED, MOI.OPTIMAL]
            # Back to the model as built: segments and loss slopes
            for b in model.ext[:pwl_loss_repair].blocks
                @test all(k -> ESFEX.JuMP.upper_bound(b.dp[k]) == b.widths[k] &&
                               ESFEX.JuMP.upper_bound(b.dn[k]) == b.widths[k], 1:length(b.widths))
                @test ESFEX.JuMP.normalized_coefficient(b.con, b.dp[1]) == -b.slopes[1]
            end

            # A master without retry parameters (HiGHS) undoes a stalled
            # round at once
            model, _, solves = sink_model((m, mk) ->
                linearised(m) ? MOI.LOCALLY_SOLVED : MOI.OPTIMAL)
            ESFEX.optimize_master!(model; retry = nothing)
            @test termination_status(model) == MOI.OPTIMAL
            @test solves == [MOI.OPTIMAL, MOI.LOCALLY_SOLVED, MOI.OPTIMAL]
            # A plain optimize! (what the dispatch ran until round 39) keeps
            # the stalled round: no retry, no undo
            model, _, solves = sink_model((m, mk) ->
                linearised(m) ? MOI.LOCALLY_SOLVED : MOI.OPTIMAL)
            optimize!(model)
            @test termination_status(model) == MOI.LOCALLY_SOLVED
            @test linearised(model)
            @test ESFEX.JuMP.get_attribute(model, "BarHomogeneous") == 0

            # The first solve stalls: retried before any repair
            model, _, solves = sink_model((m, mk) ->
                MOI.get(mk, BH) != 1 ? MOI.LOCALLY_SOLVED : MOI.OPTIMAL)
            ESFEX.optimize_master!(model; retry = ["BarHomogeneous" => 1])
            @test termination_status(model) == MOI.OPTIMAL
            @test solves[1:2] == [MOI.LOCALLY_SOLVED, MOI.OPTIMAL]
            exact_losses(model)
            @test :solve_retry ∉ keys(model.ext)
        end

        @testset "dispatch: a stalled solve is retried, a stalled repair round undone (round 39)" begin
            # The dispatch accepted Gurobi's SUBOPTIMAL (LOCALLY_SOLVED) and
            # repaired its line losses from it; it now solves as the master
            # (round 37): one more solve with BarHomogeneous = 1, and a
            # repair round still short of optimal is undone. A HiGHS-backed
            # mock stands in for Gurobi, as in the master's testset.
            MOI = ESFEX.MOI
            MOIU = MOI.Utilities
            BH = MOI.RawOptimizerAttribute("BarHomogeneous")
            function lossy_window(status_of)
                H, N = 2, 2
                pvg = mkgen("PV", "Renewable", "Solar", [130.0, 0.0], [0.0, 0.0],
                            ones(H, N); nb = N, H = H)
                gas = mkgen("Gas", "Non-renewable", "Gas", [0.0, 200.0],
                            [3.0e-5, 3.0e-5], ones(H, N); nb = N, H = H)
                buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                         BusData(2, 2, 220.0, 50.0, "AC", "PQ", "load", 1.0)]
                line = TransmissionLineData("L12", 1, 2, 200.0, 0.1, 0.05, 0.0,
                                            100.0, 220.0, 1, 50.0, "AC")
                net = NetworkConfig(N, N, buses, [1, 2],
                                    [0.0 200.0; 200.0 0.0], [0.0 100.0; 100.0 0.0],
                                    100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                                    [line], TransformerData[], ACDCConverterData[],
                                    FrequencyConverterData[], 0.1)
                input = PowerSystemInput(
                    name = "window", year = 2025, network = net, generators = [pvg, gas],
                    batteries = BatteryConfig[], demand = repeat([0.0 100.0], H),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    mode = "economic_dispatch", solver_name = "highs", verbose = false,
                    pwl_loss_segments = 3, curtailment_cost = 2e-5)
                model, _ = create_power_system(input)
                mock = MOIU.MockOptimizer(MOIU.UniversalFallback(MOIU.Model{Float64}()))
                solves = MOI.TerminationStatusCode[]
                mock.optimize! = function (mk)
                    h = ESFEX.HiGHS.Optimizer()
                    MOI.set(h, MOI.Silent(), true)
                    map = MOI.copy_to(h, mk)
                    MOI.optimize!(h)
                    xs = MOI.get(mk, MOI.ListOfVariableIndices())
                    st = status_of(model, mk)
                    push!(solves, st)
                    MOIU.mock_optimize!(mk, st, (MOI.FEASIBLE_POINT,
                        [MOI.get(h, MOI.VariablePrimal(), map[x]) for x in xs]))
                end
                ESFEX.JuMP.set_optimizer(model, () -> mock)
                ESFEX.JuMP.set_attribute(model, "BarHomogeneous", 0)
                return model, solves
            end
            linearised(model) = any(b -> b.dir != 0, model.ext[:pwl_loss_repair].blocks)
            gurobi = ["BarHomogeneous" => 1]

            # A repair round stalls unless solved with BarHomogeneous = 1: retried
            model, solves = lossy_window((m, mk) ->
                linearised(m) && MOI.get(mk, BH) != 1 ? MOI.LOCALLY_SOLVED : MOI.OPTIMAL)
            @test_logs (:warn, r"^Dispatch window 3: the solve ended LOCALLY_SOLVED.*solving again with BarHomogeneous = 1") match_mode = :any ESFEX.optimize_dispatch!(model; retry = gurobi, label = "Dispatch window 3")
            @test termination_status(model) == MOI.OPTIMAL
            @test linearised(model)
            @test ESFEX.JuMP.get_attribute(model, "BarHomogeneous") == 0
            for b in model.ext[:pwl_loss_repair].blocks
                @test value(b.ploss) ≈ ESFEX.pwl_loss_value(b, abs(value(b.pf))) rtol = 0.02 atol = 1e-3
            end
            @test :solve_retry ∉ keys(model.ext) && :solve_label ∉ keys(model.ext)

            # It stalls whatever the parameters: the round is undone (plain
            # optimize! kept it, LOCALLY_SOLVED and linearised, above)
            model, solves = lossy_window((m, mk) ->
                linearised(m) ? MOI.LOCALLY_SOLVED : MOI.OPTIMAL)
            @test_logs (:warn, r"repair round 2 ended LOCALLY_SOLVED, not proven optimal; keeping the model of round 1") match_mode = :any ESFEX.optimize_dispatch!(model; retry = gurobi)
            @test termination_status(model) == MOI.OPTIMAL
            @test !linearised(model)
            @test solves == [MOI.OPTIMAL, MOI.LOCALLY_SOLVED, MOI.LOCALLY_SOLVED, MOI.OPTIMAL]
            # Without retry parameters (HiGHS) the stalled round is undone at once
            model, solves = lossy_window((m, mk) ->
                linearised(m) ? MOI.LOCALLY_SOLVED : MOI.OPTIMAL)
            ESFEX.optimize_dispatch!(model; retry = nothing)
            @test solves == [MOI.OPTIMAL, MOI.LOCALLY_SOLVED, MOI.OPTIMAL]
            @test termination_status(model) == MOI.OPTIMAL

            # The first solve stalls whatever the parameters: retried once,
            # then its feasible point is what the window has (the adapter
            # keeps it with a warning naming the window)
            model, solves = lossy_window((m, mk) -> MOI.LOCALLY_SOLVED)
            ESFEX.optimize_dispatch!(model; retry = gurobi)
            @test solves[1:2] == [MOI.LOCALLY_SOLVED, MOI.LOCALLY_SOLVED]
            @test termination_status(model) == MOI.LOCALLY_SOLVED
            @test ESFEX.JuMP.primal_status(model) == MOI.FEASIBLE_POINT

            # A window without line losses: one more solve, as the master
            mock = MOIU.MockOptimizer(MOIU.UniversalFallback(MOIU.Model{Float64}()))
            m = ESFEX.JuMP.direct_model(mock)
            x = ESFEX.JuMP.@variable(m, lower_bound = 0)
            ESFEX.JuMP.@objective(m, Min, x)
            ESFEX.JuMP.set_attribute(m, "BarHomogeneous", 0)
            stalled(mk) = MOIU.mock_optimize!(mk, MOI.LOCALLY_SOLVED, (MOI.FEASIBLE_POINT, [1.0]))
            ok(mk) = MOIU.mock_optimize!(mk, MOI.OPTIMAL, (MOI.FEASIBLE_POINT, [0.0]))
            MOIU.set_mock_optimize!(mock, stalled, ok)
            @test_logs (:warn, r"^Dispatch: the solve ended LOCALLY_SOLVED") match_mode = :any ESFEX.optimize_dispatch!(m; retry = gurobi)
            @test termination_status(m) == MOI.OPTIMAL
            @test value(x) == 0.0
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
                                [0.0], [1.0], [0.5], [0.0], [0.0], [0.0],
                                [10.0], [0.0], [0.0], 0.0, 100.0, [0.0],
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
            # The shortfall's penalty is in the breakdown (soc_violation_cost):
            # the total is the sum of the components
            cb = short.cost_breakdown
            short_mwh = 40.0 * 0.95 - short.bat_soc[1, 1, end]
            @test cb.soc_violation_cost ≈ short_mwh * 0.1 * 0.01 rtol = 1e-6
            @test sum(getfield(cb, f) for f in fieldnames(typeof(cb))
                      if f ∉ (:total, :pe_electrolyzer_cost, :v2g_compensation,
                               :operating_budget_excess)) ≈ cb.total rtol = 1e-9
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
                                [0.0], [1.0], [0.5], [0.0], [0.0], [0.0],
                                [30.0], [10.0], [0.0], 0.0, 100.0, [0.0],
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

        @testset "clustered unit commitment (num_units)" begin
            # Each generator at a bus was ONE on/off unit: a derated fleet
            # (availability < min_power) could never be committed, a 2-unit
            # plant could not run one unit, and a small isolated plant could
            # not follow a load below its whole-plant minimum.
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            # A thermal plant of `n` units; min_up / min_down in hours
            function cgen(name, rated, n, fcost, avail; minp = 0.0, start = 0.0,
                          up = 0.0, down = 0.0, reservable = true)
                z = zeros(1)
                H = size(avail, 1)
                gen = GeneratorConfig(
                    name, "Non-renewable", "Gas", [rated], [minp],
                    [0.5], [0.4], ones(1), ones(1), [up], [down], [start],
                    [fcost], z, z, z, z, avail, reservable,
                    [30.0], z, z, z, 50.0, "AC",
                    z, z, z, z, zeros(H, 1), z, z, z, z, false, z, z, z,
                )
                return ESFEX.with_fields(gen; num_units = [n])
            end
            function run_uc(gens, load; rs = 0.0, init = nothing)
                H = length(load)
                input = PowerSystemInput(
                    name = "cuc", year = 2025, network = net, generators = gens,
                    batteries = BatteryConfig[], demand = reshape(Float64.(load), H, 1),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    reserve_static_requirement = rs > 0 ? Dict(1 => rs) : Dict{Int,Float64}(),
                    reserve_static_default_ratio = 0.0, loss_of_reserve_static = 1.0,
                    loss_of_load_penalty = 100.0,
                    generator_initial_status = init === nothing ?
                        Dict{Int, Dict{Int, Float64}}() : init,
                    mode = "unit_commitment", solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                res = extract_solution(model, vars, input)
                return res, model, vars, input
            end
            out(res, g) = res.gen_output[g, 1, :]
            units(res, g) = round.(Int, res.gen_status[g, 1, :])

            # One unit (the default) keeps the binary model
            res, model, vars, _ = run_uc([cgen("A", 100.0, 1, 1.0e-5, ones(2, 1); minp = 0.5)],
                                         [60.0, 60.0])
            @test is_binary(vars.gen_status[1, 1, 1])

            # Derated fleet: 2 × 250 MW at 30 % availability (150 MW), min 40 %
            # of a unit (100 MW). As one 500 MW unit its 200 MW minimum is above
            # what is available, so it was never committed and the load shed.
            avail = fill(0.3, 2, 1)
            res, _, _, _ = run_uc([cgen("ST", 500.0, 1, 1.0e-5, avail; minp = 0.4)], [120.0, 120.0])
            @test res.load_shed_total ≈ 240.0 atol = 1e-6
            res, _, _, _ = run_uc([cgen("ST", 500.0, 2, 1.0e-5, avail; minp = 0.4)], [120.0, 120.0])
            @test res.load_shed_total ≈ 0.0 atol = 1e-6
            @test units(res, 1) == [1, 1]           # one unit: ceil(2 × 0.3) = 1
            @test out(res, 1) ≈ [120.0, 120.0] atol = 1e-6
            # Availability caps the committed units and the total output:
            # 1.2 units' worth lets two units run but never above 150 MW
            res, _, _, _ = run_uc([cgen("ST", 500.0, 4, 1.0e-5, avail; minp = 0.4)], [140.0, 160.0])
            @test all(units(res, 1) .<= 2)
            @test out(res, 1) ≈ [140.0, 150.0] atol = 1e-6
            @test res.load_shed_total ≈ 10.0 atol = 1e-6

            # A 2 × 250 MW plant (min 60 % of a unit) runs one unit for 250 MW
            res, _, _, _ = run_uc([cgen("CC", 500.0, 1, 1.0e-5, ones(2, 1); minp = 0.6)], [250.0, 250.0])
            @test res.load_shed_total ≈ 500.0 atol = 1e-6   # 300 MW minimum as one unit
            res, _, _, _ = run_uc([cgen("CC", 500.0, 2, 1.0e-5, ones(2, 1); minp = 0.6)], [250.0, 250.0])
            @test units(res, 1) == [1, 1]
            @test out(res, 1) ≈ [250.0, 250.0] atol = 1e-6
            @test res.load_shed_total ≈ 0.0 atol = 1e-6

            # An isolated 8 MW diesel plant of 4 units (min 50 %) follows a 2 MW
            # night load and a 7 MW day load
            load = [2.0, 2.0, 7.0, 7.0, 2.0]
            res, _, _, _ = run_uc([cgen("D", 8.0, 4, 1.0e-4, ones(5, 1); minp = 0.5)], load)
            @test res.load_shed_total ≈ 0.0 atol = 1e-6
            @test out(res, 1) ≈ load atol = 1e-6
            @test all(1 .<= units(res, 1) .<= 4)
            @test units(res, 1)[3] == 4             # 7 MW needs all four units

            # Start-up cost is per start of the whole plant: one of 2 units
            # costs half. The plant starts one unit, then a second.
            res, _, _, _ = run_uc([cgen("A", 100.0, 2, 1.0e-5, ones(2, 1); minp = 0.4, start = 1.0e-3)],
                                  [30.0, 80.0])
            @test units(res, 1) == [1, 2]
            @test res.gen_startup[1, 1, :] ≈ [1.0, 1.0] atol = 1e-6
            @test res.cost_breakdown.startup_cost ≈ 2 * 0.5e-3 atol = 1e-9

            # Minimum up time, per unit: without one, a second unit runs for
            # the 80 MW hour. With 3 h up both units (started at t1 and t2)
            # would still be online at t3, where their 50 MW minimum exceeds
            # the 30 MW load, so the dear peaker covers the peak instead.
            peaker(H) = cgen("P", 100.0, 1, 1.0e-4, ones(H, 1))
            load = [30.0, 80.0, 30.0, 30.0]
            res, _, _, _ = run_uc([cgen("A", 100.0, 2, 1.0e-5, ones(4, 1); minp = 0.5), peaker(4)], load)
            @test units(res, 1) == [1, 2, 1, 1]
            @test out(res, 2) ≈ zeros(4) atol = 1e-6
            res, _, _, _ = run_uc([cgen("A", 100.0, 2, 1.0e-5, ones(4, 1); minp = 0.5, up = 3.0),
                                   peaker(4)], load)
            @test units(res, 1) == [1, 1, 1, 1]
            @test out(res, 2) ≈ [0.0, 30.0, 0.0, 0.0] atol = 1e-6
            # Minimum down time, per unit: the unit shut down for the 30 MW hour
            # cannot restart the next hour
            load = [100.0, 30.0, 80.0, 80.0]
            init = Dict(1 => Dict(1 => 2.0))
            res, _, _, _ = run_uc([cgen("A", 100.0, 2, 1.0e-5, ones(4, 1); minp = 0.5, down = 2.0),
                                   peaker(4)], load; init = init)
            @test units(res, 1) == [2, 1, 1, 2]
            @test out(res, 2) ≈ [0.0, 0.0, 30.0, 0.0] atol = 1e-6

            # Spinning reserve: the units online hold rated / N each, within
            # the plant's availability. 40 MW of reserve on a 30 MW load:
            # two of 2 × 50 MW units online give 70 MW of headroom.
            res, model, vars, input = run_uc(
                [cgen("A", 100.0, 2, 1.0e-5, ones(2, 1); minp = 0.2, start = 1.0e-6)],
                [30.0, 30.0]; rs = 40.0)
            @test units(res, 1) == [2, 2]
            @test sum(value.(vars.reserve_static_loss)) ≈ 0.0 atol = 1e-6
            # At 60 % availability two units still run but share 60 MW: 30 MW free
            res, model, vars, input = run_uc(
                [cgen("A", 100.0, 2, 1.0e-5, fill(0.6, 2, 1); minp = 0.2, start = 1.0e-6)],
                [30.0, 30.0]; rs = 40.0)
            @test units(res, 1) == [2, 2]
            @test value.(vars.reserve_static_loss[1, :]) ≈ [10.0, 10.0] atol = 1e-6
            # Prices are recovered on the LP copy with the integer units fixed
            prices = ESFEX.recover_uc_duals_via_copy(model, vars, input)
            @test prices !== nothing && all(isfinite, prices)
        end

        @testset "variable renewables are not committed; biomass is" begin
            # Rule: a variable renewable (type Renewable with sun / wind /
            # ocean / no fuel, or water without a reservoir) has no
            # commitment and holds no reserve; any other unit is dispatchable.
            vr(type, fuel; res = 0.0) = ESFEX.is_variable_renewable(
                ESFEX.with_fields(mkgen("U", type, fuel, [10.0], [0.0], ones(1, 1); nb = 1, H = 1);
                                  reservoir_capacity = [res]), 1)
            @test all(vr("Renewable", f) for f in ("Sun", "Solar", "PV", "Wind", "Wind offshore",
                                                     "OTEC", "None", "", "Water", "Hydro", "sun"))
            @test !any(vr("Renewable", f) for f in ("Biomass", "Biogas", "Geothermal", "Waste"))
            @test !vr("Renewable", "Water"; res = 100.0)
            @test !vr("Non-renewable", "Gas") && !vr("Non-renewable", "Sun")

            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            H = 4
            unit(name, type, fuel, rated, fcost, avail; minp = 0.0, start = 0.0,
                 reservable = true) = ESFEX.with_fields(
                mkgen(name, type, fuel, [rated], [fcost], avail; nb = 1, H = H);
                min_power = [minp], start_up_cost = [start], reservable = reservable)
            pv = unit("PV", "Renewable", "Sun", 50.0, 0.0, reshape([0.0, 1.0, 1.0, 0.0], H, 1))
            # Biomass, cheaper than gas: min 50 %, a start costs 1e-3
            bio = unit("Bio", "Renewable", "Biomass", 40.0, 1.0e-5, ones(H, 1);
                       minp = 0.5, start = 1.0e-3)
            gas = unit("Gas", "Non-renewable", "Gas", 100.0, 5.0e-5, ones(H, 1);
                       reservable = false)
            function run(mode)
                input = PowerSystemInput(
                    name = "vre", year = 2025, network = net, generators = [pv, bio, gas],
                    batteries = BatteryConfig[], demand = fill(30.0, H, 1),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    reserve_static_requirement = Dict(1 => 10.0),
                    reserve_static_default_ratio = 0.0, loss_of_reserve_static = 1.0,
                    loss_of_load_penalty = 100.0, curtailment_cost = 1.0e-6,
                    mode = mode, solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                return model, vars, extract_solution(model, vars, input)
            end
            model, vars, res = run("unit_commitment")
            # PV has no status / start-up variables (its start-ups were free
            # and arbitrary: 25,200 for the Cuban PV fleet in 720 h)
            @test count(v -> startswith(name(v), "gen_startup"), all_variables(model)) == 2H
            @test count(v -> startswith(name(v), "gen_status"), all_variables(model)) == 2H
            @test all(iszero, res.gen_status[1, 1, :]) && all(iszero, res.gen_startup[1, 1, :])
            # Biomass is committed: online wherever it generates, at least its
            # minimum, and its start is paid
            out_bio, on_bio = res.gen_output[2, 1, :], res.gen_status[2, 1, :]
            @test any(out_bio .> 1.0)
            @test all(on_bio[t] ≈ 1.0 for t in 1:H if out_bio[t] > 1e-6)
            @test all(out_bio .>= 20.0 .* on_bio .- 1e-6)
            @test res.cost_breakdown.startup_cost ≈ 1.0e-3 * sum(res.gen_startup[2, 1, :]) rtol = 1e-6
            @test sum(res.gen_startup[2, 1, :]) ≈ 1.0 atol = 1e-6
            for mode in ("unit_commitment", "economic_dispatch")
                model, vars, res = run(mode)
                # Biomass holds reserve (gas is not reservable, PV never)
                @test sum(value.(vars.reserve_static_loss)) ≈ 0.0 atol = 1e-6
                # Curtailment is PV's only: a biomass unit below its
                # availability spills nothing
                pv_spill = [50.0 * pv.availability[t, 1] - res.gen_output[1, 1, t] for t in 1:H]
                @test value.(vars.curtailment[1, :]) ≈ pv_spill atol = 1e-6
            end
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
                    [fcost], z, z, z, z, ones(H, 1), true,
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
                    [fcost], z, z, z, z, ones(H, 1), true,
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

        @testset "N-1 security cost over a window's committed steps" begin
            # A rolling window keeps its first `keep` steps. The cost of N-1
            # was the whole-window difference, which the runner scaled by
            # keep / H; it is now both objectives over the kept steps.
            H, keep = 4, 1
            function ngen4(name, rated, fcost)
                z = zeros(1)
                GeneratorConfig(
                    name, "Non-renewable", "Gas", [rated], [0.0],
                    [0.5], [0.4], [1.0], [1.0], z, z, [0.0],
                    [fcost], z, z, z, z, ones(H, 1), true,
                    [30.0], z, z, z, 50.0, "AC",
                    z, z, z, z, zeros(H, 1), z, z, z, z, false, z, z, z,
                )
            end
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            # Losing A leaves B's 30 MW: 20 MW short at 50 MW, none at 20 MW
            input = PowerSystemInput(;
                name = "n1", year = 2025, network = net,
                generators = [ngen4("A", 100.0, 1.0e-5), ngen4("B", 30.0, 1.0e-4)],
                batteries = BatteryConfig[], demand = reshape([50.0, 20.0, 20.0, 50.0], H, 1),
                temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                reserve_static_default_ratio = 0.0, loss_of_reserve_static = 1.0,
                loss_of_load_penalty = 100.0,
                n1_security_enabled = true, n1_generation_enabled = true,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test compute_n1_security_cost!(model, input) ≈ 40.0 rtol = 1e-6
            @test extract_solution(model, vars, input).n1_security_cost ≈ 40.0 rtol = 1e-6
            cs = ESFEX.committed_steps(vars, H, keep)
            part = extract_solution(model, vars, input; committed = cs)
            @test part.n1_security_cost ≈ 20.0 rtol = 1e-6     # was 40 (× 1/4 = 10)
            @test part.n1_security_cost ≈ part.cost_breakdown.n1_security_shortfall_cost rtol = 1e-6
            cs3 = ESFEX.committed_steps(vars, H, 3)
            @test extract_solution(model, vars, input; committed = cs3).n1_security_cost ≈
                  20.0 rtol = 1e-6
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

        @testset "generation N-1 per system" begin
            # Two systems, one node each, no link. A: a 100 MW unit serving
            # 50 MW and a 30 MW backup; B: one 40 MW unit serving 20 MW. A
            # merged run had one requirement, the overall largest unit (A's)
            # covered by A's backup and B's headroom: secure. Per system, A
            # is 20 MW short and B, losing its only unit, 20 MW short.
            H = 2
            function sgen(name, rated, fcost)
                z = zeros(2)
                GeneratorConfig(
                    name, "Non-renewable", "Gas", rated, z,
                    fill(0.5, 2), fill(0.4, 2), ones(2), ones(2), z, z, z,
                    fill(fcost, 2), z, z, z, z, ones(H, 2), true,
                    fill(30.0, 2), z, z, z, 50.0, "AC",
                    z, z, z, z, zeros(H, 2), z, z, z, z, false, z, z, z,
                )
            end
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            net2 = NetworkConfig(2, 2, buses, [1, 2], zeros(2, 2), zeros(2, 2),
                                 100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                                 TransmissionLineData[], TransformerData[],
                                 ACDCConverterData[], FrequencyConverterData[], 0.1)
            gens = [sgen("Big", [100.0, 40.0], 1.0e-5), sgen("Backup", [30.0, 0.0], 1.0e-4)]
            function sys_n1(groups; rtype = "largest_unit", rpct = 0.0)
                input = PowerSystemInput(
                    name = "A_B", year = 2025, network = net2, generators = gens,
                    batteries = BatteryConfig[], demand = repeat([50.0 20.0], H),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    co2_budget_groups = groups,
                    reserve_static_default_ratio = 0.0, loss_of_reserve_static = 1.0,
                    loss_of_load_penalty = 100.0,
                    n1_security_enabled = true, n1_generation_enabled = true,
                    n1_generation_reserve_type = rtype,
                    n1_generation_reserve_percentage = rpct,
                    mode = "economic_dispatch", solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                res = extract_solution(model, vars, input)
                return model, res.cost_breakdown.n1_security_shortfall_cost
            end
            two = [CO2BudgetGroup("A", [1], Inf, 0.0), CO2BudgetGroup("B", [2], Inf, 0.0)]
            model, cost = sys_n1(two)
            @test cost ≈ 40.0 * H atol = 1e-6
            @test constraint_by_name(model, "n1_gen_reserve_system_s1_t1") !== nothing
            @test constraint_by_name(model, "n1_gen_reserve_system_s2_t1") !== nothing
            # One system: one requirement, as before
            model, cost = sys_n1(CO2BudgetGroup[])
            @test cost ≈ 0.0 atol = 1e-6
            @test constraint_by_name(model, "n1_gen_reserve_system_t1") !== nothing
            # Percentage of each system's own demand: at 150 %, A needs 75 MW
            # (80 MW of headroom) and B 30 MW (20 MW: 10 short). Over the
            # merged demand it was 105 MW against 100 MW of headroom.
            _, cost = sys_n1(two; rtype = "percentage", rpct = 0.5)
            @test cost ≈ 0.0 atol = 1e-6
            _, cost = sys_n1(two; rtype = "percentage", rpct = 1.5)
            @test cost ≈ 10.0 * H atol = 1e-6
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
                1.0, "economic_dispatch", Dict{String, Any}(), nothing,
                Dict(1 => [100.0]))
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
                    1000.0, "economic_dispatch", Dict{String, Any}(), nothing,
                    Dict{Int, Vector{Float64}}(), 300.0, 1, 0)
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

        @testset "primary energy: development windows end with tanks 10 % full" begin
            # A 100-unit tank at the only node (configured, or built by the
            # master), no fuel supply and 30/day of non-electric demand over
            # 48 h: without an end condition the window drains the tank. A
            # development run's windows end it at least 10 % full; a tank the
            # master built used to be left out.
            function end_level(mode; built = false)
                fuel = FuelConfig("Gas", 1.0, 0.0, 10.0, 0.0, [0.0], [built ? 0.0 : 100.0],
                                  [0.5], 0.0, [0.0], 0.0, 0.0, 0.0)
                infra = Dict("Gas" => FuelInfrastructureConfig(0.0, 0.0, 0.0, 0.0, 0.0,
                                                               1.0, 30.0, 30.0, -1.0))
                ned = NonElectricDemandConfig("Industrial", "Gas", [10950.0], 0.0, ones(12))
                cumul = built ? Dict{String, Any}("storage" => Dict("Gas" => Dict(1 => 100.0))) :
                                Dict{String, Any}()
                init = built ? Dict("Gas" => [50.0]) : nothing
                inp = PrimaryEnergyInput(2025, 2025, 1, 48, [fuel], infra, [ned],
                    TransportRoute[], Dict{Int, Tuple{String, Float64, Float64, Float64}}(),
                    24, 48, 0.05, 1000.0, mode, cumul, init,
                    Dict{Int, Vector{Float64}}(), 300.0, 1, 0)
                m = ESFEX.Model(create_optimizer(solver_name = "highs"))
                ESFEX.set_silent(m)
                vars, temporal, prices = ESFEX.create_primary_energy_model(m, inp)
                terms = ESFEX.get_primary_energy_objective_terms(vars, inp, temporal, prices)
                ESFEX.@objective(m, Min, sum(values(terms)))
                optimize!(m)
                @test string(termination_status(m)) == "OPTIMAL"
                return value(vars.fuel_storage_level_hourly["Gas"][1, 49])
            end
            @test end_level("economic_dispatch") ≈ 0.0 atol = 1e-6
            @test end_level("unit_commitment") ≈ 0.0 atol = 1e-6
            @test end_level("development") ≈ 10.0 atol = 1e-6
            @test end_level("development"; built = true) ≈ 10.0 atol = 1e-6
            @test end_level("economic_dispatch"; built = true) ≈ 0.0 atol = 1e-6
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
                    1000.0, "economic_dispatch", Dict{String, Any}(), nothing,
                    Dict{Int, Vector{Float64}}(), 300.0, 1, 0)
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
                    Dict("Gas" => [0.0, init2]),
                    Dict{Int, Vector{Float64}}(), 300.0, 1, offset,
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
                1.0, "economic_dispatch", Dict{String, Any}(), nothing,
                Dict(1 => [100.0]))
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
                1.0, "economic_dispatch", Dict{String, Any}(), nothing,
                Dict(2 => [10.0]))
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
            # The unused fixed-production and investment inputs are gone
            for f in (:h2_production_hourly, :electrolyzer_config, :investment_from_master)
                @test !hasfield(PrimaryEnergyInput, f)
            end
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
                    1000.0, "economic_dispatch", Dict{String, Any}(), nothing,
                    Dict{Int, Vector{Float64}}(), 300.0, 2, 0)
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
                    "economic_dispatch", Dict{String, Any}(), nothing,
                    Dict{Int, Vector{Float64}}(), 1.0, tres, 0)
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
                TransportRoute[], Dict(1 => ("Gas", 5.0, 10.0, 0.5)), H, H, 0.05, 1.0, "economic_dispatch", Dict{String, Any}(), nothing,
                Dict{Int, Vector{Float64}}(), 1.0, tres, 0)
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
                # Fixed O&M is $/MW·yr: the dispatch's bucket also holds the
                # window's share (24 h of 8760) of the 30 MW's, the master
                # charges the full year (both charged it per hour, × 8760)
                fixed_year = 30.0 * 1.0e-6
                fixed_day = fixed_year * 24 / 8760
                @test elz_m + fixed_day ≈
                      tres * value(dmodel.ext[:cost_expressions][:electrolyzer_cost]) rtol = 1e-6
                @test value(ESFEX.master_fuel_annual_cost(mv, inp, 1)) ≈ fixed_year rtol = 1e-9
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
                Dict(1 => ("Gas", 5.0, 10.0, 0.5)), H, H, 0.05, 1.0, "economic_dispatch", Dict{String, Any}(), nothing,
                Dict{Int, Vector{Float64}}(), 1.0, 1, 0)
            return MasterProblemInput(years = [2025, 2026], network = single_bus(),
                generators = [gas], batteries = BatteryConfig[],
                base_demand = fill(50.0, 2H, 1), target_re_penetration = 0.0,
                initial_re_penetration = 0.0, representative_days_per_year = 1,
                min_day_separation = 1, hours_per_year = [24, 24],
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
            # Fixed O&M ($/MW·yr) on the power in service, once per year
            # (it was charged per hour, × hours_per_year)
            for y in 1:2
                @test value(ESFEX.master_fuel_annual_cost(mv, inp, y)) ≈
                      1.0e-6 * fi["electrolyzer_capacity"][y][1][1] rtol = 1e-6
            end

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
                  sum(value(v) * c for (_, v, c, _) in ESFEX.master_fuel_investment_terms(mv, inp)) rtol = 1e-9
            # Each year's operating cost is reported (it was always 0):
            # with the investment and the annual asset costs it makes up
            # the objective (no slack here)
            ops = res.total_operational_cost_by_year
            @test all(ops .> 0)
            npv = value(ESFEX.build_investment_cost_expression(mv, inp))
            for y in 1:2
                df = 1.0 / (1.0 + inp.discount_rate)^(y - 1)
                npv += df * (value(ESFEX.build_annual_asset_cost(mv, inp, y)) + ops[y])
            end
            @test npv ≈ objective_value(model) rtol = 1e-9
            # Benders reports its subproblems' cost for the same plan
            @test br.solution.total_operational_cost_by_year ≈ ops rtol = 1e-4

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
                res = extract_master_solution(model, mv, inp)
                # The expected operating cost per year (one scenario here)
                @test all(res.total_operational_cost_by_year .> 0)
                return objective_value(model), res.fuel_investment
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
                                [0.0], [1.0], [0.5], [0.0], [0.0], [0.0],
                                [10.0], [0.0], [0.0], 0.0, 100.0, [2.0e-6],
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
            # A biomass unit is committed like any dispatchable unit: it was
            # exempt as type "Renewable", so the master charged it no start-up
            bio = ESFEX.with_fields(gen; type = "Renewable", fuel = "Biomass")
            mkb(uc) = MasterProblemInput(
                years = [2025], network = net1, generators = [bio],
                batteries = BatteryConfig[], base_demand = dem,
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                unit_commitment = uc, verbose = false)
            b0, _ = master1(mkb(false))
            b1, _ = master1(mkb(true))
            @test objective_value(b1) - objective_value(b0) ≈ 365 * 2.0e-3 rtol = 1e-6
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
            # Its annuity over its 30-year life_time, paid in the one year
            @test coefficient(obj, mvars.converter_investment[1][("acdc", 1)]) ≈
                  1.0e-3 * ESFEX.capital_recovery_factor(inp.discount_rate, 30)
            # converter_invest_cost: a dear converter is not built (load is shed)
            model, mvars = master1(inp; mult = ScenarioMultipliers(converter_invest_cost = 1.0e10))
            @test value(mvars.converter_investment[1][("acdc", 1)]) ≈ 0.0 atol = 1e-6
        end

        @testset "master reservoir builds raise the reservoir's level bounds" begin
            # 50 MW hydro with 25 MWh/h of inflow and a 100 MWh reservoir
            # that starts and ends half full; no load for 12 h, then 50 MW
            # for 12 h, with diesel at 1e-3 as the alternative. Storing the
            # first half's 300 MWh on top of the half-full start needs 600
            # MWh: the master builds 500. The build used to be charged but
            # never added to the reservoir; then the start level stayed at
            # half the existing 100 MWh (a 350 MWh reservoir, 250 built),
            # while the dispatch starts half the in-service capacity full.
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
            @test res.reservoir_investment[1][1][1] ≈ 500.0 rtol = 1e-6
            # Built once: year 2 already has it
            @test get(res.reservoir_investment[2], 1, [0.0])[1] ≈ 0.0 atol = 1e-6
            @test res.total_investment_by_year[1] ≈ 500.0 * 1.0e-6 rtol = 1e-6
            # Year 2 still has it: the hydro covers all 600 MWh of the load
            @test sum(mvals(model, "op_y2_d1_gen[1,")) ≈ 600.0 rtol = 1e-6
            # The day starts half the in-service 600 MWh full
            @test mvals(model, "op_y1_d1_res_level[1,1,1]")[1] ≈ 300.0 rtol = 1e-6
        end

        @testset "TSAM reservoir boundaries and head fraction use the in-service capacity" begin
            # Two chained TSAM periods: day 1 has no load and 600 MWh of
            # inflow, day 2 needs 1200 MWh from the 50 MW hydro. Carrying the
            # 600 MWh across the period boundary needs a 600 MWh reservoir,
            # 500 more than the 100 it has. The boundary levels were bounded
            # by the existing 100 MWh, so the build was useless (none made)
            # and diesel served 500 MWh of day 2.
            hydro(; kw...) = ESFEX.with_fields(
                mkgen("Hydro", "Renewable", "Water", [50.0], [0.0], ones(2 * H24, 1);
                      nb = 1, H = 2 * H24);
                reservoir_capacity = [100.0], reservoir_initial_level = [0.5],
                reservoir_min_level = [0.0], reservoir_max_level = [1.0],
                reservoir_inflow = fill(25.0, 2 * H24, 1), reservoir_turbine_efficiency = [1.0],
                reservoir_evaporation_rate = [0.0], reservoir_pump_capacity = [0.0],
                reservoir_pump_efficiency = [1.0], reservoir_spillage_allowed = true,
                reservoir_invest_cost = [1.0e-6], reservoir_invest_max = [1000.0], kw...)
            diesel = mkgen("Diesel", "Non-renewable", "Diesel", [100.0], [1.0e-3],
                           ones(2 * H24, 1); nb = 1, H = 2 * H24)
            tinp(h) = MasterProblemInput(
                years = [2025], network = net1, generators = [h, diesel],
                batteries = BatteryConfig[],
                base_demand = reshape(vcat(zeros(H24), fill(50.0, H24)), 2 * H24, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                use_tsam = true, tsam_inter_period_linking = true,
                tsam_period_start_hours = [[1, H24 + 1]], tsam_period_weights = [[1.0, 1.0]],
                tsam_day_period = [[1, 2]], hours_per_year = [2 * H24], verbose = false)
            inp = tinp(hydro())
            model, mvars = master1(inp)
            res = extract_master_solution(model, mvars, inp)
            @test res.reservoir_investment[1][1][1] ≈ 500.0 rtol = 1e-6
            @test sum(mvals(model, "op_y1_d2_gen[2,")) ≈ 0.0 atol = 1e-6
            @test value(mvars.inter_period_reservoir[1][(1, 1, 1)]) ≈ 600.0 rtol = 1e-6

            # Head fraction (level - min·cap)/span: its minimum is the
            # in-service minimum level, so the build enters the numerator
            # (the span stays at the existing 80 MWh: linear). With min level
            # 0.2 and head_min_factor 0.3 the 50 MW unit's limit reads
            # gen ≤ 15 + 35·(level - 0.2·(100 + built))/80.
            hinp = tinp(hydro(reservoir_min_level = [0.2], reservoir_head_min_factor = [0.3]))
            hm, _, _ = create_master_problem(hinp; use_representative_days = true)
            built = variable_by_name(hm, "cap_res_y1_g1_b1")   # built up to year 1
            con = first(c for c in all_constraints(hm; include_variable_in_set_constraints = false)
                        if name(c) == "res_head_limit_g1_b1_t1")
            @test ESFEX.JuMP.normalized_coefficient(con, built) ≈ 35.0 * 0.2 / 80.0
            @test ESFEX.JuMP.normalized_rhs(con) ≈ 15.0 - 35.0 * 0.2 * 100.0 / 80.0
        end

        @testset "fuels' price_growth_rate grows the units' fuel_cost by year" begin
            # Gas grows 10 %/year from 2025; diesel's fuel is not in the
            # fuels block (flat). The candidate OCGT burns gas too. The
            # master charged every year the base price.
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4], ones(H24, 1);
                        nb = 1, H = H24)
            diesel = mkgen("Diesel", "Non-renewable", "Diesel", [100.0], [2.0e-4],
                           ones(H24, 1); nb = 1, H = H24)
            o = [0.0]
            ocgt = TechnologyConfig("OCGT", "Non-renewable", "Gas", [1.0], [50.0],
                                    ones(H24, 1), [1.0], [1.0], [1.0], [1.0], o, o, o,
                                    [3.0e-4], o, o, o, o, [25.0], o, o, 50.0, "AC", false, [1.0])
            mk(; years = [2025, 2026], kw...) = MasterProblemInput(;
                years = years, network = net1, generators = [gas, diesel],
                batteries = BatteryConfig[], technologies = [ocgt],
                base_demand = fill(50.0, H24 * length(years), 1),
                hours_per_year = fill(24, length(years)),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                verbose = false, kw...)
            # Objective coefficient of the first variable whose name matches
            function coefs(inp; mult = nothing)
                model = mult === nothing ?
                    create_master_problem(inp; use_representative_days = true)[1] :
                    create_stochastic_master_problem(StochasticMasterInput(
                        inp, [Scenario("s", 1.0, mult)], true))[1]
                obj = objective_function(model)
                return re -> coefficient(obj, first(v for v in all_variables(model)
                                                    if occursin(re, name(v))))
            end
            gas_y(y) = Regex("^op_y$(y)_d\\d+_gen\\[1,1,1\\]")
            diesel_y(y) = Regex("^op_y$(y)_d\\d+_gen\\[2,1,1\\]")
            tech_y(y) = Regex("^tech_out_y$(y)_d\\d+\\[1,1,1\\]")
            growth = Dict("Gas" => 0.1)
            flat = coefs(mk())
            grown = coefs(mk(fuel_price_growth = growth))
            @test grown(gas_y(1)) ≈ flat(gas_y(1)) rtol = 1e-12
            @test grown(gas_y(2)) ≈ 1.1 * flat(gas_y(2)) rtol = 1e-12
            @test grown(diesel_y(2)) ≈ flat(diesel_y(2)) rtol = 1e-12
            @test grown(tech_y(2)) ≈ 1.1 * flat(tech_y(2)) rtol = 1e-12
            # A myopic year's master (one year, 2027) grows from the run's
            # first year, not its own
            flat1 = coefs(mk(years = [2027]))
            grown1 = coefs(mk(years = [2027], fuel_price_growth = growth,
                              fuel_price_base_year = 2025))
            @test grown1(gas_y(1)) ≈ 1.21 * flat1(gas_y(1)) rtol = 1e-12
            @test grown1(tech_y(1)) ≈ 1.21 * flat1(tech_y(1)) rtol = 1e-12
            # A stochastic scenario's fuel_price_growth multiplies the grown
            # price (each counted once)
            sflat = coefs(mk(); mult = ScenarioMultipliers())
            sgrown = coefs(mk(fuel_price_growth = growth);
                           mult = ScenarioMultipliers(fuel_price_growth = 1.05))
            @test sgrown(gas_y(2)) ≈ 1.1 * 1.05 * sflat(gas_y(2)) rtol = 1e-12
            @test sgrown(diesel_y(2)) ≈ 1.05 * sflat(diesel_y(2)) rtol = 1e-12
            @test ESFEX.fuel_price_factor(0.1, 2027, 2025) ≈ 1.21
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
            # Annuities over the technology's 25 and the converter's 30 years
            a = (ESFEX.capital_recovery_factor(inp.discount_rate, 25),
                 ESFEX.capital_recovery_factor(inp.discount_rate, 30))
            @test all(coefs(ScenarioMultipliers()) .≈ (1.0, 2.0) .* a)
            @test all(coefs(ScenarioMultipliers(invest_cost_conventional = 3.0)) .≈ (3.0, 2.0) .* a)
            @test all(coefs(ScenarioMultipliers(converter_invest_cost = 7.0)) .≈ (1.0, 14.0) .* a)
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
                                [0.0], [1.0], [0.5], [0.0], [0.0], [0.0],
                                [10.0], [0.0], [0.0], 0.0, 100.0, [2.0],
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

        # ── Battery operating fields (round 8, A8) ───────────────────────────
        # BatteryConfig with the operating fields; 20 MW / `cap` MWh, 95 %.
        function mkbat(; cap = 100.0, ramp = 0.0, avail = zeros(0, 1), reservable = true,
                       fixed = 0.0, maint = 0.0)
            z = [0.0]
            BatteryConfig("Li1", [cap], [20.0], [20.0], [0.95], [0.95], z, [1.0], [0.5], z,
                          z, z, [10.0], z, z, 0.0, 100.0, [maint], z, false, "AC",
                          z, z, [1.0], [ramp], [ramp], [fixed], reservable, avail)
        end
        function bat_dispatch(bats, demand; H = size(demand, 1), tres = 1, gas_mw = 40.0,
                              curves = Dict{Int, Dict{Int, Vector{CostSegment}}}(), kw...)
            gas = mkgen("Gas", "Non-renewable", "Gas", [gas_mw], [3.0e-5],
                        ones(H, 1); nb = 1, H = H)
            input = PowerSystemInput(;
                name = "bat", year = 2025, network = net1, generators = [gas],
                batteries = bats, demand = reshape(demand, H, 1),
                temporal = TemporalConfig(H, tres, H, 0, H, H, 1, 1, 1),
                min_cycling_ratio = 0.0, soc_end_tolerance = 0.0,
                loss_of_load_penalty = 0.01, bat_cost_curves = curves,
                mode = "economic_dispatch", solver_name = "highs", verbose = false, kw...)
            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            return model, vars, input
        end
        # The same day as a master (one representative day = 365 days)
        function bat_master(bats, demand; btechs = BatteryTechnologyConfig[], kw...)
            gas = mkgen("Gas", "Non-renewable", "Gas", [40.0], [3.0e-5],
                        ones(H24, 1); nb = 1, H = H24)
            return MasterProblemInput(;
                years = [2025], network = net1, generators = [gas], batteries = bats,
                battery_technologies = btechs, base_demand = reshape(demand, H24, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                loss_of_load_penalty = 0.01, reserve_margin = 0.0, verbose = false, kw...)
        end
        # 30 MW of load and a 60 MW hour: the battery covers the spike
        spike = vcat(fill(30.0, 12), [60.0], fill(30.0, 11))

        @testset "capacity adequacy: firm capacity at peak-period availability" begin
            # Peak period = top 10 % of the day's demand: steps 18-20 (100 MW).
            # Dispatchable units count at their mean availability there, variable
            # renewables at their minimum availability. Every non-"Renewable"
            # unit used to count fully and "Renewable" biomass at its minimum
            # (0 for a seasonal mill); storage ignored its availability.
            demand = fill(50.0, H24); demand[18:20] .= 100.0
            half_at_peak = ones(H24, 1); half_at_peak[18:20, 1] .= 0.5
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-5], half_at_peak;
                        nb = 1, H = H24)
            mill_avail = ones(H24, 1); mill_avail[1:12, 1] .= 0.0   # off-season
            mill = mkgen("Mill", "Renewable", "Biomass", [30.0], [1.0e-5], mill_avail;
                         nb = 1, H = H24)
            sun = zeros(H24, 1); sun[8:17, 1] .= 1.0
            pv = mkgen("PV", "Renewable", "Sun", [40.0], [0.0], sun; nb = 1, H = H24)
            o = [0.0]
            gt = TechnologyConfig("GT", "Non-renewable", "Gas", [1.0], [50.0], half_at_peak,
                                  [1.0], [1.0], [1.0], [1.0], o, o, o, o, o, o, o, o,
                                  [25.0], o, o, 50.0, "AC", false, [1.0])
            # mkgen leaves risk_coefficient at 0 (no firm capacity at all)
            firm(g) = ESFEX.with_fields(g; risk_coefficient = [1.0])
            inp = MasterProblemInput(years = [2025], network = net1,
                generators = firm.([gas, mill, pv]), batteries = [mkbat(avail = half_at_peak)],
                technologies = [gt], base_demand = reshape(demand, H24, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                # 4 h: the 20 MW / 100 MWh battery is not energy-limited here
                # (see "storage counts up to its energy")
                reserve_margin = 1.2, storage_firm_hours = 4.0, verbose = false)
            @test ESFEX.adequacy_peak_steps(inp, 1, 1) == [18, 19, 20]
            model = ESFEX.Model()
            vars = ESFEX.build_master_variables!(model, inp)
            ESFEX.add_capacity_adequacy_constraints!(model, vars, inp)
            con = constraint_by_name(model, "cap_adequacy_y1_node1")
            # firm: gas 100 × 0.5 + mill 30 × 1 + PV 40 × 0 + battery 20 × 0.5 = 90
            @test ESFEX.normalized_rhs(con) ≈ 1.2 * 100.0 - 90.0
            @test ESFEX.normalized_coefficient(con, vars.tech_investment[1][1][1]) ≈ 0.5
        end

        @testset "capacity adequacy: storage counts up to its energy (round 33)" begin
            # 12 dark hours a day; a 20 MW / 80 MWh battery (4 h, 95 %) used to
            # count its full 20 MW, so it alone "met" a 20 MW peak that lasts
            # the night. It now counts min(power, usable energy / H), H = the
            # longest stretch without solar (12 h): 80 × 0.95 / 12 = 6.33 MW.
            sun = zeros(H24, 1); sun[7:18, 1] .= 1.0
            pv = mkgen("PV", "Renewable", "Sun", [40.0], [0.0], sun; nb = 1, H = H24)
            o = [0.0]
            gt = TechnologyConfig("GT", "Non-renewable", "Gas", [1.0e-3], [50.0], ones(H24, 1),
                                  [1.0], [1.0], [1.0], [1.0], o, o, o, o, o, o, o, o,
                                  [25.0], o, o, 50.0, "AC", false, [1.0])
            inp = MasterProblemInput(years = [2025], network = net1, generators = [pv],
                batteries = [mkbat(cap = 80.0)], technologies = [gt],
                base_demand = fill(20.0, H24, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 1, min_day_separation = 1,
                loss_of_load_penalty = 10.0, reserve_margin = 1.0, verbose = false)
            @test ESFEX.master_storage_firm_hours(inp) == 12.0
            usable = 80.0 * 0.95
            model = ESFEX.Model()
            vars = ESFEX.build_master_variables!(model, inp)
            ESFEX.add_capacity_adequacy_constraints!(model, vars, inp)
            con = constraint_by_name(model, "cap_adequacy_y1_node1")
            @test ESFEX.normalized_rhs(con) ≈ 20.0 - usable / 12
            # The battery alone no longer meets the peak: the master builds
            # the firm capacity the night needs (it built none before)
            m, v, _ = create_master_problem(inp; use_representative_days = false)
            optimize!(m)
            @test value(v.tech_investment[1][1][1]) ≈ 20.0 - usable / 12 rtol = 1e-6
            @test value(v.slack_capacity[(1, 1)]) ≈ 0.0 atol = 1e-6
            # Configured hours: a 2-hour stretch leaves the battery its 20 MW
            m2, v2, _ = create_master_problem(ESFEX.with_fields(inp; storage_firm_hours = 2.0);
                                              use_representative_days = false)
            optimize!(m2)
            @test value(v2.tech_investment[1][1][1]) ≈ 0.0 atol = 1e-6

            # Battery technology: firm ≤ power built, H × firm ≤ usable energy built
            li = BatteryTechnologyConfig("LiNew", [1.0e-3], [1.0e-3], [100.0], [400.0], 4.0, 4.0,
                                         [0.95], [0.95], o, [15.0], [0.5], [0.0], [1.0],
                                         o, o, o, false, "AC", o, [1.0])
            binp = ESFEX.with_fields(inp; batteries = BatteryConfig[],
                                     battery_technologies = [li], technologies = TechnologyConfig[])
            cm = ESFEX.Model()
            cv = ESFEX.build_master_variables!(cm, binp)
            ESFEX.add_capacity_adequacy_constraints!(cm, cv, binp)
            ce = constraint_by_name(cm, "bat_tech_firm_energy_y1_bt1_b1")
            cf = variable_by_name(cm, "bat_tech_firm_y1_bt1_b1")
            @test ESFEX.normalized_coefficient(ce, cf) ≈ 12.0
            @test ESFEX.normalized_coefficient(ce, cv.bat_tech_capacity_investment[1][1][1]) ≈ -0.95
            @test ESFEX.normalized_coefficient(
                constraint_by_name(cm, "cap_adequacy_y1_node1"), cf) ≈ 1.0
            bm, bv, _ = create_master_problem(binp; use_representative_days = false)
            firm = variable_by_name(bm, "bat_tech_firm_y1_bt1_b1")
            optimize!(bm)
            # 20 MW firm from 4-hour storage takes 12 / (4 × 0.95) × 20 MW of power
            @test value(firm) ≈ 20.0 rtol = 1e-6
            @test value(bv.bat_tech_power_investment[1][1][1]) ≈ 20.0 * 12 / (4 * 0.95) rtol = 1e-6
        end

        @testset "battery ramp limits, with the rolling-horizon seam" begin
            # 5 MW/h: from 0 MW at the previous window's last step the battery
            # gives 5 then 10 MW, so 10 of the second step's 20 MW are shed.
            for (ramp, tres, shed) in ((0.0, 1, 0.0), (0.25, 1, 10.0), (0.25, 2, 0.0))
                bat = mkbat(cap = 400.0, ramp = ramp)
                model, vars, input = bat_dispatch([bat], [40.0, 60.0]; tres = tres,
                    cyclic_end_soc = false, reserve_static_default_ratio = 0.0,
                    battery_discharge_prev = Dict(1 => Dict(1 => 0.0)))
                res = extract_solution(model, vars, input)
                @test res.load_shed_total ≈ shed * tres atol = 1e-6
            end
            # Charging 20 MW at the seam, it can only fall to 15 MW (it had
            # no limit: the charge stopped at once)
            bat = mkbat(cap = 400.0, ramp = 0.25, maint = 1.0e-6)
            model, vars, _ = bat_dispatch([bat], [30.0, 30.0]; cyclic_end_soc = false,
                reserve_static_default_ratio = 0.0, gas_mw = 100.0,
                battery_charge_prev = Dict(1 => Dict(1 => 20.0)))
            @test value(vars.bat_charge[1, 1, 1]) ≈ 15.0 atol = 1e-6
            @test value(vars.bat_charge[1, 1, 2]) ≈ 10.0 atol = 1e-6
        end

        @testset "battery availability scales its charge and discharge power" begin
            avail = reshape([0.5, 1.0], 2, 1)
            model, vars, input = bat_dispatch([mkbat(avail = avail)], [60.0, 30.0];
                cyclic_end_soc = false, reserve_static_default_ratio = 0.0)
            res = extract_solution(model, vars, input)
            @test res.load_shed_total ≈ 10.0 atol = 1e-6
            @test value(vars.bat_discharge[1, 1, 1]) ≈ 10.0 atol = 1e-6
        end

        @testset "a battery with energy and no power holds nothing" begin
            # 0.18 MWh and no charge power at a bus (a master build whose
            # 0.09 MW had been dropped): the minimum cycling asked for charge
            # that bat_charge <= 0 forbade, and every window was infeasible.
            bat = ESFEX.with_fields(mkbat(cap = 0.1833);
                max_charge_power = [0.0], max_discharge_power = [0.0])
            for mode in ("economic_dispatch", "unit_commitment")
                model, vars, input = bat_dispatch([bat], fill(30.0, 48); mode = mode,
                    min_cycling_ratio = 0.8, min_cycling_period_days = 7.0,
                    cyclic_end_soc = false)
                @test all(abs(value(vars.bat_soc[1, 1, t])) < 1e-9 for t in 1:49)
                @test all(iszero, extract_solution(model, vars, input).bat_soc)
                @test constraint_by_name(model, "bat_min_cycling_1_1") === nothing
            end
        end

        @testset "a battery that cannot charge keeps the window feasible" begin
            # Unavailable the whole window, 0.1 %/h self-discharge, starting
            # at its 20 % floor: the SOC decays below the floor, and neither
            # the floor, the minimum cycling nor the cyclic end SOC asks for
            # charge it cannot take. All three made the window infeasible.
            bat = ESFEX.with_fields(mkbat(avail = zeros(48, 1));
                soc_min = [0.2], soc_initial = [0.2], self_discharge = [0.001])
            for cyclic in (false, true)
                model, vars, _ = bat_dispatch([bat], fill(30.0, 48);
                    min_cycling_ratio = 0.8, min_cycling_period_days = 1.0,
                    cyclic_end_soc = cyclic, soc_end_tolerance = 0.01)
                @test value(vars.bat_soc[1, 1, 49]) ≈ 20.0 * 0.999^48 rtol = 1e-6
                @test constraint_by_name(model, "bat_min_cycling_1_1") === nothing
            end
            # Available, it holds its floor and cycles as before
            model, vars, _ = bat_dispatch([ESFEX.with_fields(bat; availability = zeros(0, 1))],
                fill(30.0, 48); min_cycling_ratio = 0.8, min_cycling_period_days = 1.0,
                cyclic_end_soc = false)
            @test minimum(value(vars.bat_soc[1, 1, t]) for t in 2:49) >= 20.0 - 1e-6
            @test sum(value(vars.bat_charge[1, 1, t]) for t in 1:48) >= 0.8 * 100 * 2 - 1e-6
        end

        @testset "minimum cycling out of reach is priced, not infeasible" begin
            # 0.8 × 400 MWh a day = 320 MW-steps. Ramping 0.2 MW/h from 0 at
            # the seam the battery charges at most 0.2 × (1 + … + 24) = 60.
            short(m) = only(v for v in all_variables(m) if name(v) == "bat_min_cycling_short_1_1")
            for (ramp, missing) in ((0.01, 260.0), (0.0, 0.0))
                model, vars, _ = bat_dispatch([mkbat(cap = 400.0, ramp = ramp)], fill(30.0, H24);
                    min_cycling_ratio = 0.8, min_cycling_period_days = 1.0,
                    cyclic_end_soc = false, reserve_static_default_ratio = 0.0,
                    battery_charge_prev = Dict(1 => Dict(1 => 0.0)))
                @test value(short(model)) ≈ missing atol = 1e-6
                @test coefficient(objective_function(model), short(model)) ≈ 0.1 * 0.01
            end
        end

        @testset "only reservable batteries hold reserve" begin
            # 5 MW of static reserve; the gas unit is not reservable. Every
            # battery used to count.
            for (reservable, loss) in ((true, 0.0), (false, 10.0))
                model, vars, _ = bat_dispatch([mkbat(reservable = reservable)], [30.0, 30.0];
                    cyclic_end_soc = false, reserve_static_requirement = Dict(1 => 5.0))
                @test sum(value.(vars.reserve_static_loss)) ≈ loss atol = 1e-6
            end
        end

        @testset "battery fixed cost per MW-year, prorated to the window" begin
            m0, _, _ = bat_dispatch([mkbat()], fill(30.0, H24))
            m1, v1, i1 = bat_dispatch([mkbat(fixed = 1.0e-3)], fill(30.0, H24))
            @test objective_value(m1) - objective_value(m0) ≈ 1.0e-3 * 20 * 24 / 8760 rtol = 1e-6
            res = extract_solution(m1, v1, i1)
            @test res.cost_breakdown.fixed_om_cost ≈ 1.0e-3 * 20 * 24 / 8760 rtol = 1e-6
        end

        @testset "flat battery discharge curve prices the discharge" begin
            # A single segment at 1e-3 (above gas): the battery's stored energy
            # is not spent. Single-segment curves were skipped (discharge free).
            flat = Dict(1 => Dict(1 => [CostSegment(1.0, 1.0e-3)]))
            for (curves, used) in ((Dict{Int, Dict{Int, Vector{CostSegment}}}(), true),
                                   (flat, false))
                model, vars, _ = bat_dispatch([mkbat()], [30.0, 30.0]; curves = curves,
                    cyclic_end_soc = false, reserve_static_default_ratio = 0.0)
                d = value(vars.bat_discharge[1, 1, 1]) + value(vars.bat_discharge[1, 1, 2])
                @test (d > 1.0) == used
            end
            # The master prices it the same way
            inp = bat_master([mkbat()], spike; bat_cost_curves = flat)
            model, _, _ = create_master_problem(inp; use_representative_days = true)
            seg = only(v for v in all_variables(model) if name(v) == "op_y1_d1_bseg_1_1[1,1]")
            @test coefficient(objective_function(model), seg) ≈ 365 * 1.0e-3 rtol = 1e-9
        end

        @testset "master battery ramp, availability, reservable and fixed cost as the dispatch" begin
            # Each field changes the master's year cost by 365 × what it
            # changes the dispatch's cost over the same day.
            avail = reshape([t == 13 ? 0.5 : 1.0 for t in 1:H24], H24, 1)
            cases = [
                (mkbat(ramp = 0.25), (;)),
                (mkbat(avail = avail), (;)),
                (mkbat(reservable = false), (; reserve_static_requirement = Dict(1 => 5.0))),
                (mkbat(fixed = 1.0e-3), (;)),
            ]
            for (bat, kw) in cases
                req = get(kw, :reserve_static_requirement, Dict{Int, Float64}())
                base = mkbat()
                d0, _, _ = bat_dispatch([base], spike; reserve_static_default_ratio = 0.0,
                                        reserve_static_requirement = req)
                d1, _, _ = bat_dispatch([bat], spike; reserve_static_default_ratio = 0.0,
                                        reserve_static_requirement = req)
                m0, _ = master1(bat_master([base], spike; reserve_static_default_ratio = 0.0,
                                           reserve_static_requirement = req))
                m1, _ = master1(bat_master([bat], spike; reserve_static_default_ratio = 0.0,
                                           reserve_static_requirement = req))
                dd = objective_value(d1) - objective_value(d0)
                @test dd > 1e-9
                @test objective_value(m1) - objective_value(m0) ≈ 365 * dd rtol = 1e-6
            end

            # A retired battery (no life extension) holds no reserve in the
            # master: it used to be credited its nameplate discharge power.
            # (Life extension at 1 per MW costs more than the reserve shortfall.)
            old = ESFEX.with_fields(mkbat(); life_time = [5.0], initial_age = [10.0],
                                    invest_cost_power = [1.0])
            m, _ = master1(bat_master([old], fill(30.0, H24); reserve_static_default_ratio = 0.0,
                                      reserve_static_requirement = Dict(1 => 5.0),
                                      loss_of_reserve_static = 1.0e-6))
            @test sum(mvals(m, "bat_life_ext")) ≈ 0.0 atol = 1e-9
            @test sum(mvals(m, "op_y1_d1_rs_loss")) ≈ 5.0 * H24 atol = 1e-6
        end

        @testset "TSAM boundary SOC of a battery uses its in-service energy" begin
            # Two chained TSAM periods: day 1 has no load and 4 h of 20 MW
            # PV, day 2 needs 60 MWh that only the battery (charged on day 1)
            # or 1e-3 diesel can serve. The battery is past its life_time;
            # the master may extend it. The boundary SOC was bounded by the
            # existing energy within lifetime only (0 here), so even the
            # life-extended battery could not carry energy across periods.
            H2 = 2 * H24
            pv_av = zeros(H2, 1); pv_av[1:4, 1] .= 1.0
            pv = mkgen("PV", "Renewable", "Solar", [20.0], [0.0], pv_av; nb = 1, H = H2)
            diesel = mkgen("Diesel", "Non-renewable", "Diesel", [100.0], [1.0e-3],
                           ones(H2, 1); nb = 1, H = H2)
            tinp(bat) = MasterProblemInput(
                years = [2025], network = net1, generators = [pv, diesel],
                batteries = [bat],
                base_demand = reshape(vcat(zeros(H24), fill(2.5, H24)), H2, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                loss_of_load_penalty = 0.01, reserve_margin = 0.0,
                reserve_static_default_ratio = 0.0,
                use_tsam = true, tsam_inter_period_linking = true,
                tsam_period_start_hours = [[1, H24 + 1]], tsam_period_weights = [[1.0, 1.0]],
                tsam_day_period = [[1, 2]], hours_per_year = [2 * H24], verbose = false)
            old = ESFEX.with_fields(mkbat(); life_time = [5.0], initial_age = [10.0],
                                    invest_cost_power = [1.0e-7])
            model, mvars = master1(tinp(old))
            ext = only(v for v in all_variables(model) if startswith(name(v), "bat_life_ext"))
            @test value(ext) > 1.0
            @test sum(mvals(model, "op_y1_d2_gen[2,")) ≈ 0.0 atol = 1e-6
            # 60 MWh discharged at 95 %: day 2 starts with at least 63.2 MWh
            # carried across the boundary (it was held at 0)
            @test value(mvars.inter_period_soc[1][(1, 1, 1)]) >= 60.0 / 0.95 - 1e-6
            # The inter-day bound is the period's SOC bound: life-extended MW
            # at the battery's 100 MWh / 20 MW ratio
            cap_con(m) = first(c for c in all_constraints(m; include_variable_in_set_constraints = false)
                               if name(c) == "soc_inter_y1_b1_n1_max_d1")
            @test ESFEX.JuMP.normalized_coefficient(cap_con(model), ext) ≈ -5.0

            # Within its lifetime, with degradation: the boundary bound is the
            # degraded energy the period's SOC bound uses, 100 × 0.95^4 MWh
            # (round 43: it was the rated 100 MWh, the master did not
            # degrade batteries)
            young = ESFEX.with_fields(mkbat(); degradation_rate = [0.05], initial_age = [4.0])
            ym, _, _ = create_master_problem(tinp(young); use_representative_days = true)
            @test ESFEX.JuMP.normalized_rhs(cap_con(ym)) ≈ 100.0 * 0.95^4
            soc = variable_by_name(ym, "op_y1_d1_bat_soc[1,1,2]")
            soc_cap = only(c for c in all_constraints(ym, AffExpr, ESFEX.JuMP.MOI.LessThan{Float64})
                           if collect(keys(constraint_object(c).func.terms)) == [soc])
            @test ESFEX.JuMP.normalized_rhs(soc_cap) ≈ 100.0 * 0.95^4
        end

        @testset "master degrades existing batteries as the dispatch (round 43)" begin
            # A 20 MW / 100 MWh battery aged 10, 5 %/yr: the dispatch holds
            # it at 0.95^10 of its ratings; the master held it at its rated
            # power and energy, so the plan counted on storage the dispatch
            # of the same year did not have.
            f = 0.95^10
            aged = ESFEX.with_fields(mkbat(); degradation_rate = [0.05], initial_age = [10.0],
                                     life_time = [30.0])
            @test ESFEX.battery_degradation_factor(aged, 1, 1) ≈ f
            @test ESFEX.battery_degradation_factor(aged, 1, 3) ≈ 0.95^12
            @test ESFEX.battery_degradation_factor(mkbat(), 1, 3) == 1.0
            # Master year y and the dispatch y − 1 years after its base year
            for (y, year) in ((1, 2025), (3, 2027))
                lim = ESFEX.battery_power_limits(aged, 1, (; year = year, base_year = 2025))
                inp = bat_master([aged], spike)
                inp = ESFEX.with_fields(inp; years = collect(2025:2024 + y))
                vars = ESFEX.build_master_variables!(ESFEX.Model(), inp)
                p = ESFEX.battery_power_in_service(vars, aged, 1, 1, y)
                e = ESFEX.battery_energy_in_service(vars, aged, 1, 1, y)
                @test ESFEX.JuMP.constant(p) ≈ lim[3] ≈ 20.0 * 0.95^(9 + y)
                @test ESFEX.JuMP.constant(e) ≈ lim[1] ≈ 100.0 * 0.95^(9 + y)
            end

            # The day model's charge, discharge and SOC bounds are the
            # dispatch's for the same year
            model, mvars, _ = create_master_problem(bat_master([aged], spike);
                                                    use_representative_days = true)
            bound(m, v) = ESFEX.JuMP.normalized_rhs(only(
                c for c in all_constraints(m, AffExpr, ESFEX.JuMP.MOI.LessThan{Float64})
                if collect(keys(constraint_object(c).func.terms)) == [v]))
            @test bound(model, variable_by_name(model, "op_y1_d1_bat_ch[1,1,13]")) ≈ 20.0 * f
            @test bound(model, variable_by_name(model, "op_y1_d1_bat_dch[1,1,13]")) ≈ 20.0 * f
            @test bound(model, variable_by_name(model, "op_y1_d1_bat_soc[1,1,2]")) ≈ 100.0 * f

            # Same day, same cost: what ageing the battery costs the master
            # is 365 × what it costs the dispatch (it covered the 60 MW spike
            # in full; at 12 MW the 40 MW gas leaves load unserved)
            d0, _, _ = bat_dispatch([mkbat()], spike)
            d1, _, _ = bat_dispatch([aged], spike)
            m0, _ = master1(bat_master([mkbat()], spike))
            m1, _ = master1(bat_master([aged], spike))
            dd = objective_value(d1) - objective_value(d0)
            @test dd > 1e-6
            @test objective_value(m1) - objective_value(m0) ≈ 365 * dd rtol = 1e-6

            # Firm capacity: the 40 MW gas and the battery's degraded power
            # (4 firm hours: not energy-limited)
            inp = bat_master([aged], spike; reserve_margin = 1.0, storage_firm_hours = 4.0)
            am = ESFEX.Model()
            avars = ESFEX.build_master_variables!(am, inp)
            ESFEX.add_capacity_adequacy_constraints!(am, avars, inp)
            @test ESFEX.normalized_rhs(constraint_by_name(am, "cap_adequacy_y1_node1")) ≈
                  60.0 - 40.0 - 20.0 * f

            # Past its lifetime the master keeps at most the degraded power;
            # cheap life extension keeps all of it, the energy at 5 h, and
            # pays no decommissioning
            old(dc) = ESFEX.with_fields(aged; life_time = [5.0], invest_cost_power = [1.0e-7],
                                        decommissioning_cost = [dc])
            ext_of(m) = only(v for v in all_variables(m) if startswith(name(v), "bat_life_ext"))
            em, evars = master1(bat_master([old(0.0)], spike))
            @test ESFEX.JuMP.upper_bound(ext_of(em)) ≈ 20.0 * f
            @test value(ext_of(em)) ≈ 20.0 * f rtol = 1e-6
            @test value(ESFEX.battery_energy_in_service(evars, old(0.0), 1, 1, 1)) ≈
                  100.0 * f rtol = 1e-6
            res = ESFEX.extract_master_solution(em, evars, bat_master([old(0.0)], spike))
            @test res.cumulative_bat_power[1][1][1] ≈ 20.0 * f rtol = 1e-6
            @test res.bat_life_extension[1][1][1] ≈ 1.0 rtol = 1e-6
            dm, _ = master1(bat_master([old(1.0e-3)], spike))
            @test objective_value(dm) ≈ objective_value(em) rtol = 1e-9
            # Within its lifetime the reported capacity is degraded too
            rm, rvars = master1(bat_master([aged], spike))
            r = ESFEX.extract_master_solution(rm, rvars, bat_master([aged], spike))
            @test r.cumulative_bat_power[1][1][1] ≈ 20.0 * f
            @test r.cumulative_bat_capacity[1][1][1] ≈ 100.0 * f
        end

        # A battery technology at d/yr, 1 h minimum (round 44)
        function li_deg(d; fixed = 0.0)
            o = [0.0]
            BatteryTechnologyConfig("LiNew", [1.0e-4], [1.0e-5], [50.0], [200.0], 1.0, 8.0,
                [0.95], [0.95], [d], [15.0], [0.5], [0.0], [1.0], o, o, [1.0e-7], false,
                "AC", o, [1.0], o, o, [fixed], true, zeros(0, 1))
        end
        # The right-hand side of the only `≤` row on variable `v` alone
        only_bound(m, v) = ESFEX.JuMP.normalized_rhs(only(
            c for c in all_constraints(m, AffExpr, ESFEX.JuMP.MOI.LessThan{Float64})
            if collect(keys(constraint_object(c).func.terms)) == [v]))

        @testset "master degrades the batteries it builds, as the dispatch (round 44)" begin
            # A build of year 1 is 5 years old in year 6: 0.95^5 of its
            # power and energy is left, as the dispatch ages the battery it
            # becomes. The master held it at what was built for its whole
            # life (and so did the dispatch), so the technology's
            # degradation_rate was never applied.
            bt = li_deg(0.05; fixed = 1.0e-3)
            inp = bat_master(BatteryConfig[], spike; btechs = [bt], years = collect(2025:2030))
            vars = ESFEX.build_master_variables!(ESFEX.Model(), inp)
            @test vars.years_per_inv_period == 1
            pinv(y) = vars.bat_tech_power_investment[y][1][1]
            einv(y) = vars.bat_tech_capacity_investment[y][1][1]
            p6 = ESFEX.bat_tech_power_built(vars, inp, 1, 1, 6; state = false)
            e6 = ESFEX.bat_tech_energy_built(vars, inp, 1, 1, 6; state = false)
            @test coefficient(p6, pinv(1)) ≈ 0.95^5
            @test coefficient(e6, einv(1)) ≈ 0.95^5
            @test coefficient(p6, pinv(4)) ≈ 0.95^2
            @test coefficient(p6, pinv(6)) == 1.0
            @test ESFEX.bat_tech_degradation_factor(bt, 1, 5) ≈ 0.95^5
            @test ESFEX.bat_tech_degradation_factor(li_deg(0.0), 1, 5) == 1.0
            # Fixed O&M stays on the rated power built (round 12)
            om = ESFEX.build_annual_asset_cost(vars, inp, 6)
            @test coefficient(om, pinv(1)) ≈ 1.0e-3
            @test coefficient(om, pinv(6)) ≈ 1.0e-3

            # The battery the build becomes: in a later myopic year an
            # existing battery aged 5 at the technology's rate, which the
            # master and the dispatch of that year hold at 0.95^5 too
            mine = ESFEX.with_fields(mkbat(); degradation_rate = [0.05], initial_age = [5.0],
                                     life_time = [15.0])
            mvars = ESFEX.build_master_variables!(ESFEX.Model(), bat_master([mine], spike))
            lim = ESFEX.battery_power_limits(mine, 1, (; year = 2025, base_year = 2025))
            @test ESFEX.JuMP.constant(ESFEX.battery_power_in_service(mvars, mine, 1, 1, 1)) ≈
                  lim[3] ≈ 20.0 * 0.95^5
            @test ESFEX.JuMP.constant(ESFEX.battery_energy_in_service(mvars, mine, 1, 1, 1)) ≈
                  lim[1] ≈ 100.0 * 0.95^5

            # Solved, at 50 %/yr, two years of one spiky day each: year 1
            # builds the 20 MW the spike takes; half of it is left in year
            # 2, which builds the rest. The day model's capacity, the
            # reported capacity (what the dispatch receives, with no further
            # degradation) and the Benders copy all hold the build degraded.
            inp2 = MasterProblemInput(;
                years = [2025, 2026], network = net1,
                generators = [mkgen("Gas", "Non-renewable", "Gas", [40.0], [3.0e-5],
                                    ones(H24, 1); nb = 1, H = H24)],
                batteries = BatteryConfig[], battery_technologies = [li_deg(0.5)],
                base_demand = reshape(vcat(spike, spike), 2 * H24, 1),
                hours_per_year = [H24, H24], target_re_penetration = 0.0,
                initial_re_penetration = 0.0, representative_days_per_year = 1,
                min_day_separation = 1, loss_of_load_penalty = 0.01, reserve_margin = 0.0,
                reserve_static_default_ratio = 0.0, verbose = false)
            model, mv = master1(inp2)
            @test ESFEX.JuMP.normalized_coefficient(
                constraint_by_name(model, "def_cap_bt_pow_y2_bt1_b1"),
                mv.bat_tech_power_investment[1][1][1]) ≈ -0.5
            @test ESFEX.JuMP.normalized_coefficient(
                constraint_by_name(model, "def_cap_bt_en_y2_bt1_b1"),
                mv.bat_tech_capacity_investment[1][1][1]) ≈ -0.5
            res = ESFEX.extract_master_solution(model, mv, inp2)
            b1 = res.bat_tech_power_investment[1][1][1]
            b2 = res.bat_tech_power_investment[2][1][1]
            @test b1 ≈ 20.0 rtol = 1e-6
            @test b2 ≈ 10.0 rtol = 1e-6
            @test res.cumulative_bat_tech_power[1][1][1] ≈ 20.0 rtol = 1e-6
            @test res.cumulative_bat_tech_power[2][1][1] ≈ 0.5 * b1 + b2 rtol = 1e-9
            @test res.cumulative_bat_tech_capacity[2][1][1] ≈
                  0.5 * res.bat_tech_capacity_investment[1][1][1] +
                  res.bat_tech_capacity_investment[2][1][1] rtol = 1e-9
            fixed = ESFEX.evaluate_fixed_capacities(mv, inp2, 2)
            @test fixed.bat_tech_power[(1, 1)] ≈ 0.5 * b1 + b2 rtol = 1e-9
            # A Benders cut prices the year-1 build at its share left
            cut = ESFEX.build_optimality_cut(1.0, Dict{Tuple{Int,Int}, Float64}(),
                Dict((1, 1) => 2.0), Dict((1, 1) => 3.0), fixed, inp2, 2, 1)
            @test cut.bat_tech_pow_gradients[(1001, 1)] ≈ 1.0
            @test cut.bat_tech_pow_gradients[(1002, 1)] ≈ 2.0
            @test cut.bat_tech_cap_gradients[(1001, 1)] ≈ 1.5
        end

        @testset "master holds MaxChargePower apart from the discharge power (round 44)" begin
            # 20 MW discharge, 0.5 MW charge: the dispatch recharges ~11 MWh
            # a day and sheds part of the spike; the master let it charge at
            # its discharge power, so it saw no shedding.
            slow(; kw...) = ESFEX.with_fields(mkbat(); max_charge_power = [0.5], kw...)
            d0, _, _ = bat_dispatch([mkbat()], spike)
            d1, _, _ = bat_dispatch([slow()], spike)
            m0, _ = master1(bat_master([mkbat()], spike))
            m1, _ = master1(bat_master([slow()], spike))
            dd = objective_value(d1) - objective_value(d0)
            @test dd > 1e-6
            @test objective_value(m1) - objective_value(m0) ≈ 365 * dd rtol = 1e-6

            # Degraded like the discharge power, as the dispatch does
            f = 0.95^10
            aged = slow(; degradation_rate = [0.05], initial_age = [10.0], life_time = [30.0])
            lim = ESFEX.battery_power_limits(aged, 1, (; year = 2025, base_year = 2025))
            model, _, _ = create_master_problem(bat_master([aged], spike);
                                                use_representative_days = true)
            @test only_bound(model, variable_by_name(model, "op_y1_d1_bat_ch[1,1,13]")) ≈
                  lim[2] ≈ 0.5 * f
            @test only_bound(model, variable_by_name(model, "op_y1_d1_bat_dch[1,1,13]")) ≈
                  lim[3] ≈ 20.0 * f
            @test ESFEX.battery_charge_ratio(aged, 1) ≈ 0.025
            @test ESFEX.battery_charge_ratio(mkbat(), 1) == 1.0

            # Life-extended: the charge power keeps its share of the MW kept,
            # as the runner scales MaxChargePower for the dispatch
            old = slow(; life_time = [5.0], initial_age = [10.0], invest_cost_power = [1.0])
            om, _, _ = create_master_problem(bat_master([old], spike);
                                             use_representative_days = true)
            ext = only(v for v in all_variables(om) if startswith(name(v), "bat_life_ext"))
            ch = variable_by_name(om, "op_y1_d1_bat_ch[1,1,13]")
            con = only(c for c in all_constraints(om, AffExpr, ESFEX.JuMP.MOI.LessThan{Float64})
                       if haskey(constraint_object(c).func.terms, ch) &&
                          length(constraint_object(c).func.terms) == 2)
            @test ESFEX.JuMP.normalized_coefficient(con, ext) ≈ -0.025
        end

        @testset "battery NPV: degraded revenue and retirement, fixed O&M on rated" begin
            f = 0.95^10
            bat = ESFEX.with_fields(mkbat(); degradation_rate = [0.05], initial_age = [10.0],
                                    life_time = [30.0], invest_cost_power = [1000.0],
                                    fixed_cost = [2.0], decommissioning_cost = [3.0])
            inp = bat_master([bat], spike; npv_annual_return_rate = 0.1)
            # Two later years of a myopic year's master (no discounting):
            # revenue on the power degraded on from the last year, fixed O&M
            # on the rated 20 MW as the master charges it
            op = ESFEX.UnitOperation(1.0, 0.0)
            v, years = ESFEX.extra_unit_npv(op, inp, :bat, 1, 1, 20.0 * f, 2)
            @test years == 2
            @test v ≈ sum(0.1 * 1000.0 * 20.0 * f * 0.95^k - 2.0 * 20.0 for k in 1:2)
            # Retired: decommissioned on its degraded power in the first year
            u = ESFEX.UnitNPV("battery", 1, 1, "", -1.0, 1.0, true)
            @test only(ESFEX.retire_units(inp, [u]).npv_retired_units).decommissioned_mw ≈
                  20.0 * f
        end

        @testset "TSAM seasonal storage counts each period as often as its weight (K44)" begin
            # Three TSAM periods standing for 300, 60 and 5 days: a dark day
            # with 30 MW of load on 1e-3 diesel, a neutral one, and a sunny
            # one (100 MW PV). The old chain linked each period once, in
            # chronological order, while its cost and energy balance counted
            # × weight: the battery charged on the 5-day period and
            # discharged the same energy on each of the 300 dark days (free
            # energy). Kotzur et al. (2018): the inter-day state runs through
            # all 365 days on the day → period map, so the weighted change of
            # the stored energy over the year is zero.
            H3 = 3 * H24
            w = [300.0, 60.0, 5.0]
            dmap = vcat(fill(1, 300), fill(2, 60), fill(3, 5))
            pv_av = zeros(H3, 1); pv_av[2 * H24 + 1:end, 1] .= 1.0
            pv = mkgen("PV", "Renewable", "Solar", [100.0], [0.0], pv_av; nb = 1, H = H3)
            diesel = mkgen("Diesel", "Non-renewable", "Diesel", [100.0], [1.0e-3],
                           ones(H3, 1); nb = 1, H = H3)
            sinp(gens, bats; kw...) = MasterProblemInput(;
                years = [2025], network = net1, generators = gens, batteries = bats,
                base_demand = fill(30.0, H3, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                loss_of_load_penalty = 0.01, reserve_margin = 0.0,
                reserve_static_default_ratio = 0.0, max_curtailment_ratio = 1.0,
                use_tsam = true, tsam_inter_period_linking = true,
                tsam_period_start_hours = [[1, H24 + 1, 2 * H24 + 1]],
                tsam_period_weights = [w], tsam_day_period = [dmap], verbose = false, kw...)
            per(m, pfx) = [sum(mvals(m, "op_y1_d$(k)_$(pfx)")) for k in 1:3]

            # Battery: weighted discharge = η_c·η_d × weighted charge
            bm, bv = master1(sinp([pv, diesel], [mkbat()]))
            ch, dch = w' * per(bm, "bat_ch["), w' * per(bm, "bat_dch[")
            @test ch > 1.0   # it does shift energy from the sunny days
            @test dch <= 0.95 * 0.95 * ch + 1e-6
            Δ = [value(variable_by_name(bm, "soc_delta_y1_b1_n1_p$k")) for k in 1:3]
            @test w' * Δ ≈ 0.0 atol = 1e-6
            # The inter-day state: one per day, year-cyclic, within [0, 100]
            S = [value(bv.inter_period_soc[1][(1, 1, d)]) for d in 0:364]
            @test length(bv.inter_period_soc[1]) == 365
            @test S[1] ≈ S[365] + Δ[dmap[365]] atol = 1e-6
            @test all(-1e-6 .<= S .<= 100.0 + 1e-6)

            # Reservoir: 100 MW-eq of inflow on the sunny period only; the
            # weighted turbine output cannot exceed the weighted inflow
            hydro = ESFEX.with_fields(
                mkgen("Hydro", "Renewable", "Water", [50.0], [0.0], ones(H3, 1); nb = 1, H = H3);
                reservoir_capacity = [5000.0], reservoir_initial_level = [0.5],
                reservoir_min_level = [0.0], reservoir_max_level = [1.0],
                reservoir_inflow = reshape(vcat(zeros(2 * H24), fill(100.0, H24)), H3, 1),
                reservoir_turbine_efficiency = [1.0], reservoir_evaporation_rate = [0.0],
                reservoir_pump_capacity = [0.0], reservoir_pump_efficiency = [1.0],
                reservoir_spillage_allowed = true)
            rm, rv = master1(sinp([hydro, diesel], BatteryConfig[]))
            turb = w' * per(rm, "gen[1,")
            inflow = 5.0 * 100.0 * H24
            @test turb > 1.0
            @test turb <= inflow + 1e-6
            Δr = [value(variable_by_name(rm, "reslvl_delta_y1_g1_n1_p$k")) for k in 1:3]
            @test w' * Δr ≈ 0.0 atol = 1e-6
            Sr = [value(rv.inter_period_reservoir[1][(1, 1, d)]) for d in 0:364]
            @test all(-1e-6 .<= Sr .<= 5000.0 + 1e-6)

            # Evaporation: the stored water decays day by day; the chain
            # carries ρ = (1 - e)^24 of each day's start level
            evap = ESFEX.with_fields(hydro; reservoir_evaporation_rate = [1.0e-4])
            em, ev = master1(sinp([evap, diesel], BatteryConfig[]))
            Se = [value(ev.inter_period_reservoir[1][(1, 1, d)]) for d in 0:364]
            Δe = [value(variable_by_name(em, "reslvl_delta_y1_g1_n1_p$k")) for k in 1:3]
            ρ = (1.0 - 1.0e-4)^H24
            @test all(isapprox(Se[mod1(d + 1, 365)], ρ * Se[d] + Δe[dmap[d]]; atol = 1e-6)
                      for d in 1:365)
            @test w' * per(em, "gen[1,") <= turb + 1e-6

            # A 30-day year of three day types in seasonal blocks (10 sunny,
            # 15 dark, 5 mild) and a 400 MWh battery: three periods with the
            # exact day map give the full year's plan (every day its own
            # period). The old chain gave a cost 27 % lower and an RE share
            # of 47.1 % against the year's 39.5 %.
            Dy = 30
            types = vcat(fill(1, 10), fill(2, 15), fill(3, 5))
            sunday = [max(0.0, sin(pi * (h - 6) / 12)) for h in 0:23]
            dayav = Dict(1 => sunday, 2 => 0.1 .* sunday, 3 => 0.5 .* sunday)
            yav = reshape(reduce(vcat, [dayav[t] for t in types]), Dy * H24, 1)
            ypv = mkgen("PV", "Renewable", "Solar", [120.0], [0.0], yav; nb = 1, H = Dy * H24)
            ydiesel = mkgen("Diesel", "Non-renewable", "Diesel", [100.0], [1.0e-3],
                            ones(Dy * H24, 1); nb = 1, H = Dy * H24)
            yinp(starts, weights, dmap) = MasterProblemInput(;
                years = [2025], network = net1, generators = [ypv, ydiesel],
                batteries = [mkbat(cap = 400.0)], base_demand = fill(30.0, Dy * H24, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                loss_of_load_penalty = 0.01, reserve_margin = 0.0,
                reserve_static_default_ratio = 0.0, max_curtailment_ratio = 1.0,
                hours_per_year = [Dy * H24], use_tsam = true, tsam_inter_period_linking = true,
                tsam_period_start_hours = [starts], tsam_period_weights = [weights],
                tsam_day_period = [dmap], verbose = false)
            function yplan(i)
                m, v = master1(i)
                return objective_value(m), extract_master_solution(m, v, i).re_penetration_by_year[1]
            end
            full = yplan(yinp([(d - 1) * H24 + 1 for d in 1:Dy], ones(Dy), collect(1:Dy)))
            agg = yplan(yinp([1, 10 * H24 + 1, 25 * H24 + 1], [10.0, 15.0, 5.0], types))
            @test agg[1] ≈ full[1] rtol = 1e-6
            @test agg[2] ≈ full[2] atol = 1e-6
            @test full[2] ≈ 0.3946 atol = 1e-3

            # The day map must describe the year the weights count
            bad(; kw...) = sinp([pv, diesel], [mkbat()]; kw...)
            msg(inp) = try
                create_master_problem(inp; use_representative_days = true); ""
            catch e
                sprint(showerror, e)
            end
            @test occursin("has 364 days; the year has 365",
                           msg(bad(tsam_day_period = [dmap[1:364]])))
            @test occursin("not one of the 3 periods",
                           msg(bad(tsam_day_period = [vcat(dmap[1:364], 4)])))
            @test occursin("stands for 300.0 days (its weight) but tsam_day_period gives it 299",
                           msg(bad(tsam_day_period = [vcat(dmap[1:299], 2, dmap[301:365])])))
            @test occursin("needs tsam_day_period",
                           msg(bad(tsam_day_period = Vector{Int}[])))
            # Without linking (or storage) no map is needed
            @test msg(bad(tsam_day_period = Vector{Int}[], tsam_inter_period_linking = false)) == ""
            @test msg(sinp([pv, diesel], BatteryConfig[]; tsam_day_period = Vector{Int}[])) == ""
        end

        @testset "battery technologies: fixed cost, ramp and availability (master, Benders)" begin
            o = [0.0]
            function li(; fixed = 0.0, ramp = 0.0, avail = zeros(0, 1))
                BatteryTechnologyConfig("LiNew", [1.0e-4], [1.0e-5], [50.0], [200.0], 1.0, 8.0,
                    [0.95], [0.95], o, [15.0], [0.5], [0.0], [1.0], o, o, [1.0e-7], false,
                    "AC", o, [1.0], [ramp], [ramp], [fixed], true, avail)
            end
            built(m) = sum(mvals(m, "bat_tech_pow_inv"))
            # The fixed cost enters each year's cost of the power built
            inp0 = bat_master(BatteryConfig[], spike; btechs = [li()],
                              reserve_static_default_ratio = 0.0)
            inp1 = bat_master(BatteryConfig[], spike; btechs = [li(fixed = 1.0e-3)],
                              reserve_static_default_ratio = 0.0)
            m0, _, _ = create_master_problem(inp0; use_representative_days = true)
            m1, _, _ = create_master_problem(inp1; use_representative_days = true)
            pv(m) = only(v for v in all_variables(m) if name(v) == "bat_tech_pow_inv_y1_bt1[1]")
            @test coefficient(objective_function(m1), pv(m1)) -
                  coefficient(objective_function(m0), pv(m0)) ≈ 1.0e-3 rtol = 1e-9

            # Availability bounds the battery the master builds as the
            # dispatch bounds it: the 20 MW spike takes 40 MW at 50 %.
            avail = reshape([t == 13 ? 0.5 : 1.0 for t in 1:H24], H24, 1)
            for (tech, mw) in ((li(), 20.0), (li(avail = avail), 40.0))
                m, _ = master1(bat_master(BatteryConfig[], spike; btechs = [tech],
                                          reserve_static_default_ratio = 0.0))
                @test built(m) ≈ mw rtol = 1e-6
            end
            # Benders reaches the monolithic optimum with every field set
            for tech in (li(fixed = 1.0e-4), li(avail = avail), li(ramp = 0.25)),
                    bats in (BatteryConfig[], [mkbat(ramp = 0.25, avail = avail)])
                inp = bat_master(bats, spike; btechs = [tech],
                                 reserve_static_requirement = Dict(1 => 5.0))
                model, _ = master1(inp)
                br = run_benders_decomposition(inp; max_iterations = 60, tolerance = 1e-7,
                                               verbose_benders = false)
                @test br.gap < 1e-6
                @test br.objective ≈ objective_value(model) rtol = 1e-6
            end
        end

        # ── Fixed O&M per MW-year (round 9, F9) ─────────────────────────────
        # Coal (100 MW, cheap) and gas (40 MW, dear) on one bus; gas pays
        # `fixed` per MW of power in service per year. It was charged per
        # MWh produced, so a unit that did not run paid nothing.
        fo_gas(fixed; kw...) = ESFEX.with_fields(
            mkgen("Gas", "Non-renewable", "Gas", [40.0], [3.0e-5], ones(H24, 1);
                  nb = 1, H = H24); fixed_cost = [fixed], kw...)
        fo_coal() = mkgen("Coal", "Non-renewable", "Coal", [100.0], [1.0e-5],
                          ones(H24, 1); nb = 1, H = H24)
        function fo_dispatch(gas, demand)
            input = PowerSystemInput(;
                name = "fo", year = 2025, network = net1, generators = [fo_coal(), gas],
                batteries = BatteryConfig[], demand = fill(demand, H24, 1),
                temporal = TemporalConfig(H24, 1, H24, 0, H24, H24, 1, 1, 1),
                loss_of_load_penalty = 0.01, reserve_static_default_ratio = 0.0,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test string(termination_status(model)) == "OPTIMAL"
            return model, vars, input
        end
        function fo_master(gas; techs = TechnologyConfig[], bats = BatteryConfig[],
                           demand = 30.0, kw...)
            return MasterProblemInput(;
                years = [2025], network = net1, generators = [fo_coal(), gas],
                batteries = bats, technologies = techs,
                base_demand = fill(demand, H24, 1), target_re_penetration = 0.0,
                initial_re_penetration = 0.0, representative_days_per_year = 1,
                min_day_separation = 1, loss_of_load_penalty = 0.01,
                reserve_static_default_ratio = 0.0, reserve_margin = 0.0,
                verbose = false, kw...)
        end
        fo_tech(fixed) = TechnologyConfig("GT", "Non-renewable", "Gas", [1.0e-3], [50.0],
            ones(H24, 1), [1.0], [1.0], [1.0], [1.0], [0.0], [0.0], [0.0], [2.0e-5],
            [fixed], [0.0], [0.0], [0.0], [25.0], [0.1], [0.0], 50.0, "AC", false, [1.0])

        @testset "generator fixed O&M per MW-year, whether or not it runs" begin
            fc = 1.0e-3
            window = fc * 40.0 * 24 / 8760
            for demand in (30.0, 120.0)        # gas idle, gas running
                m0, v0, i0 = fo_dispatch(fo_gas(0.0), demand)
                m1, v1, i1 = fo_dispatch(fo_gas(fc), demand)
                ran = sum(value.(v1.gen_output[2, 1, :]))
                @test (ran > 1.0) == (demand > 100.0)
                @test objective_value(m1) - objective_value(m0) ≈ window rtol = 1e-6
                res = extract_solution(m1, v1, i1)
                @test res.cost_breakdown.fixed_om_cost ≈ window rtol = 1e-6
                # Same dispatch: a constant does not change the operation
                out(v) = [value(v.gen_output[g, 1, t]) for g in 1:2, t in 1:H24]
                @test out(v1) ≈ out(v0) atol = 1e-6
            end
            # Degraded power in service (age 2 at 10 %/year), none past life
            for (age, life, mw) in ((2.0, 30.0, 40.0 * 0.9^2), (2.0, 1.0, 0.0))
                g = fo_gas(fc; degradation_rate = [0.1], initial_age = [age],
                           life_time = [life])
                m0, _, _ = fo_dispatch(ESFEX.with_fields(g; fixed_cost = [0.0]), 30.0)
                m1, _, _ = fo_dispatch(g, 30.0)
                @test objective_value(m1) - objective_value(m0) ≈ fc * mw * 24 / 8760 atol = 1e-10
            end
        end

        @testset "master fixed O&M per year = 8760/window × the dispatch's" begin
            fc = 1.0e-3
            # Existing unit, degraded unit: the year costs 365 × the day
            for kw in ((;), (; degradation_rate = [0.1], initial_age = [2.0]))
                d0, _, _ = fo_dispatch(fo_gas(0.0; kw...), 30.0)
                d1, _, _ = fo_dispatch(fo_gas(fc; kw...), 30.0)
                m0, _ = master1(fo_master(fo_gas(0.0; kw...)))
                m1, _ = master1(fo_master(fo_gas(fc; kw...)))
                dd = objective_value(d1) - objective_value(d0)
                @test dd > 0
                @test objective_value(m1) - objective_value(m0) ≈ 8760 / 24 * dd rtol = 1e-6
            end
            # Life-extended MW and technology MW built (degraded with age)
            # pay it per year; nothing in the day cost
            old = fo_gas(fc; life_time = [5.0], initial_age = [10.0], invest_cost = [1.0])
            inp = fo_master(old; techs = [fo_tech(fc)], years = [2025, 2026])
            model = ESFEX.Model()
            vars = ESFEX.build_master_variables!(model, inp)
            for y in 1:2
                cost = ESFEX.build_annual_asset_cost(vars, inp, y)
                lev = vars.gen_life_extension[y][2][1]
                @test coefficient(cost, lev) ≈ 1.0 * inp.life_extension_cost_factor + fc rtol = 1e-9
                @test coefficient(cost, vars.tech_investment[1][1][1]) ≈ fc * 0.9^(y - 1) rtol = 1e-9
            end
        end

        @testset "gen_fixed_cost scales generator, technology and battery fixed O&M" begin
            fc = 1.0e-3
            bat = mkbat(fixed = 2.0e-3)
            inp = fo_master(fo_gas(fc); techs = [fo_tech(fc)], bats = [bat])
            model = ESFEX.Model()
            vars = ESFEX.build_master_variables!(model, inp)
            base = ESFEX.build_annual_asset_cost(vars, inp, 1)
            twice = ESFEX.build_annual_asset_cost(vars, inp, 1;
                                                  mult = ScenarioMultipliers(gen_fixed_cost = 2.0))
            @test constant(base) ≈ fc * 40.0 + 2.0e-3 * 20.0 rtol = 1e-9
            @test constant(twice) ≈ 2 * constant(base) rtol = 1e-9
            vt = vars.tech_investment[1][1][1]
            @test coefficient(twice, vt) ≈ 2 * coefficient(base, vt) rtol = 1e-9
            # The scenario's operating input no longer carries it
            si = ESFEX.scenario_master_input(inp, ScenarioMultipliers(gen_fixed_cost = 2.0), 1)
            @test si.generators[2].fixed_cost == [fc]
            # End to end: the stochastic master pays twice the fixed O&M
            m1, _ = master1(fo_master(fo_gas(fc)))
            m0, _ = master1(fo_master(fo_gas(0.0)))
            m2, _ = master1(fo_master(fo_gas(fc)); mult = ScenarioMultipliers(gen_fixed_cost = 2.0))
            m2b, _ = master1(fo_master(fo_gas(0.0)); mult = ScenarioMultipliers(gen_fixed_cost = 2.0))
            @test objective_value(m2) - objective_value(m2b) ≈
                  2 * (objective_value(m1) - objective_value(m0)) rtol = 1e-6
        end

        @testset "Benders = monolithic with generator and technology fixed O&M" begin
            fc = 1.0e-3
            for (gas, techs, demand) in ((fo_gas(fc), [fo_tech(fc)], 160.0),
                                         (fo_gas(fc; life_time = [5.0], initial_age = [10.0],
                                                 invest_cost = [1.0e-3]), [fo_tech(fc)], 120.0))
                inp = fo_master(gas; techs = techs, demand = demand)
                model, _ = master1(inp)
                br = run_benders_decomposition(inp; max_iterations = 60, tolerance = 1e-7,
                                               verbose_benders = false)
                @test br.gap < 1e-6
                @test br.objective ≈ objective_value(model) rtol = 1e-6
            end
        end

        @testset "non-renewable technology output follows its availability (master, Benders)" begin
            # 160 MW against 140 MW existing: a gas technology (dearer to run
            # than the existing units) available at 50 % must build 40 MW to
            # give 20. The master capped a non-renewable technology at its
            # built capacity × 1.0, while the dispatch caps the generator it
            # becomes by its profile.
            gt(avail) = TechnologyConfig("GT", "Non-renewable", "Gas", [1.0e-3], [100.0],
                avail, [1.0], [1.0], [1.0], [1.0], [0.0], [0.0], [0.0], [5.0e-5],
                [0.0], [0.0], [0.0], [0.0], [25.0], [0.0], [0.0], 50.0, "AC", false, [1.0])
            for (avail, mw) in ((ones(H24, 1), 20.0), (fill(0.5, H24, 1), 40.0))
                inp = fo_master(fo_gas(0.0); techs = [gt(avail)], demand = 160.0)
                model, _ = master1(inp)
                @test sum(mvals(model, "tech_inv")) ≈ mw rtol = 1e-6
                br = run_benders_decomposition(inp; max_iterations = 60, tolerance = 1e-7,
                                               verbose_benders = false)
                @test br.gap < 1e-6
                @test br.objective ≈ objective_value(model) rtol = 1e-6
            end
        end

        @testset "Benders master stopped at a limit" begin
            # A master that stops at a limit with a feasible plan is used;
            # its objective is no lower bound. It used to end the iterations.
            MOI = ESFEX.MOI
            m = ESFEX.Model(create_optimizer(solver_name = "highs"))
            ESFEX.set_silent(m)
            ESFEX.set_attribute(m, "mip_max_improving_sols", 1)
            ESFEX.set_attribute(m, "presolve", "off")
            n = 30
            w = [mod(7i, 13) + 3 for i in 1:n]
            v = [mod(11i, 17) + 2 for i in 1:n]
            x = ESFEX.@variable(m, [1:n], Bin)
            ESFEX.@constraint(m, sum(w[i] * x[i] for i in 1:n) <= 60)
            ESFEX.@objective(m, Min, -sum(v[i] * x[i] for i in 1:n))
            optimize!(m)
            @test termination_status(m) == MOI.SOLUTION_LIMIT
            @test ESFEX.limit_with_feasible_point(m)
            bound = ESFEX.objective_bound(m)
            @test bound <= objective_value(m)
            # The MIP bound, or the previous lower bound when it is higher
            @test ESFEX.limited_master_lower_bound(m, Float64[]) ≈ bound
            @test ESFEX.limited_master_lower_bound(m, [bound + 1.0]) ≈ bound + 1.0

            # An LP stopped by the simplex iteration limit: no feasible
            # primal point, but a feasible dual one bounds the optimum
            lp = ESFEX.Model(create_optimizer(solver_name = "highs"))
            ESFEX.set_silent(lp)
            ESFEX.set_attribute(lp, "simplex_iteration_limit", 1)
            ESFEX.set_attribute(lp, "presolve", "off")
            y = ESFEX.@variable(lp, [1:n], lower_bound = 0)
            ESFEX.@constraint(lp, [i in 1:n], y[i] >= i)
            ESFEX.@constraint(lp, sum(y) >= 1000)
            ESFEX.@objective(lp, Min, sum(i * y[i] for i in 1:n))
            optimize!(lp)
            @test termination_status(lp) == MOI.ITERATION_LIMIT
            @test !ESFEX.limit_with_feasible_point(lp)
            if ESFEX.dual_status(lp) == MOI.FEASIBLE_POINT
                @test ESFEX.limited_master_lower_bound(lp, [-1.0e9]) ≈
                      ESFEX.dual_objective_value(lp)
            end
            @test ESFEX.limited_master_lower_bound(lp, [1.0e12]) == 1.0e12
        end

        @testset "Benders: year subproblems, operating bound, battery floor (B25)" begin
            # Cuba (25 years, 5 days, 136 buses): θ stayed 0 and the gap at
            # 99.9 %. The master shared each year's RE target out among the
            # days and moved the shares for free; with no bound on θ it also
            # moved batteries it had paid for to buses where a degenerate
            # dual valued power alone at VOLL. Two buses, three years, five
            # days, a growth-capped RE target, a capacity deficit, PV,
            # engines and batteries.
            ny, nd = 3, 5
            Hy = ny * 8760
            buses2 = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                      BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            line = TransmissionLineData("l1", 1, 2, 10.0, 0.1, 0.0, 0.0, 30.0, 220.0, 1, 50.0,
                                        "AC", 0.0)
            netb = NetworkConfig(2, 2, buses2, [1, 2], [0 10.0; 10.0 0], zeros(2, 2), 100.0,
                0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0], [line], TransformerData[],
                ACDCConverterData[], FrequencyConverterData[], 0.1)
            dayk(h) = mod((h - 1) ÷ 24, 7) / 2
            dem = hcat([30.0 + 5.0 * dayk(h) for h in 1:Hy], [25.0 + 6.0 * (3 - dayk(h)) for h in 1:Hy])
            sunf(h, b) = (6 <= mod(h - 1, 24) < 18 ? 1.0 : 0.0) *
                         (b == 1 ? 0.3 + 0.2 * dayk(h) : 0.9 - 0.2 * dayk(h))
            sun = [sunf(h, b) for h in 1:Hy, b in 1:2]
            o2 = [0.0, 0.0]
            pvb = TechnologyConfig("PV", "Renewable", "Sun", [1.0, 1.0], [500.0, 500.0], sun,
                [1.0, 1.0], [1.0, 1.0], [1.0, 1.0], [1.0, 1.0], o2, o2, o2, o2, o2, o2, o2, o2,
                [25.0, 25.0], o2, o2, 50.0, "AC", false, [1.0, 1.0])
            engb = TechnologyConfig("Eng", "Non-renewable", "Oil", [1.0, 1.0], [300.0, 300.0],
                ones(Hy, 2), [1.0, 1.0], [1.0, 1.0], [1.0, 1.0], [1.0, 1.0],
                o2, o2, o2, [8.0e-5, 8.0e-5], o2, o2, o2, o2, [25.0, 25.0], o2, o2, 50.0, "AC",
                false, [1.0, 1.0])
            lib = BatteryTechnologyConfig("LiNew", [0.15, 0.15], [0.2, 0.2], [50.0, 50.0],
                [200.0, 200.0], 2.0, 8.0, [0.95, 0.95], [0.95, 0.95], o2, [15.0, 15.0],
                [0.5, 0.5], [0.1, 0.1], [1.0, 1.0], o2, o2, [1.0e-6, 1.0e-6], false,
                "AC", o2, [1.0, 1.0], o2, o2, o2, true, zeros(0, 2))
            gensb = [mkgen("Gas", "Non-renewable", "Gas", [45.0, 20.0], [1.0e-4, 1.2e-4],
                           ones(Hy, 2); nb = 2, H = Hy),
                     mkgen("PVold", "Renewable", "Sun", [4.0, 4.0], [0.0, 0.0], sun; nb = 2, H = Hy)]
            inp = MasterProblemInput(years = collect(2025:(2024 + ny)), network = netb,
                generators = gensb, batteries = BatteryConfig[], technologies = [pvb, engb],
                battery_technologies = [lib], base_demand = dem,
                target_re_penetration = 1.0, initial_re_penetration = 0.05,
                min_re_increment = 0.01, max_re_increment = 0.10,
                fre_penetration_loss_penalty = 1e-4, representative_days_per_year = nd,
                min_day_separation = 1, loss_of_load_penalty = 0.0075,
                max_curtailment_ratio = 0.05, verbose = false)
            mono, _ = master1(inp)

            # The master's first solve already holds an operating cost: θ of
            # each year is at least a copper-plate relaxation of the year
            # (it was 0 until the first cuts), and the bound is a valid one
            bm, _, _, btheta, _ = ESFEX.create_benders_master(inp)
            ESFEX.set_silent(bm)
            optimize!(bm)
            @test string(termination_status(bm)) == "OPTIMAL"
            @test sum(value(t) for t in values(btheta)) > 0.1 * objective_value(mono)
            @test objective_value(bm) <= objective_value(mono) * (1 + 1e-9)

            # One subproblem (and one θ) per year: no per-day master shares
            @test length(btheta) == ny
            @test !any(n -> occursin("re_share_y", n) || occursin("co2_alloc", n),
                       name.(all_variables(bm)))

            br = run_benders_decomposition(inp; max_iterations = 60, tolerance = 1e-7,
                                           verbose_benders = false)
            @test br.gap < 1e-6
            @test br.objective ≈ objective_value(mono) rtol = 1e-6
            # LB ≤ UB, up to the value of the battery floor's sliver
            @test all(br.lb_history .<= br.ub_history .* (1 + 1e-6))

            # The battery floor: every bus where a battery can be built sees
            # a sliver of one, and nothing else changes
            fc = ESFEX.FixedCapacities(Dict((1, 1) => 45.0), Dict{Tuple{Int,Int}, Float64}(),
                Dict{Tuple{Int,Int}, Float64}(), Dict((1, 1) => 3.0),
                Dict((1, 2) => 7.0), Dict((1, 2) => 14.0), Dict{Int, Float64}(), [0.1])
            ff = ESFEX.benders_battery_floor(fc, inp)
            @test ff.bat_tech_power == Dict((1, 1) => ESFEX.BENDERS_BATTERY_FLOOR, (1, 2) => 7.0)
            @test ff.bat_tech_energy == Dict((1, 1) => 2 * ESFEX.BENDERS_BATTERY_FLOOR,
                                             (1, 2) => 14.0)
            @test ff.tech_capacity == fc.tech_capacity && ff.gen_capacity == fc.gen_capacity
        end

        @testset "Benders: curtailment limit in the bound, priced subproblems (B26)" begin
            # Cuba (25 years): the master's copper-plate bound curtailed for
            # free, so it built more renewables than the years could absorb
            # within the 5 % limit; those years only gave feasibility cuts,
            # more of them each iteration, and the bounds stalled. Cheap PV,
            # dear gas, one bus: within the limit PV stops at 700/13 MW.
            o = [0.0]
            sun = reshape(vcat(zeros(6), [0.25, 0.5, 0.75], ones(6), [0.75, 0.5, 0.25],
                               zeros(6)), H24, 1)
            pv = TechnologyConfig("PV", "Renewable", "Sun", [0.001], [1000.0], sun,
                                  [1.0], [1.0], [1.0], [1.0], o, o, o, o, o, o, o, o,
                                  [25.0], o, o, 50.0, "AC", false, [1.0])
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-3], ones(H24, 1);
                        nb = 1, H = H24)
            inp = MasterProblemInput(years = [2025], network = net1, generators = [gas],
                batteries = BatteryConfig[], technologies = [pv],
                base_demand = fill(50.0, H24, 1), target_re_penetration = 0.0,
                initial_re_penetration = 0.0, max_curtailment_ratio = 0.05,
                curtailment_cost = 1.0e-5, representative_days_per_year = 1,
                min_day_separation = 1, verbose = false)

            # The bound holds the limit: the first master plan builds no more
            # PV than the day can use (it built 200 MW), up to the
            # subproblems' tolerance (`curtailment_tolerance`)
            bm, bvars, targets, _, _ = ESFEX.create_benders_master(inp)
            ESFEX.set_silent(bm)
            optimize!(bm)
            @test value(bvars.tech_investment[1][1][1]) <= 700 / 13 * (1 + 1e-5)

            # A plan above the limit: the priced subproblem curtails no more
            # than it must, and its cut and cost are kept; the least
            # violation then gives the feasibility cut
            days = ESFEX.benders_rep_days(inp)[1]
            caps(mw) = ESFEX.FixedCapacities(Dict((1, 1) => 100.0),
                Dict{Tuple{Int,Int}, Float64}(), Dict{Tuple{Int,Int}, Float64}(),
                Dict((1, 1) => mw), Dict{Tuple{Int,Int}, Float64}(),
                Dict{Tuple{Int,Int}, Float64}(), Dict{Int, Float64}(), [0.0])
            ysp(mw) = ESFEX.create_benders_year_subproblem(inp, 1, days, caps(mw), bvars, targets)
            pen = ESFEX.benders_curtailment_penalty(inp)
            hard = ysp(100.0)
            @test ESFEX.solve_benders_subproblem!(hard) === :infeasible
            least = objective_value(hard.model)
            @test least > 1.0
            priced = ysp(100.0)
            @test ESFEX.solve_benders_subproblem!(priced; penalty = pen) === :infeasible
            @test sum(value, ESFEX.benders_curt_slacks(priced)) ≈ least rtol = 1e-6
            @test occursin("breaks the annual curtailment limit: curtails",
                           ESFEX.benders_curtailment_report(priced, 0.05))
            @test ESFEX.benders_cost_terms(priced.model)["curtailment above the limit"] ≈
                  pen * least * ESFEX.rep_day_scaling(inp, 1) rtol = 1e-6
            @test ESFEX.benders_least_violation!(priced)
            @test objective_value(priced.model) ≈ least rtol = 1e-6
            # Within the limit, priced and hard subproblems are the same
            for (a, b) in ((ysp(40.0), ysp(40.0)),)
                @test ESFEX.solve_benders_subproblem!(a) === :optimal
                @test ESFEX.solve_benders_subproblem!(b; penalty = pen) === :optimal
                @test objective_value(b.model) ≈ objective_value(a.model) rtol = 1e-9
            end

            # In-out stabilisation: some iterations solve the subproblems
            # halfway between the master's plan and the previous point; the
            # run still reaches the monolithic optimum and returns its plan
            mono, mvars = master1(inp)
            local br
            @test_logs (:info, r"solved between the master plan") match_mode = :any begin
                br = run_benders_decomposition(inp; max_iterations = 40, tolerance = 1e-7,
                                               verbose_benders = true)
            end
            @test br.gap < 1e-6
            @test br.objective ≈ objective_value(mono) rtol = 1e-6
            @test br.solution.tech_investment[1][1][1] ≈ value(mvars.tech_investment[1][1][1]) rtol = 1e-6
            @test all(br.lb_history .<= br.ub_history .* (1 + 1e-6))
        end

        @testset "Benders: in-out core within the curtailment limit (S29)" begin
            # Cuba's 25-year run held its upper bound from iteration 10 to
            # 50: the core moved to every point solved, also one above the
            # curtailment limit, so the points between it and a plan within
            # the limit were above it too and gave no bound. Four one-day
            # years, load growing at bus 1, the wind cheaper at bus 2 behind
            # a 30 MW line: the copper-plate bound builds more there than the
            # line lets the years use within 5 %. The first bound came at
            # iteration 14 of 18; now at 6 of 10.
            NY = 4
            H = 24 * NY
            buses2 = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                      BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            line = TransmissionLineData("l1", 1, 2, 30.0, 0.1, 0.0, 0.0, 100.0, 220.0, 1,
                                        50.0, "AC", 0.0)
            net2 = NetworkConfig(2, 2, buses2, [1, 2], [0 30.0; 30.0 0], zeros(2, 2), 100.0,
                0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0], [line], TransformerData[],
                ACDCConverterData[], FrequencyConverterData[], 0.1)
            o = [0.0, 0.0]
            l = [1.0, 1.0]
            sun = repeat(vcat(zeros(6), [0.25, 0.5, 0.75], ones(6), [0.75, 0.5, 0.25],
                              zeros(6)), NY)
            wind = repeat(0.5 .+ 0.4 .* sin.(range(0, 2pi, length = 24)), NY)
            pv = TechnologyConfig("PV", "Renewable", "Sun", [0.001, 0.001], [0.0, 1000.0],
                hcat(sun, sun), l, l, l, l, o, o, o, o, o, o, o, o, [25.0, 25.0], o, o, 50.0,
                "AC", false, l)
            wd = TechnologyConfig("Wind", "Renewable", "Wind", [0.0016, 0.0013],
                [1000.0, 1000.0], hcat(wind, wind), l, l, l, l, o, o, o, o, o, o, o, o,
                [25.0, 25.0], o, o, 50.0, "AC", false, l)
            gas = mkgen("Gas", "Non-renewable", "Gas", [200.0, 0.0], [1.0e-3, 0.0],
                        ones(H, 2); nb = 2, H = H)
            inp = MasterProblemInput(years = collect(2025:2024 + NY), network = net2,
                generators = [gas], batteries = BatteryConfig[], technologies = [pv, wd],
                base_demand = hcat(vcat([fill(60.0 * (1 + 0.15k), 24) for k in 0:NY-1]...),
                                   fill(15.0, H)),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                max_curtailment_ratio = 0.05, curtailment_cost = 1.0e-5,
                representative_days_per_year = 1, min_day_separation = 1,
                hours_per_year = fill(24, NY), rep_day_offsets = [0],
                transmission_loss_segments = 0, verbose = false)
            mono, _ = master1(inp)
            br = run_benders_decomposition(inp; max_iterations = 40, tolerance = 1e-6,
                                           verbose_benders = false)
            @test br.gap < 1e-6
            @test br.objective ≈ objective_value(mono) rtol = 1e-6
            @test findfirst(isfinite, br.ub_history) <= 8
            @test br.iterations <= 12

            # The best plan is recovered when a later feasibility cut cuts it
            # off by more than the solver's tolerance (Cuba aborted there)
            m = ESFEX.JuMP.Model(ESFEX.HiGHS.Optimizer)
            ESFEX.JuMP.set_silent(m)
            ESFEX.JuMP.@variable(m, 0 <= x <= 10)
            ESFEX.JuMP.@variable(m, t >= 0)
            ESFEX.JuMP.@constraint(m, t >= 5 - x)
            ESFEX.JuMP.@constraint(m, 1.0e7 * x <= 1.0e7 * 0.5 - 1.0,
                                   base_name = "benders_feas_y1_iter2")
            ESFEX.JuMP.@objective(m, Min, x + t)
            opt = ESFEX.JuMP.optimizer_with_attributes(ESFEX.HiGHS.Optimizer,
                                                       "output_flag" => false)
            @test_logs (:warn, r"without the feasibility cuts") match_mode = :any @test(
                ESFEX.recover_benders_plan!(m, Dict(x => 0.5), opt))
            @test value(x) ≈ 0.5 rtol = 1e-5
            @test value(t) ≈ 4.5 rtol = 1e-5
            @test ESFEX.JuMP.constraint_by_name(m, "benders_feas_y1_iter2") === nothing
            # A plan the master holds is fixed as it is
            m2 = ESFEX.JuMP.Model(ESFEX.HiGHS.Optimizer)
            ESFEX.JuMP.@variable(m2, 0 <= y <= 10)
            ESFEX.JuMP.@objective(m2, Min, y)
            @test ESFEX.recover_benders_plan!(m2, Dict(y => 3.0), opt)
            @test value(y) == 3.0
        end

        @testset "Curtailment limit: annual, per system, with a tolerance (C28)" begin
            # The limit held every representative day: Cuba's Benders run
            # flagged a year curtailing 226.7 GWh against a 580.8 GWh limit
            # because one of its five days curtailed more than 5 %. It is
            # annual now. Two days, 50 then 100 MW of load, 60 MW of
            # existing PV in full sun: day 1 must curtail 10 MW (20 % of its
            # renewable output), day 2 nothing; the year 240/2640 = 9.1 %,
            # within an annual 10 %.
            Hy = 8760
            dem = vcat(fill(50.0, 24, 1), fill(100.0, Hy - 24, 1))
            gas = mkgen("Gas", "Non-renewable", "Gas", [150.0], [1.0e-4], ones(Hy, 1);
                        nb = 1, H = Hy)
            pvold = mkgen("PVold", "Renewable", "Sun", [60.0], [0.0], ones(Hy, 1);
                          nb = 1, H = Hy)
            mk(; kw...) = MasterProblemInput(; years = [2025], network = net1,
                generators = [gas, pvold], batteries = BatteryConfig[],
                technologies = TechnologyConfig[], base_demand = dem,
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                max_curtailment_ratio = 0.10, curtailment_cost = 1.0e-5,
                representative_days_per_year = 2, rep_day_offsets = [0, 24],
                min_day_separation = 1, verbose = false, kw...)
            inp = mk()
            mono, _ = master1(inp)
            @test constraint_by_name(mono, "max_curtailment_s1_y1") !== nothing
            curt = sum(value(v) for v in all_variables(mono)
                       if occursin(r"^op_y1_d\d+_curt\[", name(v)))
            @test curt ≈ 240.0 atol = 1e-6
            # Stochastic: per scenario
            sc = [Scenario("lo", 0.5, ScenarioMultipliers(fuel_cost = 1.0)),
                  Scenario("hi", 0.5, ScenarioMultipliers(fuel_cost = 2.0))]
            sm, _, _, _ = create_stochastic_master_problem(StochasticMasterInput(inp, sc, true))
            ESFEX.set_silent(sm)
            optimize!(sm)
            @test string(termination_status(sm)) == "OPTIMAL"
            @test constraint_by_name(sm, "max_curtailment_sc2_s1_y1") !== nothing
            # TSAM periods count with their weights: day 1 standing for 65
            # days curtails 3.1 % of the year's renewable output, for 300
            # days 15.9 %
            tsam(w) = mk(use_tsam = true, tsam_period_start_hours = [[1, 25]],
                         tsam_period_weights = [w])
            master1(tsam([65.0, 300.0]))
            # Day 1 standing for 300 days curtails 72 000 MWh/yr against a
            # limit of 45 360 (10 % of 453 600 MWh/yr): the existing PV
            # cannot be held within it. The master was INFEASIBLE (a hard
            # limit; a loss-repaired master then kept its PWL solution). The
            # excess is priced now (round 39): the master solves, pays the
            # 26 640 MWh/yr above the limit and warns
            tm, _, _ = create_master_problem(tsam([300.0, 65.0]); use_representative_days = true)
            ESFEX.set_silent(tm)
            @test_logs (:warn, r"system global, year 2025 curtails 26\.64 GWh/yr above its annual curtailment limit \(max_curtailment_ratio = 0\.1 of 453\.6 GWh/yr") match_mode = :any ESFEX.optimize_master!(tm)
            @test string(termination_status(tm)) == "OPTIMAL"
            ex = only(ESFEX.curtailment_excess(tm))
            @test ex.excess_mwh ≈ 26640.0 rtol = 1e-6
            @test ex.re_mwh ≈ 453600.0 rtol = 1e-6
            @test (ex.system, ex.year, ex.scenario) == ("global", 2025, 0)
            exc = only(tm.ext[:curtailment_excess]).excess
            @test name(exc) == "curtailment_excess_s1_y1"
            # Priced in the objective at curtailment_excess_penalty (its floor
            # CURTAILMENT_EXCESS_PENALTY, 10⁴ $/MWh)
            obj = objective_value(tm)
            fix(exc, 26640.0 + 1000.0; force = true)
            optimize!(tm)
            @test objective_value(tm) - obj ≈ 1000.0 * ESFEX.CURTAILMENT_EXCESS_PENALTY rtol = 1e-6
            # MGA / SPORES alternatives curtail above it no more than the
            # cost-optimal plan (as under a hard limit)
            ESFEX.JuMP.unfix(exc)
            ESFEX.JuMP.set_lower_bound(exc, 0.0)
            optimize!(tm)
            ESFEX.hold_curtailment_excess!(tm)
            @test ESFEX.JuMP.upper_bound(exc) ≈ 26640.0 + ESFEX.curtailment_tolerance(453600.0) rtol = 1e-6
            mga = ESFEX.run_mga_spores(tsam([300.0, 65.0]); num_alternatives = 1,
                                       slack_fraction = 0.05)
            @test length(mga.alternatives) == 2
            # Stochastic: per scenario, named in the warning
            ssm, _, _, _ = create_stochastic_master_problem(StochasticMasterInput(
                tsam([300.0, 65.0]), sc, true))
            ESFEX.set_silent(ssm)
            @test_logs (:warn, r"year 2025, scenario 2 curtails 26\.64 GWh/yr") match_mode = :any ESFEX.optimize_master!(ssm)
            @test string(termination_status(ssm)) == "OPTIMAL"
            @test [e.scenario for e in ESFEX.curtailment_excess(ssm)] == [1, 2]
            @test constraint_by_name(ssm, "max_curtailment_sc2_s1_y1") !== nothing
            # Within the limit nothing changes: no excess, and the objective
            # of the hard limit (the excess fixed at 0)
            @test isempty(ESFEX.curtailment_excess(mono))
            mobj = objective_value(mono)
            foreach(e -> fix(e.excess, 0.0; force = true), mono.ext[:curtailment_excess])
            optimize!(mono)
            @test objective_value(mono) ≈ mobj rtol = 1e-9
            # Benders: the year is within its limit, same optimum
            br = run_benders_decomposition(inp; max_iterations = 20, tolerance = 1e-7,
                                           verbose_benders = false)
            @test br.objective ≈ objective_value(mono) rtol = 1e-6

            # Tolerance: a year over its limit by 0.05 MWh/yr (2.6e-4 per
            # day) counts as within it (the slack once had to stay under
            # 1e-6), up to 1e-3 MWh/yr + 1e-6 × its renewable output
            @test ESFEX.curtailment_tolerance(1.0e6) ≈ 1e-3 + 1.0
            bm, bvars, targets, _, _ = ESFEX.create_benders_master(inp)
            days = ESFEX.benders_rep_days(inp)[1]
            caps = ESFEX.FixedCapacities(Dict((1, 1) => 150.0, (2, 1) => 60.0),
                Dict{Tuple{Int,Int}, Float64}(), Dict{Tuple{Int,Int}, Float64}(),
                Dict{Tuple{Int,Int}, Float64}(), Dict{Tuple{Int,Int}, Float64}(),
                Dict{Tuple{Int,Int}, Float64}(), Dict{Int, Float64}(), [0.0])
            tight = mk(max_curtailment_ratio = 240 / 2640 - 1e-7)
            ysp() = ESFEX.create_benders_year_subproblem(tight, 1, days, caps, bvars, targets)
            pen = ESFEX.benders_curtailment_penalty(tight)
            y1 = ysp()
            @test ESFEX.solve_benders_subproblem!(y1; penalty = pen) === :optimal
            x = only(ESFEX.benders_curt_slacks(y1))
            @test 0 < value(x) * ESFEX.rep_day_scaling(tight, 2) < 0.1
            @test !ESFEX.benders_curt_violated(y1)
            # Hard: its least violation is within, and the year solves
            @test ESFEX.solve_benders_subproblem!(ysp()) === :optimal
            # Well above the limit it is not within
            far = mk(max_curtailment_ratio = 0.05)
            y2 = ESFEX.create_benders_year_subproblem(far, 1, days, caps, bvars, targets)
            @test ESFEX.solve_benders_subproblem!(y2; penalty = pen) === :infeasible
            @test occursin("breaks the annual curtailment limit: curtails 43.8 GWh/yr",
                           ESFEX.benders_curtailment_report(y2, 0.05))
        end

        @testset "Curtailment excess beats the RE shortfall price (M44)" begin
            # The excess above the annual limit was priced at a fixed 10⁴ $/MWh:
            # with a dearer RE shortfall price (fre_penetration_loss above it,
            # or a stochastic scenario's multiplier) the master curtailed above
            # the limit to meet its RE target. One day, 50 MW of load, cheap
            # candidate PV under a 12-hour sun and a 50 % RE target: meeting it
            # takes 200 MW of PV curtailing two thirds of its output. Within
            # the 5 % limit PV stops near 50 MW and the shortfall is paid.
            o = [0.0]
            sun = reshape(vcat(zeros(6), [0.25, 0.5, 0.75], ones(6), [0.75, 0.5, 0.25],
                               zeros(6)), H24, 1)
            pv = TechnologyConfig("PV", "Renewable", "Sun", [1.0e-3], [1000.0], sun,
                                  [1.0], [1.0], [1.0], [1.0], o, o, o, o, o, o, o, o,
                                  [25.0], o, o, 50.0, "AC", false, [1.0])
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4], ones(H24, 1);
                        nb = 1, H = H24)
            mk(fre) = MasterProblemInput(years = [2025], network = net1, generators = [gas],
                batteries = BatteryConfig[], technologies = [pv],
                base_demand = fill(50.0, H24, 1), target_re_penetration = 0.5,
                initial_re_penetration = 0.5, re_target_override = Dict((1, 1) => 0.5),
                representative_days_per_year = 1, min_day_separation = 1,
                fre_penetration_loss_penalty = fre, curtailment_cost = 1.0e-5,
                reserve_static_default_ratio = 0.0, max_curtailment_ratio = 0.05,
                verbose = false)
            # 1e6 $/MWh of shortfall: the excess costs 100 × that
            @test ESFEX.curtailment_excess_penalty(mk(1.0)) ≈ 100.0
            @test ESFEX.curtailment_excess_penalty(mk(1.0e-4)) == ESFEX.CURTAILMENT_EXCESS_PENALTY
            for (fre, mult) in ((1.0, nothing),
                                (1.0e-3, ScenarioMultipliers(penalty_fre_penetration = 1000.0)))
                m, v = master1(mk(fre); mult)
                @test isempty(ESFEX.curtailment_excess(m))
                @test value(v.tech_investment[1][1][1]) < 60.0
            end
        end

        @testset "RE share = renewable / total generation everywhere (R27)" begin
            # The master, Benders and the dispatch target measured the RE
            # share on demand (renewable energy used / demand): load not
            # served counted against it, and storage losses let it pass
            # 100 % while fossil units ran (Cuba 102–105 %). It is renewable
            # over total generation now: 100 % only without fossil
            # generation. 40 MW of PV against 100 MW of load; when shedding
            # is cheaper than gas only the PV runs.
            pv = mkgen("PV", "Renewable", "Sun", [40.0], [0.0], ones(H24, 1); nb = 1, H = H24)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4], ones(H24, 1);
                        nb = 1, H = H24)
            mk(voll) = MasterProblemInput(years = [2025], network = net1,
                generators = [pv, gas], batteries = BatteryConfig[],
                base_demand = fill(100.0, H24, 1), target_re_penetration = 0.8,
                initial_re_penetration = 0.8, re_target_override = Dict((1, 1) => 0.8),
                representative_days_per_year = 1, min_day_separation = 1,
                loss_of_load_penalty = voll, fre_penetration_loss_penalty = 1.0e-3,
                reserve_static_default_ratio = 0.0, max_curtailment_ratio = 1.0,
                verbose = false)
            for (voll, share, short) in ((1.0e-5, 1.0, 0.0), (10.0, 0.4, 40.0))
                inp = mk(voll)
                m, v = master1(inp)
                res = extract_master_solution(m, v, inp)
                @test res.re_penetration_by_year[1] ≈ share atol = 1e-6
                @test res.re_penetration_by_system["global"][1] ≈ share atol = 1e-6
                # Shortfall (MWh/year) against 80 % of the generation
                @test value(v.slack_re_target[(0, 1, 1)]) ≈ short * 8760 rtol = 1e-6 atol = 1e-3
                br = run_benders_decomposition(inp; max_iterations = 30, tolerance = 1e-7,
                                               verbose_benders = false)
                @test br.objective ≈ objective_value(m) rtol = 1e-6
                @test br.solution.re_penetration_by_year[1] ≈ share atol = 1e-4
                # The dispatch target alike (development mode)
                dinp = PowerSystemInput(name = "re", year = 2025, network = net1,
                    generators = [pv, gas], batteries = BatteryConfig[],
                    demand = fill(100.0, H24, 1),
                    temporal = TemporalConfig(H24, 1, H24, 0, H24, H24, 1, 1, 1),
                    re_penetration_target = 0.8, loss_of_load_penalty = voll,
                    fre_penetration_penalty = 1.0e-3,
                    mode = "economic_dispatch", solver_name = "highs", verbose = false)
                dm, dv = create_power_system(dinp)
                optimize!(dm)
                @test string(termination_status(dm)) == "OPTIMAL"
                @test extract_solution(dm, dv, dinp).re_penetration ≈ share atol = 1e-6
                @test sum(value.(dv.fre_penetration_loss)) ≈ short * H24 atol = 1e-6
            end
            # The growth cap's allowance: a share times this year's
            # generation, or last year's renewable generation scaled by the
            # demand growth
            @test ESFEX.re_cap_prev_allowance(0.3, 200.0, 100.0) ≈ 60.0
            @test ESFEX.re_cap_prev_allowance((50.0, 80.0), 200.0, 100.0) ≈ 62.5
        end

        @testset "master: scheduled outages on the representative days (R27)" begin
            # Cuba 2025: pv_2025 (729 MW) is out until each park is
            # commissioned (full-year potential 1.18 TWh, 0.58 TWh with the
            # outages) and ps_suheyla from hour 5112. The dispatch folds the
            # outage_schedule into availability; the master ignored it, so
            # it counted 1.18 TWh (RE 15.0 % against the dispatch's 12.1 %).
            H = 8760
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-5], ones(H, 1);
                        nb = 1, H = H)
            oil = mkgen("Oil", "Non-renewable", "Oil", [100.0], [1.0e-4], ones(H, 1);
                        nb = 1, H = H)
            pv = mkgen("PV", "Renewable", "Sun", [30.0], [0.0], ones(H, 1); nb = 1, H = H)
            half = ones(H); half[1:24] .= 0.5          # the whole unit at 50 %
            dark = ones(H); dark[1:12] .= 0.0          # at its node (1), first 12 h
            inp = MasterProblemInput(years = [2025], network = net1,
                generators = [gas, oil, pv], batteries = BatteryConfig[mkbat()],
                base_demand = fill(100.0, H, 1), target_re_penetration = 0.0,
                initial_re_penetration = 0.0, representative_days_per_year = 1,
                min_day_separation = 1, rep_day_offsets = [0],
                reserve_static_default_ratio = 0.0, verbose = false,
                gen_outage_factors = Dict((1, 0) => half, (3, 1) => dark),
                bat_outage_factors = Dict((1, 0) => fill(0.25, H)))
            m, _ = master1(inp)
            out(g) = [sum(value(v) for v in all_variables(m)
                          if startswith(name(v), "op_y1_d1_gen[$(g),1,$(t)]")) for t in 1:24]
            @test all(out(1) .<= 50.0 + 1e-6)
            @test sum(out(1)) ≈ 24 * 50.0 rtol = 1e-6
            @test out(3) ≈ vcat(zeros(12), fill(30.0, 12)) atol = 1e-6
            day = ESFEX.generators_for_day(inp, 1, 24)
            @test day[1].availability[:, 1] ≈ fill(0.5, 24)
            @test day[2].availability == ones(24, 1)
            @test ESFEX.batteries_for_day(inp, 1, 24)[1].availability ≈ fill(0.25, 24, 1)
            @test ESFEX.existing_vre_available(inp, 1, 1, 24) ≈ [12 * 30.0]
            # Past the factors (or without any) a unit is fully available
            @test ESFEX.master_outage_factor(inp.gen_outage_factors, 1, 1, H + 1) == 1.0
            @test ESFEX.master_outage_factor(inp.gen_outage_factors, 3, 1, 5) == 0.0
            @test ESFEX.master_outage_factor(inp.gen_outage_factors, 3, 2, 5) == 1.0
            # The adequacy credit of a dispatchable unit at its peak steps
            @test ESFEX.firm_capacity_credit(ones(10, 1), 1, 1:4; variable = false,
                                             outage = t -> t <= 2 ? 0.0 : 1.0) ≈ 0.5
            @test ESFEX.firm_capacity_credit(zeros(0, 1), 1, 1:4; variable = false,
                                             outage = t -> 0.5) ≈ 0.5
            # Benders: the same plan and cost
            br = run_benders_decomposition(inp; max_iterations = 30, tolerance = 1e-7,
                                           verbose_benders = false)
            @test br.objective ≈ objective_value(m) rtol = 1e-6
        end

        @testset "Benders: operating bound with linked systems (R27)" begin
            # The bound balanced each system on its own: a system that
            # imports in the subproblem had to shed in the bound, so θ's bound
            # passed the year's cost (B26). Two one-bus systems on a 100 MW
            # line: A has 50 MW of load and no unit, B 10 MW of load and a
            # 100 MW gas unit.
            buses2 = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                      BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            line = TransmissionLineData("l1", 1, 2, 100.0, 0.1, 0.0, 0.0, 30.0, 220.0, 1,
                                        50.0, "AC", 0.0)
            net2 = NetworkConfig(2, 2, buses2, [1, 2], [0 10.0; 10.0 0], zeros(2, 2), 100.0,
                0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0], [line], TransformerData[],
                ACDCConverterData[], FrequencyConverterData[], 0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [0.0, 100.0], [1.0e-4, 1.0e-4],
                        ones(H24, 2); nb = 2, H = H24)
            inp = MasterProblemInput(years = [2025], network = net2, generators = [gas],
                batteries = BatteryConfig[],
                base_demand = hcat(fill(50.0, H24), fill(10.0, H24)),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                system_node_ranges = [SystemNodeRange("A", 1, 1, 0.0),
                                      SystemNodeRange("B", 2, 1, 0.0)],
                representative_days_per_year = 1, min_day_separation = 1,
                transmission_loss_segments = 0, reserve_static_default_ratio = 0.0,
                loss_of_load_penalty = 10.0, verbose = false)
            @test ESFEX.benders_linked_systems(inp) == [1, 1]
            alone = ESFEX.with_fields(inp; network = ESFEX.with_fields(net2;
                transmission_lines = TransmissionLineData[]))
            @test ESFEX.benders_linked_systems(alone) == [1, 2]
            mono, _ = master1(inp)
            bm, _, _, btheta, _ = ESFEX.create_benders_master(inp)
            ESFEX.set_silent(bm)
            optimize!(bm)
            @test string(termination_status(bm)) == "OPTIMAL"
            # A valid bound (it shed A's 50 MW at VOLL: 4.4e6 M$ a year)
            @test objective_value(bm) <= objective_value(mono) * (1 + 1e-9)
            # ... still the copper plate's: B's gas serves both loads
            @test sum(value(t) for t in values(btheta)) ≈ 60 * 1.0e-4 * 8760 rtol = 1e-6
            br = run_benders_decomposition(inp; max_iterations = 20, tolerance = 1e-7,
                                           verbose_benders = false)
            @test br.objective ≈ objective_value(mono) rtol = 1e-6
            @test all(br.lb_history .<= br.ub_history .* (1 + 1e-6))
        end

        @testset "representative days stand for the year over the days selected" begin
            # Three days asked, 200 days apart: only two fit in a year. Each
            # day stood for 365 / 3 days, so the year's cost and energy
            # were a third short (T25/B25). With a flat 50 MW load the year
            # costs the same as with two days asked (both fit).
            bus = BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0)
            net = NetworkConfig(1, 1, [bus], [1], zeros(1, 1), zeros(1, 1),
                                100.0, 0.4, 220.0, 0.5, 1, [0.0], [0.0],
                                TransmissionLineData[], TransformerData[],
                                ACDCConverterData[], FrequencyConverterData[], 0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [1.0e-4],
                        ones(8760, 1); nb = 1, H = 8760)
            inp = MasterProblemInput(years = [2025], network = net, generators = [gas],
                batteries = BatteryConfig[], base_demand = fill(50.0, 8760, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = 3, min_day_separation = 200,
                loss_of_load_penalty = 10.0, verbose = false)
            nd = length(master_rep_day_offsets(inp))
            @test nd == 2
            @test ESFEX.rep_day_scaling(inp, nd) ≈ 365.0 / 2
            @test ESFEX.master_fuel_day_scale(inp, nd) ≈ 365.0 / 2
            ref, _ = master1(ESFEX.with_fields(inp; representative_days_per_year = 2))
            full = objective_value(ref)
            m, _ = master1(inp)
            @test objective_value(m) ≈ full rtol = 1e-9
            br = run_benders_decomposition(inp; max_iterations = 20, tolerance = 1e-7,
                                           verbose_benders = false)
            @test br.objective ≈ full rtol = 1e-6
            @test br.solution.total_operational_cost_by_year[1] ≈ full rtol = 1e-6
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
            # on top of the half-full start needs 600 MWh of reservoir, 500
            # more than the 100 it has (see "master reservoir builds raise
            # the reservoir's level bounds")
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
            @test mono.reservoir_investment[1][1][1] ≈ 500.0 rtol = 1e-6
            @test bend.reservoir_investment[1][1][1] ≈ 500.0 rtol = 1e-4

            # Curtailment cost on candidate PV: at 2e-5 per MWh curtailed the
            # 16.7 MW that would saturate the peak hours no longer pay off.
            # Overnight costs below are set so that the one-year annuity is
            # 0.08 (PV, 25 years) or 1.0 (dear PV and battery, 15 years).
            o = [0.0]
            crf(life) = ESFEX.capital_recovery_factor(0.05, life)
            sun = reshape(vcat(zeros(6), [0.25, 0.5, 0.75], ones(6), [0.75, 0.5, 0.25],
                               zeros(6)), H24, 1)
            pv = TechnologyConfig("PV", "Renewable", "Sun", [0.08 / crf(25)], [100.0], sun,
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
            # master forced 0.07 MW of PV and 0.35 MWh of battery). The
            # battery is not reservable: a reservable one is worth building
            # to hold the reserve the non-reservable gas unit cannot.
            pv_dear = ESFEX.with_fields(pv; invest_cost = [1.0 / crf(25)])
            li_dear = ESFEX.with_fields(
                BatteryTechnologyConfig("LiNew", [1.0 / crf(15)], [1.0 / crf(15)], [100.0], [400.0], 1.0, 8.0,
                    [0.95], [0.95], o, [15.0], [0.5], [0.0], [1.0], o, o, o, false, "AC", o,
                    [1.0]); reservable = false)
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
            for iters in (3, 4)   # the first iterations give no upper bound
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

        @testset "master economics: annuities, RE shortfall per MWh, budget" begin
            # One node, 50 MW flat, gas at 3e-5 per MWh. PV (25-year life)
            # is the candidate; hours_per_year = 24 × days makes each
            # representative day stand for itself (scaling 1).
            r = 0.05
            crf(life) = ESFEX.capital_recovery_factor(r, life)
            o = [0.0]
            gas = mkgen("Gas", "Non-renewable", "Gas", [100.0], [3.0e-5],
                        ones(H24, 1); nb = 1, H = H24)
            pvtech(avail; cost, imax = [100.0]) =
                TechnologyConfig("PV", "Renewable", "Sun", cost, imax, avail,
                                 [1.0], [1.0], [1.0], [1.0], o, o, o, o, o, o, o, o,
                                 [25.0], o, o, 50.0, "AC", false, [1.0])
            solve_master(inp) = begin
                m, v, _ = create_master_problem(inp; use_representative_days = true)
                ESFEX.set_silent(m)
                optimize!(m)
                @test string(termination_status(m)) == "OPTIMAL"
                m, v
            end

            # --- CRF and the annuity sum (OBJ-2, OBJ-3) ---
            @test crf(20) ≈ r * 1.05^20 / (1.05^20 - 1)
            @test ESFEX.capital_recovery_factor(0.0, 20) ≈ 1 / 20
            # A 2-year build in a 3-year horizon: two payments worth
            # (1 + r) × the overnight cost at the build year; a build in the
            # last year pays one annuity
            @test ESFEX.annuity_npv_factor(r, 2, 1, 3) ≈ 1 + r
            @test ESFEX.annuity_npv_factor(r, 2, 2, 3) ≈ (1 + r) / (1 + r)
            @test ESFEX.annuity_npv_factor(r, 25, 3, 3) ≈ crf(25) / (1 + r)^2
            @test ESFEX.annuity_npv_factor(0.0, 4, 1, 10) ≈ 1.0
            @test collect(ESFEX.annuity_years(2, 2.5, 10)) == [2, 3, 4]
            sun = reshape(vcat(zeros(6), ones(12), zeros(6)), H24, 1)
            inp3 = MasterProblemInput(years = [2025, 2026, 2027], network = net1,
                generators = [gas], batteries = BatteryConfig[],
                technologies = [pvtech(repeat(sun, 3); cost = [1.0], imax = [10.0])],
                base_demand = fill(50.0, 3H24, 1), target_re_penetration = 0.0,
                initial_re_penetration = 0.0, representative_days_per_year = 1,
                min_day_separation = 1, hours_per_year = [24, 24, 24],
                max_curtailment_ratio = 1.0, curtailment_cost = 0.0, verbose = false)
            model = ESFEX.Model()
            vars = ESFEX.build_master_variables!(model, inp3)
            e = ESFEX.build_investment_cost_expression(vars, inp3)
            for y in 1:3
                @test coefficient(e, vars.tech_investment[y][1][1]) ≈
                      crf(25) * sum(1 / (1 + r)^(k - 1) for k in y:3)
            end
            # Two-year investment periods: a period's build pays from its
            # first year
            inp4 = ESFEX.with_fields(inp3; years = [2025, 2026, 2027, 2028],
                investment_resolution_hours = 2 * 8760, hours_per_year = fill(24, 4),
                base_demand = fill(50.0, 4H24, 1))
            model4 = ESFEX.Model()
            vars4 = ESFEX.build_master_variables!(model4, inp4)
            e4 = ESFEX.build_investment_cost_expression(vars4, inp4)
            @test sort(collect(keys(vars4.tech_investment))) == [1, 3]
            @test coefficient(e4, vars4.tech_investment[3][1][1]) ≈
                  crf(25) * (1 / (1 + r)^2 + 1 / (1 + r)^3)

            # --- A PV build late in the horizon is now economic ---
            # Sun only in the third year. 1 MW of PV saves 12 MWh × 3e-5 of
            # gas a year (3.6e-4): more than its annuity (0.004 × crf(25) =
            # 2.8e-4) but far less than its overnight cost. Charged in full
            # in the build year with no residual value it was never built.
            avail = vcat(zeros(2H24, 1), sun)
            late = ESFEX.with_fields(inp3; technologies = [pvtech(avail; cost = [0.004])])
            m, v = solve_master(late)
            res = extract_master_solution(m, v, late)
            @test res.tech_investment[3][1][1] ≈ 50.0 rtol = 1e-6
            @test res.tech_investment[1][1][1] ≈ 0.0 atol = 1e-6
            @test res.tech_investment[2][1][1] ≈ 0.0 atol = 1e-6
            # Reporting: overnight cost in the build year, the annuity paid
            @test res.total_investment_by_year ≈ [0.0, 0.0, 50.0 * 0.004] atol = 1e-9
            @test res.annuity_cost_by_year ≈ [0.0, 0.0, 50.0 * 0.004 * crf(25)] atol = 1e-9
            # The stream behind it: (build year, life, annuity per year)
            @test length(res.annuity_streams) == 1
            @test res.annuity_streams[1][1:2] == (3, 25.0)
            @test res.annuity_streams[1][3] ≈ 50.0 * 0.004 * crf(25) rtol = 1e-9
            br = run_benders_decomposition(late; max_iterations = 40, tolerance = 1e-7,
                                           verbose_benders = false)
            @test br.objective ≈ objective_value(m) rtol = 1e-6
            @test br.solution.tech_investment[3][1][1] ≈ 50.0 rtol = 1e-4
            @test br.solution.annuity_cost_by_year ≈ res.annuity_cost_by_year rtol = 1e-4
            @test br.solution.annuity_streams[1][3] ≈ res.annuity_streams[1][3] rtol = 1e-4

            # --- Annual RE target, shortfall priced per MWh ---
            # Two representative days, the first with 12 h of sun, the
            # second dark; a 20 % target of the 2400 MWh. PV (annuity 7e-4
            # per MW) is worth building up to the target at 1e-3 per MWh
            # missing: 40 MW make the 480 MWh on the sunny day. The target
            # used to hold on each day: 20 MW for the sunny one, the dark
            # one short at the penalty.
            sun2 = vcat(sun, zeros(H24, 1))
            re_inp(; kw...) = MasterProblemInput(; years = [2025], network = net1,
                generators = [gas], batteries = BatteryConfig[],
                technologies = [pvtech(sun2; cost = [7.0e-4 / crf(25)])],
                base_demand = fill(50.0, 2H24, 1), target_re_penetration = 0.2,
                initial_re_penetration = 0.2, representative_days_per_year = 2,
                min_day_separation = 1, hours_per_year = [2H24],
                fre_penetration_loss_penalty = 1.0e-3,
                max_curtailment_ratio = 1.0, curtailment_cost = 0.0, verbose = false, kw...)
            inp = re_inp()
            m, v = solve_master(inp)
            res = extract_master_solution(m, v, inp)
            @test res.tech_investment[1][1][1] ≈ 40.0 rtol = 1e-6
            @test value(v.slack_re_target[(0, 1, 1)]) ≈ 0.0 atol = 1e-6
            @test res.re_penetration_by_year[1] ≈ 0.2 rtol = 1e-6
            @test res.re_penetration_by_system["global"][1] ≈ 0.2 rtol = 1e-6
            # The same with TSAM periods (weight 1 each) and with Benders
            tinp = re_inp(use_tsam = true, tsam_period_start_hours = [[1, 25]],
                          tsam_period_weights = [[1.0, 1.0]])
            tm, tv = solve_master(tinp)
            @test objective_value(tm) ≈ objective_value(m) rtol = 1e-9
            @test value(tv.tech_investment[1][1][1]) ≈ 40.0 rtol = 1e-6
            br = run_benders_decomposition(inp; max_iterations = 40, tolerance = 1e-7,
                                           verbose_benders = false)
            @test br.objective ≈ objective_value(m) rtol = 1e-6
            @test br.solution.tech_investment[1][1][1] ≈ 40.0 rtol = 1e-4
            @test br.solution.re_penetration_by_year[1] ≈ 0.2 rtol = 1e-4

            # 30 MW at most: 360 MWh, 120 MWh short, paid 1e-3 per MWh (it
            # used to be a fraction of the target at 1 M$ per 100 %)
            sinp = re_inp(technologies = [pvtech(sun2; cost = [7.0e-4 / crf(25)], imax = [30.0])])
            sm, sv = solve_master(sinp)
            s = sv.slack_re_target[(0, 1, 1)]
            @test value(s) ≈ 120.0 rtol = 1e-6
            @test coefficient(objective_function(sm), s) ≈ 1.0e-3
            sres = extract_master_solution(sm, sv, sinp)
            @test sres.re_penetration_by_year[1] ≈ 0.15 rtol = 1e-6
            @test ESFEX.diagnose_infeasibility(sm, sv, sinp)["re_target_s1_y1"] ≈ 120.0 rtol = 1e-6
            sbr = run_benders_decomposition(sinp; max_iterations = 40, tolerance = 1e-7,
                                            verbose_benders = false)
            @test sbr.objective ≈ objective_value(sm) rtol = 1e-6
            @test sbr.solution.re_penetration_by_year[1] ≈ 0.15 rtol = 1e-4
            # A stochastic scenario prices its own shortfall with its multiplier
            sc = [Scenario("s", 1.0, ScenarioMultipliers(penalty_fre_penetration = 2.0))]
            stm, stv, _, _ = create_stochastic_master_problem(StochasticMasterInput(sinp, sc, true))
            ESFEX.set_silent(stm)
            optimize!(stm)
            @test coefficient(objective_function(stm), stv.slack_re_target[(1, 1, 1)]) ≈ 2.0e-3
            @test objective_value(stm) ≈ objective_value(sm) + 120.0 * 1.0e-3 rtol = 1e-6
            # Without a configured penalty: the dispatch's 100 $/MWh (1e-4)
            zm, zv = solve_master(re_inp(fre_penetration_loss_penalty = 0.0))
            @test coefficient(objective_function(zm), zv.slack_re_target[(0, 1, 1)]) ≈ 1.0e-4

            # --- The annual budget binds ---
            # A budget of 0.1 buys 10 MW (overnight 0.01 each). At 5e-3 per
            # MWh missing, a MW of PV saves 0.06 a year: more than its
            # overnight cost plus the old 1:1 budget slack (0.02), less than
            # the slack at 10 per unit over the budget.
            binp = re_inp(technologies = [pvtech(sun2; cost = [0.01])],
                          fre_penetration_loss_penalty = 5.0e-3, max_annual_investment = 0.1)
            bm, bv = solve_master(binp)
            bres = extract_master_solution(bm, bv, binp)
            @test bres.tech_investment[1][1][1] ≈ 10.0 rtol = 1e-6
            @test value(bv.slack_budget[1]) ≈ 0.0 atol = 1e-6
            @test bres.total_investment_by_year[1] ≈ 0.1 rtol = 1e-6
            @test coefficient(objective_function(bm), bv.slack_budget[1]) ≈ ESFEX.BUDGET_SLACK_PENALTY
            bbr = run_benders_decomposition(binp; max_iterations = 40, tolerance = 1e-7,
                                            verbose_benders = false)
            @test bbr.objective ≈ objective_value(bm) rtol = 1e-6

            # --- Adequacy shortfall at VOLL × the node's peak hours ---
            # 100 MW of gas for a 100 MW peak × 1.15: 15 MW short. The peak
            # period is every step of a flat day: 24 h × VOLL (10) per MW.
            ainp = ESFEX.with_fields(inp3; base_demand = fill(100.0, 3H24, 1),
                                     technologies = TechnologyConfig[])
            am, av = solve_master(ainp)
            @test value(av.slack_capacity[(1, 1)]) ≈ 15.0 rtol = 1e-6
            @test coefficient(objective_function(am), av.slack_capacity[(2, 1)]) ≈
                  10.0 * 24 / (1 + r)
        end

        @testset "master: investment variables stay out of the hourly rows" begin
            # Every technology, battery and corridor build used to be summed,
            # with its degradation, into each hourly row of every later year:
            # dense columns that stalled interior-point solvers on large
            # masters. Per-year capacity state variables now carry it: the same
            # optimum, and an investment column has the same few nonzeros
            # however many hours the master models.
            nb, ny = 2, 3
            Hy = 24 * 4                       # four days per modelled year
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            line = TransmissionLineData("l1", 1, 2, 20.0, 0.1, 0.0, 0.0, 100.0, 220.0, 1,
                                        50.0, "AC", 0.0)
            net = NetworkConfig(2, 2, buses, [1, 2], [0 20.0; 20.0 0], zeros(2, 2),
                100.0, 0.4, 220.0, 0.6, 1, [2.0e-4, 2.0e-4], [100.0, 100.0], [line],
                TransformerData[], ACDCConverterData[], FrequencyConverterData[], 0.1)
            gas = mkgen("Gas", "Non-renewable", "Gas", [80.0, 0.0], [1.0e-4, 0.0],
                        ones(Hy, nb); nb = nb, H = Hy)
            sun = [max(0.0, sin(pi * (mod(h - 1, 24) - 6) / 12)) * (1.0 - 0.1 * ((h - 1) ÷ 24))
                   for h in 1:Hy]
            z, o = zeros(nb), ones(nb)
            pv = TechnologyConfig("PV", "Renewable", "Solar", [3.0e-4, 3.0e-4], [100.0, 100.0],
                hcat(sun, sun), o, o, o, o, z, z, z, z, [1.0e-6, 1.0e-6], z, z, z,
                [2.0, 2.0], [0.05, 0.05], z, 50.0, "AC", false, o)
            li = BatteryTechnologyConfig("Li", [2.0e-4, 2.0e-4], [5.0e-5, 5.0e-5],
                [50.0, 50.0], [200.0, 200.0], 1.0, 8.0, [0.95, 0.95], [0.95, 0.95], z,
                [15.0, 15.0], [0.5, 0.5], z, o, z, z, [1.0e-7, 1.0e-7], false, "AC", z, o,
                z, z, [1.0e-6, 1.0e-6], true, zeros(0, nb))
            dem = hcat(fill(10.0, Hy), [30.0 + 5.0 * sin(2pi * h / 24) for h in 1:Hy])
            inp(days) = MasterProblemInput(
                years = collect(2025:2025 + ny - 1), network = net, generators = [gas],
                batteries = BatteryConfig[], technologies = [pv], battery_technologies = [li],
                base_demand = repeat(dem, ny, 1), hours_per_year = fill(Hy, ny),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                representative_days_per_year = days, min_day_separation = 1,
                demand_growth = 0.05, transmission_loss_segments = 0, verbose = false)
            inv_pfx = ("tech_inv", "bat_tech_pow_inv", "bat_tech_cap_inv", "trans_inv")
            function inv_nonzeros(model)
                cnt = Dict{String, Int}(name(v) => 0 for v in all_variables(model)
                                        if any(startswith(name(v), p) for p in inv_pfx))
                for (F, S) in list_of_constraint_types(model)
                    F <: AffExpr || continue
                    for c in all_constraints(model, F, S), (v, a) in constraint_object(c).func.terms
                        a != 0 && haskey(cnt, name(v)) && (cnt[name(v)] += 1)
                    end
                end
                return cnt
            end
            counts = Dict{Int, Dict{String, Int}}()
            for days in (1, 3)
                input = inp(days)
                m1, v1, _ = create_master_problem(input)
                m0, v0, _ = create_master_problem(input; capacity_states = false)
                optimize!(m1)
                optimize!(m0)
                @test string(termination_status(m1)) == "OPTIMAL"
                @test objective_value(m1) ≈ objective_value(m0) rtol = 1e-9
                r1 = extract_master_solution(m1, v1, input)
                r0 = extract_master_solution(m0, v0, input)
                @test r1.total_investment_by_year ≈ r0.total_investment_by_year rtol = 1e-6
                # Something of each kind is built, so the rows above matter
                @test sum(sum(v) for d in values(r1.tech_investment) for v in values(d)) > 1
                @test sum(sum(v) for d in values(r1.bat_tech_power_investment)
                          for v in values(d)) > 1
                @test sum(sum(values(d)) for d in values(r1.transfer_investment)) > 1
                counts[days] = inv_nonzeros(m1)
                # Without the states a build's column grows with the hours
                @test maximum(values(inv_nonzeros(m0))) > 24 * days
            end
            @test counts[1] == counts[3]
            @test maximum(values(counts[3])) <= 2 * ny + 6
        end

        @testset "reserves are pooled per system" begin
            # Node 1 has 50 MW of load and no unit; node 2 has the only
            # (reservable) unit, 200 MW for 100 MW of load. A 20 % static
            # reserve (10 MW per node) used to be held per node, so node 1
            # was always 10 MW short although the system had 100 MW free.
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            line = TransmissionLineData("l1", 1, 2, 100.0, 0.1, 0.0, 0.0, 100.0, 220.0,
                                        1, 50.0, "AC")
            net2 = NetworkConfig(2, 2, buses, [1, 2], [0 100.0; 100.0 0], zeros(2, 2),
                100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0],
                [line], TransformerData[], ACDCConverterData[], FrequencyConverterData[], 0.1)
            gas(rated; H) = ESFEX.with_fields(
                mkgen("Gas", "Non-renewable", "Gas", [0.0, rated], [1.0e-5, 1.0e-5],
                      ones(H, 2); nb = 2, H = H); reservable = true)
            two_systems = [CO2BudgetGroup("A", [1], Inf, 1.0), CO2BudgetGroup("B", [2], Inf, 1.0)]
            H = 2
            function dispatch(rated; groups = CO2BudgetGroup[], rd = Dict{Int, Float64}())
                input = PowerSystemInput(
                    name = "res2", year = 2025, network = net2, generators = [gas(rated; H = H)],
                    batteries = BatteryConfig[], demand = fill(50.0, H, 2),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    reserve_static_default_ratio = 0.2, reserve_dynamic_requirement = rd,
                    dynamic_reserve_contribution = 0.1,
                    loss_of_reserve_static = 1.0, loss_of_reserve_dynamic = 1.0,
                    loss_of_load_penalty = 100.0, pwl_loss_segments = 0,
                    co2_budget_groups = groups,
                    mode = "economic_dispatch", solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                return value.(vars.reserve_static_loss), value.(vars.reserve_dynamic_loss),
                       value.(vars.reserve_static)
            end
            # One system: node 2's headroom holds node 1's reserve too.
            rs_loss, _, rs = dispatch(200.0)
            @test sum(rs_loss) ≈ 0.0 atol = 1e-6
            @test rs ≈ fill(10.0, 2, H) atol = 1e-6
            # 110 MW: 10 MW free for 20 MW of reserve. The shortfall is shared
            # in proportion to each node's requirement.
            rs_loss, _, _ = dispatch(110.0)
            @test rs_loss ≈ fill(5.0, 2, H) atol = 1e-6
            # Merged run, one system per node: system A has no unit of its own.
            rs_loss, _, _ = dispatch(200.0; groups = two_systems)
            @test rs_loss[1, :] ≈ fill(10.0, H) atol = 1e-6
            @test rs_loss[2, :] ≈ zeros(H) atol = 1e-6
            # Dynamic reserve at node 1, held by node 2's unit (0.1 × 200 MW).
            _, rd_loss, _ = dispatch(200.0; rd = Dict(1 => 15.0))
            @test sum(rd_loss) ≈ 0.0 atol = 1e-6
            _, rd_loss, _ = dispatch(200.0; rd = Dict(1 => 25.0))
            @test rd_loss[1, :] ≈ fill(5.0, H) atol = 1e-6

            # Master and Benders: same pooled reserve over one representative day
            function res_master(groups)
                MasterProblemInput(
                    years = [2025], network = net2, generators = [gas(200.0; H = H24)],
                    batteries = BatteryConfig[], base_demand = fill(50.0, H24, 2),
                    target_re_penetration = 0.0, initial_re_penetration = 0.0,
                    reserve_margin = 0.0, reserve_static_default_ratio = 0.2,
                    loss_of_reserve_static = 1.0e-3, co2_budget_groups = groups,
                    representative_days_per_year = 1, min_day_separation = 1,
                    transmission_loss_segments = 0, verbose = false)
            end
            for (groups, short) in ((CO2BudgetGroup[], 0.0), (two_systems, 10.0 * H24))
                inp = res_master(groups)
                model, _ = master1(inp)
                @test sum(mvals(model, "op_y1_d1_rs_loss")) ≈ short atol = 1e-6
                br = run_benders_decomposition(inp; max_iterations = 40, tolerance = 1e-7,
                                               verbose_benders = false)
                @test br.gap < 1e-6
                @test br.objective ≈ objective_value(model) rtol = 1e-6
            end
        end

        @testset "master: units it builds hold reserve, as in the dispatch" begin
            # 50 MW of load on a cheap unit that is not reservable; a 20 %
            # static reserve (10 MW) needs another unit. The master's built
            # capacity held no reserve, so it paid the shortfall forever
            # instead of building a reservable peaker or battery.
            o = [0.0]
            base = mkgen("Base", "Non-renewable", "Gas", [100.0], [1.0e-5],
                         ones(H24, 1); nb = 1, H = H24)
            gt(avail, reservable) = TechnologyConfig("GT", "Non-renewable", "Gas", [1.0e-3],
                [100.0], fill(avail, H24, 1), [1.0], [1.0], [1.0], [1.0], o, o, o, [1.0e-3],
                o, o, o, o, [25.0], o, o, 50.0, "AC", reservable, [1.0])
            li(reservable) = ESFEX.with_fields(
                BatteryTechnologyConfig("LiNew", [1.0e-3], [1.0e-4], [100.0], [400.0], 1.0, 8.0,
                    [0.95], [0.95], o, [15.0], [0.5], [0.0], [1.0], o, o, o, false, "AC", o,
                    [1.0]); reservable = reservable)
            res_inp(techs, bts) = MasterProblemInput(
                years = [2025], network = net1, generators = [base],
                batteries = BatteryConfig[], technologies = techs, battery_technologies = bts,
                base_demand = fill(50.0, H24, 1),
                target_re_penetration = 0.0, initial_re_penetration = 0.0,
                reserve_margin = 0.0, reserve_static_default_ratio = 0.2,
                loss_of_reserve_static = 1.0e-3,
                representative_days_per_year = 1, min_day_separation = 1,
                transmission_loss_segments = 0, verbose = false)
            # (technologies, battery technologies, MW built, reserve shortfall)
            cases = (
                ([gt(1.0, true)], BatteryTechnologyConfig[], 10.0, 0.0),
                # Headroom is built capacity × availability, as in the dispatch
                ([gt(0.5, true)], BatteryTechnologyConfig[], 20.0, 0.0),
                ([gt(1.0, false)], BatteryTechnologyConfig[], 0.0, 10.0 * H24),
                (TechnologyConfig[], [li(true)], 10.0, 0.0),
                (TechnologyConfig[], [li(false)], 0.0, 10.0 * H24),
            )
            for (techs, bts, built, short) in cases
                inp = res_inp(techs, bts)
                model, mvars = master1(inp)
                @test sum(mvals(model, "op_y1_d1_rs_loss")) ≈ short atol = 1e-6
                mw = isempty(techs) ? value(mvars.bat_tech_power_investment[1][1][1]) :
                                      value(mvars.tech_investment[1][1][1])
                @test mw ≈ built atol = 1e-6
                br = run_benders_decomposition(inp; max_iterations = 60, tolerance = 1e-7,
                                               verbose_benders = false)
                @test br.gap < 1e-6
                @test br.objective ≈ objective_value(model) rtol = 1e-6
            end

            # The dispatch, with the 20 MW the master built as an existing
            # unit at availability 0.5, holds the same 10 MW without shortfall
            peaker = ESFEX.with_fields(
                mkgen("GT", "Non-renewable", "Gas", [20.0], [1.0e-3], fill(0.5, H24, 1);
                      nb = 1, H = H24); reservable = true)
            input = PowerSystemInput(
                name = "res_built", year = 2025, network = net1, generators = [base, peaker],
                batteries = BatteryConfig[], demand = fill(50.0, H24, 1),
                temporal = TemporalConfig(H24, 1, H24, 0, H24, H24, 1, 1, 1),
                reserve_static_default_ratio = 0.2, loss_of_reserve_static = 1.0e-3,
                mode = "economic_dispatch", solver_name = "highs", verbose = false)
            model, vars = create_power_system(input)
            optimize!(model)
            @test sum(value.(vars.reserve_static_loss)) ≈ 0.0 atol = 1e-6
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

        @testset "converter min_power_mva: on/off in unit commitment, ignored in ED" begin
            # 15 MW of load at bus 2 behind a 10 MW line; the 40 MW converter
            # has a 30 MW minimum. Economic dispatch sends the missing 5 MW
            # through it; with unit commitment it either carries ≥ 30 MW
            # (more than bus 2 can take) or nothing, so dear diesel at bus 2
            # covers the 5 MW. min_power_mva was never read.
            buses = [BusData(1, 1, 220.0, 50.0, "AC", "slack", "mixed", 1.0),
                     BusData(2, 2, 220.0, 50.0, "AC", "PQ", "mixed", 1.0)]
            line = TransmissionLineData("l1", 1, 2, 10.0, 0.1, 0.0, 0.0, 100.0, 220.0,
                                        1, 50.0, "AC")
            H = 4
            dem2 = [15.0, 15.0, 50.0, 15.0]
            function conv_dispatch(mode; freq = false)
                acdc = ACDCConverterData("c1", "VSC", 1, 2, 220.0, 320.0, 40.0, 30.0,
                    1.0, 1.0, 0.0, -50.0, 50.0, 1.0, 0.05, 0.01, 1.0e-3, 0.0, 1.0e-6, 50.0,
                    30, 0, 0.0)
                fc = FrequencyConverterData("f1", 1, 2, 50.0, 60.0, 40.0, 30.0, 1.0, 1.0,
                    0.0, -50.0, 50.0, 0.05, 0.01, 1.0e-3, 0.0, 1.0e-6, 50.0, 30, 0, 0.0)
                net = NetworkConfig(2, 2, buses, [1, 2], [0 10.0; 10.0 0], zeros(2, 2),
                    100.0, 0.4, 220.0, 0.6, 1, [0.0, 0.0], [0.0, 0.0], [line],
                    TransformerData[], freq ? ACDCConverterData[] : [acdc],
                    freq ? [fc] : FrequencyConverterData[], 0.1)
                gas = mkgen("Gas", "Non-renewable", "Gas", [100.0, 0.0], [1.0e-5, 0.0],
                            ones(H, 2); nb = 2, H = H)
                diesel = mkgen("Diesel", "Non-renewable", "Diesel", [0.0, 100.0],
                               [0.0, 1.0e-3], ones(H, 2); nb = 2, H = H)
                input = PowerSystemInput(
                    name = "convmin", year = 2025, network = net, generators = [gas, diesel],
                    batteries = BatteryConfig[], demand = hcat(zeros(H), dem2),
                    temporal = TemporalConfig(H, 1, H, 0, H, H, 1, 1, 1),
                    pwl_loss_segments = 0, reserve_static_default_ratio = 0.0,
                    mode = mode, solver_name = "highs", verbose = false)
                model, vars = create_power_system(input)
                optimize!(model)
                @test string(termination_status(model)) == "OPTIMAL"
                flow = freq ? value.(vars.freq_flow_a_to_b[1, :]) : value.(vars.acdc_rectify[1, :])
                return model, vars, input, flow
            end
            for freq in (false, true)
                _, _, _, ed = conv_dispatch("economic_dispatch"; freq = freq)
                @test ed ≈ [5.0, 5.0, 40.0, 5.0] atol = 1e-6
                model, vars, input, uc = conv_dispatch("unit_commitment"; freq = freq)
                @test uc ≈ [0.0, 0.0, 40.0, 0.0] atol = 1e-6
                @test length(model.ext[:converter_commitment]) == 2 * H
                # The UC dual recovery fixes the converter binaries too (the
                # LP re-solve had binaries left and returned no prices)
                @test recover_uc_duals_via_copy(model, vars, input) !== nothing
                # ... and leaves the solved model as it was. The in-place
                # variant (which read values after changing the model) is gone.
                @test ESFEX.JuMP.has_values(model)
                @test !isdefined(ESFEX, :recover_uc_duals!)
            end
        end

        @testset "NPV-based retirement of existing units" begin
            # One bus, 50 MW flat, two years of one 24 h representative day
            # (hours_per_year 24: the day is the year). Gas (200 MW, $30/MWh)
            # serves the load; a $200/MWh peaker (50 MW) never runs but pays
            # $1000/MW·yr of fixed O&M; a battery (20 MW / 40 MWh).
            # calculate_unit_npv used to ignore the result (fixed capacity
            # factor, and it read a field generators do not have), and
            # force_unit_retirements! did nothing.
            H = 24
            wf = ESFEX.with_fields
            gas = wf(mkgen("Gas", "Non-renewable", "Gas", [200.0], [3.0e-5], ones(H, 1);
                           nb = 1, H = H); fixed_cost = [1.0e-5], maintenance_cost = [2.0e-6])
            peaker = wf(mkgen("Peaker", "Non-renewable", "Diesel", [50.0], [2.0e-4],
                              ones(H, 1); nb = 1, H = H);
                        fixed_cost = [1.0e-3], decommissioning_cost = [1.0e-4])
            bat = wf(BatteryConfig("Li1", [40.0], [20.0], [20.0], [0.95], [0.95],
                                   [0.0], [1.0], [0.5], [0.0], [0.0], [0.0],
                                   [10.0], [0.0], [0.0], 0.0, 100.0, [2.0e-6],
                                   [0.0], false, "AC", [0.0], [1.0e-6], [0.0]);
                     invest_cost_power = [0.1], fixed_cost = [1.0e-3])
            mkinput(; kw...) = MasterProblemInput(; years = [2025, 2026],
                network = single_bus(), generators = [gas, peaker], batteries = [bat],
                base_demand = fill(50.0, 2H, 1), target_re_penetration = 0.0,
                initial_re_penetration = 0.0, representative_days_per_year = 1,
                min_day_separation = 1, hours_per_year = [24, 24],
                verbose = false, kw...)
            df(op, y) = 1.0 / (1.0 + op.discount_rate)^(y - 1)
            unit_total(op) = sum(df(op, k[4]) * v for (k, v) in op.operating_cost) +
                             sum(df(op, k[4]) * v for (k, v) in op.asset_cost)
            function solve(i)
                local m, v
                m, v, _ = create_master_problem(i; use_representative_days = true)
                ESFEX.set_silent(m)
                optimize!(m)
                @test string(termination_status(m)) == "OPTIMAL"
                return m, v, extract_master_solution(m, v, i; unit_operation = true)
            end

            inp = mkinput()
            model, mv, res = solve(inp)
            op = only(res.unit_operation)
            @test op.probability == 1.0
            # Every cost of this system is an existing unit's: the units'
            # operating and asset costs make up the master's objective
            @test unit_total(op) ≈ objective_value(model) rtol = 1e-9
            # Gas produces the load; the peaker nothing
            for y in 1:2
                @test op.energy[(:gen, 1, 1, y)] ≈ 50.0 * H rtol = 1e-6
                @test op.energy[(:gen, 2, 1, y)] ≈ 0.0 atol = 1e-6
                @test op.operating_cost[(:gen, 1, 1, y)] ≈ 50.0 * H * (3.0e-5 + 2.0e-6) rtol = 1e-6
                @test op.asset_cost[(:gen, 2, 1, y)] ≈ 50.0 * 1.0e-3 rtol = 1e-9
                @test op.asset_cost[(:bat, 1, 1, y)] ≈ 20.0 * 1.0e-3 rtol = 1e-9
            end
            npvs = Dict((u.unit_type, u.unit_idx) => u for u in unit_npvs(res, inp))
            @test length(npvs) == 3
            r = inp.discount_rate
            # Revenue: the dispatched MWh × base_lcoe
            gas_npv = sum((op.energy[(:gen, 1, 1, y)] * inp.base_lcoe -
                           op.operating_cost[(:gen, 1, 1, y)] - op.asset_cost[(:gen, 1, 1, y)]) /
                          (1 + r)^(y - 1) for y in 1:2)
            @test npvs[("generator", 1)].npv ≈ gas_npv rtol = 1e-9
            @test npvs[("generator", 1)].npv > 0
            @test npvs[("generator", 2)].npv ≈ -50.0 * 1.0e-3 * (1 + 1 / (1 + r)) rtol = 1e-9
            @test npvs[("generator", 2)].recommend_retirement
            @test npvs[("generator", 2)].remaining_lifetime == 2.0
            # Battery revenue: npv_annual_return_rate × invest_cost_power × MW
            bat_npv = sum((inp.npv_annual_return_rate * 0.1 * 20.0 -
                           op.operating_cost[(:bat, 1, 1, y)] - op.asset_cost[(:bat, 1, 1, y)]) /
                          (1 + r)^(y - 1) for y in 1:2)
            @test npvs[("battery", 1)].npv ≈ bat_npv rtol = 1e-9
            @test !npvs[("battery", 1)].recommend_retirement
            @test [u.unit_idx for u in get_units_with_negative_npv(res, inp)] == [2]
            # A threshold above the gas NPV flags it too
            @test length(get_units_with_negative_npv(res, inp;
                npv_threshold = npvs[("generator", 1)].npv + 1.0)) == 3
            # Without the units' operation the NPV cannot be computed
            @test_throws ErrorException calculate_unit_npv(
                extract_master_solution(model, mv, inp), inp, "generator", 1, 1)

            # Retiring the peaker: no capacity, no fixed O&M, its
            # decommissioning ($100/MW) paid once, in the first year
            rinp = retire_units(inp, [npvs[("generator", 2)]])
            @test rinp.generators[2].rated_power == [0.0]
            @test only(rinp.npv_retired_units).decommissioned_mw ≈ 50.0
            rmodel, rmv, rres = solve(rinp)
            @test all(rres.cumulative_gen_capacity[y][2][1] == 0.0 for y in 1:2)
            @test objective_value(rmodel) ≈ objective_value(model) -
                  50.0 * 1.0e-3 * (1 + 1 / (1 + r)) + 50.0 * 1.0e-4 rtol = 1e-9
            @test value(ESFEX.build_annual_asset_cost(rmv, rinp, 1)) -
                  value(ESFEX.build_annual_asset_cost(rmv, rinp, 2)) ≈ 50.0 * 1.0e-4 rtol = 1e-6
            # A myopic later year keeps it retired without paying again
            ninp = retire_units(inp, [npvs[("generator", 2)]]; decommission = false)
            nmodel, _, _ = solve(ninp)
            @test objective_value(nmodel) ≈ objective_value(rmodel) - 50.0 * 1.0e-4 rtol = 1e-9

            # The iteration retires the peaker, re-solves and stops
            it = solve_with_npv_iteration(inp; max_iterations = 5)
            @test it.converged
            @test it.iterations == 2
            @test [(u.unit_type, u.unit_idx) for u in it.forced_retirements] == [("generator", 2)]
            @test it.final_result.objective ≈ objective_value(rmodel) rtol = 1e-9
            @test it.npv_history[1]["generator_2_1"] < 0
            @test !haskey(it.npv_history[2], "generator_2_1")
            # max_iterations = 0: flags, but does not retire
            it0 = solve_with_npv_iteration(inp; max_iterations = 0)
            @test !it0.converged && isempty(it0.forced_retirements)

            # Benders: same units' operation, same retirement
            br = run_benders_decomposition(inp; max_iterations = 60, tolerance = 1e-7,
                                           verbose_benders = false, record_unit_operation = true)
            bop = only(br.solution.unit_operation)
            @test unit_total(bop) ≈ objective_value(model) rtol = 1e-6
            for u in unit_npvs(br.solution, inp)
                @test u.npv ≈ npvs[(u.unit_type, u.unit_idx)].npv rtol = 1e-5
            end
            bit = solve_with_npv_iteration(inp; solver = :benders, max_iterations = 60,
                                           tolerance = 1e-7, verbose_benders = false)
            @test bit.converged
            @test [(u.unit_type, u.unit_idx) for u in bit.forced_retirements] == [("generator", 2)]
            @test bit.final_result.objective ≈ objective_value(rmodel) rtol = 1e-6
            # Benders without recording keeps the result as it was
            @test isempty(run_benders_decomposition(inp; max_iterations = 60,
                tolerance = 1e-7, verbose_benders = false).solution.unit_operation)

            # Stochastic: one UnitOperation per scenario (its probability and
            # discount rate); they make up the expected objective
            sc = [Scenario("low", 0.3, ScenarioMultipliers(fuel_cost = 0.5, gen_fixed_cost = 2.0)),
                  Scenario("high", 0.7, ScenarioMultipliers(fuel_cost = 2.0, discount_rate = 2.0))]
            smodel, smv, _, _ = create_stochastic_master_problem(
                StochasticMasterInput(inp, sc, true))
            ESFEX.set_silent(smodel)
            optimize!(smodel)
            sres = extract_master_solution(smodel, smv, inp; unit_operation = true)
            sops = sres.unit_operation
            @test [o.probability for o in sops] == [0.3, 0.7]
            @test sops[2].discount_rate ≈ 2 * inp.discount_rate
            @test sum(o.probability * unit_total(o) for o in sops) ≈ objective_value(smodel) rtol = 1e-9
            @test sops[1].operating_cost[(:gen, 1, 1, 1)] ≈ 50.0 * H * (1.5e-5 + 2.0e-6) rtol = 1e-6
            @test sops[1].asset_cost[(:gen, 2, 1, 1)] ≈ 2 * 50.0 * 1.0e-3 rtol = 1e-9
            snpv = only(u for u in unit_npvs(sres, inp) if u.unit_idx == 1 && u.unit_type == "generator")
            per_sc = [sum((o.energy[(:gen, 1, 1, y)] * inp.base_lcoe -
                           o.operating_cost[(:gen, 1, 1, y)] - o.asset_cost[(:gen, 1, 1, y)]) /
                          (1 + o.discount_rate)^(y - 1) for y in 1:2) for o in sops]
            @test snpv.npv ≈ 0.3 * per_sc[1] + 0.7 * per_sc[2] rtol = 1e-9

            # LP start-ups (the dispatch commits units) are the unit's too: no
            # load in the first 6 h, and a 50 % minimum power, so gas starts
            # each morning (reservable: it holds the reserve the battery held)
            ucgas = wf(gas; min_power = [0.5], start_up_cost = [1.0e-3], reservable = true)
            dem = repeat(vcat(zeros(6), fill(50.0, H - 6)), 2)
            uinp = MasterProblemInput(; years = [2025, 2026], network = single_bus(),
                generators = [ucgas], batteries = BatteryConfig[],
                base_demand = reshape(dem, 2H, 1), target_re_penetration = 0.0,
                initial_re_penetration = 0.0, representative_days_per_year = 1,
                min_day_separation = 1, hours_per_year = [24, 24],
                verbose = false, unit_commitment = true)
            umodel, _, ures = solve(uinp)
            uop = only(ures.unit_operation)
            @test unit_total(uop) ≈ objective_value(umodel) rtol = 1e-9
            fuel_only = uop.energy[(:gen, 1, 1, 1)] * (3.0e-5 + 2.0e-6)
            @test uop.operating_cost[(:gen, 1, 1, 1)] - fuel_only > 1e-6

            # A re-solve that breaks the curtailment limit undoes its round.
            # Solar (12 h a day) and a store that takes its surplus serve the
            # load; the store's fixed O&M ($1M/MW·yr) and the idle gas make
            # both NPVs negative, but without the store the surplus breaks
            # the master's 5 % curtailment limit. That limit was a hard
            # constraint (the re-solve was infeasible, and the iteration
            # used to stop there with nothing returned to keep); it is
            # priced now (round 39), and a re-solve that curtails more above
            # it than the plan before is undone like an infeasible one.
            day_sun = reshape(vcat(ones(12), zeros(12)), H, 1)
            solar = mkgen("Solar", "Renewable", "Sun", [100.0], [0.0], day_sun; nb = 1, H = H)
            store = wf(BatteryConfig("Store", [700.0], [60.0], [60.0], [1.0], [1.0],
                                     [0.0], [1.0], [0.0], [0.0], [0.0], [0.0],
                                     [10.0], [0.0], [0.0], 0.0, 100.0, [0.0],
                                     [0.0], false, "AC", [0.0], [0.0], [0.0]);
                       invest_cost_power = [0.1], fixed_cost = [1.0])
            cinp = MasterProblemInput(; years = [2025], network = single_bus(),
                generators = [solar, gas], batteries = [store],
                base_demand = fill(50.0, H, 1), target_re_penetration = 0.0,
                initial_re_penetration = 0.0, representative_days_per_year = 1,
                min_day_separation = 1, hours_per_year = [24],
                verbose = false)
            cmodel, _, cres = solve(cinp)
            flagged = get_units_with_negative_npv(cres, cinp)
            @test sort([(u.unit_type, u.unit_idx) for u in flagged]) ==
                  [("battery", 1), ("generator", 2)]
            fm, _, _ = create_master_problem(retire_units(cinp, flagged))
            ESFEX.set_silent(fm)
            optimize!(fm)
            @test string(termination_status(fm)) == "OPTIMAL"
            @test ESFEX.curtailment_excess_mwh(fm) > 100.0
            @test ESFEX.curtailment_excess_mwh(cmodel) == 0.0
            # The iteration solves with optimize_master! (its retry, and its
            # warning on the re-solve's curtailment above the limit; it used
            # plain optimize!)
            cit = @test_logs (:warn, r"Master problem: system .* curtails [0-9.]+ GWh/yr above its annual curtailment limit") (:warn, r"curtails [0-9.]+ GWh/yr above the annual curtailment limit \(the plan before: 0\.0\); keeping these units") match_mode = :any solve_with_npv_iteration(cinp)
            @test !cit.converged && cit.iterations == 2
            @test isempty(cit.forced_retirements)
            @test cit.final_result.objective ≈ objective_value(cmodel) rtol = 1e-9

            # A re-solve that costs more than the plan before undoes its
            # round (any optimal re-solve used to be kept). At a base_lcoe of
            # $10/MWh gas ($32/MWh) has a negative NPV, but without it the
            # $200/MWh peaker serves the load.
            idle = wf(peaker; rated_power = [200.0], fixed_cost = [0.0])
            linp = mkinput(; generators = [gas, idle], batteries = BatteryConfig[],
                           base_lcoe = 1.0e-5)
            lmodel, _, lres = solve(linp)
            @test [(u.unit_type, u.unit_idx) for u in get_units_with_negative_npv(lres, linp)] ==
                  [("generator", 1)]
            @test npv_round_reference_cost(lres, linp, get_units_with_negative_npv(lres, linp)) ==
                  objective_value(lmodel)
            lm, _, _ = solve(retire_units(linp, get_units_with_negative_npv(lres, linp)))
            # The peaker's $200/MWh for gas's $32/MWh, less gas's fixed O&M
            @test objective_value(lm) - objective_value(lmodel) ≈
                  (24 * 50.0 * (2.0e-4 - 3.2e-5) - 200.0 * 1.0e-5) * (1 + 1 / (1 + r)) rtol = 1e-6
            lit = @test_logs (:warn, r"retiring generator 1 at bus 1 .* raises the plan's total cost to [0-9.e-]+ M\$ \(the plan before: [0-9.e-]+ M\$\); keeping these units") match_mode = :any solve_with_npv_iteration(linp)
            @test !lit.converged && lit.iterations == 2
            @test isempty(lit.forced_retirements)
            @test lit.final_result.objective ≈ objective_value(lmodel) rtol = 1e-9
            # Within the solver's noise (relative 1e-6) it is not a rise
            @test !ESFEX.cost_grew(1.0, 1.0 + 1e-7) && ESFEX.cost_grew(1.0, 1.0 + 1e-5)
            @test !ESFEX.cost_grew(-1.0, -1.0 + 1e-7) && ESFEX.cost_grew(-1.0, -1.0 + 1e-5)
            # (retiring the $1000/MW·yr peaker above lowers it: kept, `it`)
            @test it.final_result.objective < objective_value(model)

            # The comparison counts what the objective leaves out. An idle
            # peaker with $1000/MW·yr of fixed O&M and a $1500/MW
            # decommissioning cost: in a one-year master retiring it costs
            # $500/MW more than keeping it (undone), but a myopic year's
            # master whose NPV covers two more years (extra_years) counts
            # the fixed O&M it saves in them: kept.
            dpk = wf(peaker; decommissioning_cost = [1.5e-3])
            yinp = mkinput(; years = [2025], base_demand = fill(50.0, H, 1),
                           hours_per_year = [24], generators = [gas, dpk],
                           batteries = BatteryConfig[])
            ymodel, _, yres = solve(yinp)
            ypk = only(get_units_with_negative_npv(yres, yinp))
            ym, _, _ = solve(retire_units(yinp, [ypk]))
            @test objective_value(ym) ≈ objective_value(ymodel) + 50.0 * (1.5e-3 - 1.0e-3) rtol = 1e-9
            yit = @test_logs (:warn, r"raises the plan's total cost") match_mode = :any solve_with_npv_iteration(yinp)
            @test isempty(yit.forced_retirements)
            later = 50.0 * 1.0e-3 * (1 / (1 + r) + 1 / (1 + r)^2)
            ypk2 = only(get_units_with_negative_npv(yres, yinp; extra_years = 2))
            @test npv_round_reference_cost(yres, yinp, [ypk2]; extra_years = 2) ≈
                  objective_value(ymodel) + later rtol = 1e-9
            yit2 = solve_with_npv_iteration(yinp; extra_years = 2)
            @test yit2.converged
            @test [(u.unit_type, u.unit_idx) for u in yit2.forced_retirements] == [("generator", 2)]
            @test yit2.final_result.objective ≈ objective_value(ym) rtol = 1e-9

            # Myopic: a one-year master's NPV can cover the unit's later
            # years in the horizon (extra_years), its operation held. With
            # nothing changing from year to year it is the two-year master's
            # NPV (it covered the master's own year only).
            one = mkinput(; years = [2025], base_demand = fill(50.0, H, 1), hours_per_year = [24])
            _, _, ores = solve(one)
            for u in unit_npvs(ores, one; extra_years = 1)
                @test u.npv ≈ npvs[(u.unit_type, u.unit_idx)].npv rtol = 1e-9
                @test u.remaining_lifetime == 2.0
            end
            @test only(u for u in unit_npvs(ores, one) if u.unit_idx == 2 &&
                       u.unit_type == "generator").npv ≈ -50.0 * 1.0e-3 rtol = 1e-9
            # The peaker reaches its life_time (31, age 29 now) in year 3 of
            # 4: two years in service, then decommissioned (on 50 MW)
            old = mkinput(; years = [2025], base_demand = fill(50.0, H, 1), hours_per_year = [24],
                          generators = [gas, wf(peaker; initial_age = [29.0], life_time = [31.0])])
            _, _, olres = solve(old)
            pk = only(u for u in unit_npvs(olres, old; extra_years = 3) if u.unit_idx == 2 &&
                      u.unit_type == "generator")
            @test pk.remaining_lifetime == 2.0
            @test pk.npv ≈ -50.0 * 1.0e-3 * (1 + 1 / (1 + r)) - 50.0 * 1.0e-4 / (1 + r)^2 rtol = 1e-9
            # Degradation and fuel price growth (a scenario's fuel_price_growth)
            dgas = wf(gas; degradation_rate = [0.01])
            gsc = [Scenario("g", 1.0, ScenarioMultipliers(fuel_price_growth = 1.1))]
            ginp = mkinput(; years = [2025], base_demand = fill(50.0, H, 1), hours_per_year = [24],
                           generators = [dgas])
            gm, gv, _, _ = create_stochastic_master_problem(StochasticMasterInput(ginp, gsc, true))
            ESFEX.set_silent(gm)
            optimize!(gm)
            gres = extract_master_solution(gm, gv, ginp; unit_operation = true)
            gop = only(gres.unit_operation)
            k1 = (:gen, 1, 1, 1)
            @test gop.fuel_growth[k1] ≈ 1.1
            @test gop.fuel_cost[k1] ≈ gop.energy[k1] * 3.0e-5 rtol = 1e-9
            E, C, F = gop.energy[k1], gop.operating_cost[k1], gop.fuel_cost[k1]
            expected = E * ginp.base_lcoe - C - gop.asset_cost[k1]
            for k in 1:2
                d = 0.99^k
                expected += (E * d * ginp.base_lcoe - ((C - F) * d + F * d * 1.1^k) -
                             1.0e-5 * 200.0 * 0.99^k) / (1 + r)^k
            end
            @test only(u for u in unit_npvs(gres, ginp; extra_years = 2)
                       if u.unit_type == "generator").npv ≈ expected rtol = 1e-9

            # Primary energy on: a fuel-coupled unit pays the fuel it burns at
            # the fuel's price (3e-5 per unit, 5 MWh per unit), not fuel_cost
            finp = fuel_invest_input()
            fm, fv, _ = create_master_problem(finp; use_representative_days = true)
            ESFEX.set_silent(fm)
            optimize!(fm)
            fop = only(extract_master_solution(fm, fv, finp; unit_operation = true).unit_operation)
            for y in 1:2
                @test fop.operating_cost[(:gen, 1, 1, y)] ≈
                      fop.energy[(:gen, 1, 1, y)] * 3.0e-5 / 5.0 rtol = 1e-9
            end
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
