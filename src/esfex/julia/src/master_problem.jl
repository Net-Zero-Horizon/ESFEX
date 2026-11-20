"""
master_problem.jl - Capacity Expansion Master Problem

Handles long-term investment decisions for:
- Generator capacity additions and retirements
- Battery storage investments
- Transmission expansion
- Representative day selection and operational validation

The Master Problem uses a two-stage structure:
1. First stage: Investment decisions (common across scenarios)
2. Second stage: Operational costs validated through representative days
"""

using JuMP: @variable, @constraint, @objective, @expression, Model, AffExpr
using JuMP: value, VariableRef, add_to_expression!, objective_value
using JuMP: termination_status, solve_time, optimize!
using JuMP: lower_bound, upper_bound, set_lower_bound, set_upper_bound
using JuMP: set_objective_function, set_objective_sense, MAX_SENSE, MIN_SENSE
using JuMP: objective_function

"""
Capacities (MW, MWh) a solved master reports at or below this are solver
noise and read as 0 (`capacity_value`). An interior-point solve without
crossover leaves such values on capacity it did not mean to build (1e-6 MW at
a bus, a limit left at 3e-7 MW); carried into the next myopic year or the
dispatch as existing units, they gave rows like `gen ≤ 2e-9` that made the
next master numerically fragile (round 36).
"""
const CAPACITY_TOL = 1e-3

"""
    capacity_value(x) -> Float64

`value(x)` of a solved capacity variable or expression, 0 when its magnitude
is at most `CAPACITY_TOL`.
"""
capacity_value(x) = (v = value(x); abs(v) <= CAPACITY_TOL ? 0.0 : v)

"""
    optimize_master!(model; retry = NUMERICAL_RETRY_PARAMS[solver])

Solve a master problem. When a solve ends `NUMERICAL_ERROR` or
`LOCALLY_SOLVED` (Gurobi's `SUBOPTIMAL`: the barrier stalled and returned an
iterate short of its tolerances) and the solver has `retry` parameters
(`NUMERICAL_RETRY_PARAMS`: Gurobi's homogeneous barrier), it is solved once
more with them, with a warning, and they are set back afterwards
(`solve_retrying!`). With line losses, that holds for each solve of the loss
repair (`optimize_exact_losses!`), which also keeps the last optimal round
when a re-solve still ends short of optimal. A barrier run can lose its way
on a large master (Cuba myopic TSAM 2027: `NUMERICAL_ERROR` after 228
iterations, solved by the homogeneous barrier in 112; Cuba myopic 2037: the
third repair round ended `SUBOPTIMAL`), and a myopic run stops at the first
year without an optimal plan. Solvers without retry parameters keep the
status of the solve. The dispatch solves the same way (`optimize_dispatch!`).
A solved master that curtails above its annual curtailment limit gets a
warning per system and year (`warn_curtailment_excess`).
"""
function optimize_master!(model::Model; retry = numerical_retry_params(model))
    optimize_retrying!(model, retry, "Master problem")
    # The annual curtailment limit is soft: report what passes it
    warn_curtailment_excess(model)
    return
end

# =============================================================================
# Target RE Calculation
# =============================================================================

"""
    calculate_target_ratios(input::MasterProblemInput)

Calculate progressive RE penetration targets for each year.
Linear interpolation from initial to target penetration, each step clamped
to the min/max increments, never above 1.
Entries of `input.re_target_override` replace the computed targets.
"""
function calculate_target_ratios(input::MasterProblemInput)::Dict{Tuple{Int,Int}, Float64}
    num_years = length(input.years)
    targets = Dict{Tuple{Int,Int}, Float64}()
    n_sys = length(input.system_node_ranges)

    if n_sys == 0
        # Single global system — use global initial_re_penetration
        for y_idx in 1:num_years
            targets[(1, y_idx)] = _compute_year_target(
                y_idx, num_years, input.initial_re_penetration,
                input.target_re_penetration, input.min_re_increment,
                input.max_re_increment,
                y_idx > 1 ? targets[(1, y_idx - 1)] : 0.0
            )
        end
    else
        # Per-system targets using each system's initial_re
        for s_idx in 1:n_sys
            sys_initial = input.system_node_ranges[s_idx].initial_re
            for y_idx in 1:num_years
                targets[(s_idx, y_idx)] = _compute_year_target(
                    y_idx, num_years, sys_initial,
                    input.target_re_penetration, input.min_re_increment,
                    input.max_re_increment,
                    y_idx > 1 ? targets[(s_idx, y_idx - 1)] : 0.0
                )
            end
        end
    end

    merge!(targets, input.re_target_override)
    return targets
end

function _compute_year_target(
    y_idx::Int, num_years::Int, initial_re::Float64,
    target_re::Float64, min_inc::Float64, max_inc::Float64,
    prev_target::Float64
)::Float64
    if y_idx == 1
        return initial_re
    elseif num_years == 1
        return target_re
    else
        progress = (y_idx - 1) / (num_years - 1)
        target = initial_re + progress * (target_re - initial_re)
        increment = target - prev_target
        if increment < min_inc
            target = prev_target + min_inc
        elseif increment > max_inc
            target = prev_target + max_inc
        end
        # A share above 100 % cannot be met (the minimum step may push the
        # path past it)
        return min(target, 1.0)
    end
end

# =============================================================================
# Representative Day Selection
# =============================================================================

"""
    select_representative_days(demand, year_idx, num_days, min_separation, timesteps_per_day, timesteps_per_year)

Select representative days based on peak gross demand.

Returns vector of starting timestep indices for each representative day.
"""
function select_representative_days(
    demand::Matrix{Float64},
    year_idx::Int,
    num_days::Int,
    min_separation::Int,
    timesteps_per_day::Int,
    timesteps_per_year::Int,
)::Vector{Int}
    year_start = (year_idx - 1) * timesteps_per_year + 1
    year_end = min(year_idx * timesteps_per_year, size(demand, 1))

    if year_end < year_start
        return Int[]
    end

    year_demand = demand[year_start:year_end, :]
    num_timesteps = size(year_demand, 1)
    num_possible_days = num_timesteps ÷ timesteps_per_day

    if num_possible_days < 1
        return Int[]
    end

    daily_peaks = Float64[]
    for d in 1:num_possible_days
        start_t = (d - 1) * timesteps_per_day + 1
        end_t = d * timesteps_per_day
        day_signal = year_demand[start_t:end_t, :]
        push!(daily_peaks, maximum(sum(day_signal, dims=2)))
    end

    # Helper: check if day satisfies minimum separation from all selected
    function is_valid_day(day_idx::Int, selected::Set{Int}, min_sep::Int)::Bool
        for sel_d in selected
            if abs(day_idx - sel_d) < min_sep
                return false
            end
        end
        return true
    end

    # Helper: find day with highest peak in range satisfying constraints
    function find_best_in_range(start_d::Int, end_d::Int, excluded::Set{Int}, min_sep::Int)::Int
        best = -1
        best_peak = -Inf
        for d in start_d:end_d
            if d in excluded
                continue
            end
            if !is_valid_day(d, excluded, min_sep)
                continue
            end
            if daily_peaks[d] > best_peak
                best_peak = daily_peaks[d]
                best = d
            end
        end
        return best
    end

    # Start with global peak day
    selected_days = Int[]
    selected_indices = Set{Int}()

    peak_day = argmax(daily_peaks)
    push!(selected_days, year_start + (peak_day - 1) * timesteps_per_day)
    push!(selected_indices, peak_day)

    # Select remaining days using segment-based approach for temporal diversity
    # (matches Python legacy: divide year into segments, pick best per segment)
    remaining_needed = min(num_days, num_possible_days) - 1

    if remaining_needed > 0
        # Divide year into segments to ensure diversity across seasons
        num_segments = remaining_needed + 1  # +1 because peak day occupies one segment
        segment_size = num_possible_days ÷ num_segments

        for seg in 0:(num_segments - 1)
            if length(selected_days) >= min(num_days, num_possible_days)
                break
            end

            seg_start = seg * segment_size + 1
            seg_end = min((seg + 1) * segment_size, num_possible_days)

            best = find_best_in_range(seg_start, seg_end, selected_indices, min_separation)
            if best > 0
                push!(selected_days, year_start + (best - 1) * timesteps_per_day)
                push!(selected_indices, best)
            end
        end
    end

    return selected_days
end

"""
    select_matching_days(day_means, daily_peaks, num_days, min_separation) -> Vector{Int}

Representative days (1-based day indices, the peak day first) whose mean of
every feature matches its annual mean, so that the year the days stand for
(each `365 / num_days` days, uniform weights) has the year's demand energy
and renewable energy. `day_means[d, f]` is feature `f`'s mean on day `d`
(the system demand, then each renewable availability profile).

The day with the highest `daily_peaks` is always kept (adequacy). The year
is split into `num_days` segments; every segment other than the peak day's
gets one day (temporal diversity), at least `min_separation` days from the
others (a segment with no valid day takes one from anywhere in the year).
The days are chosen to minimise Σ_f (mean on the days / annual mean − 1)²:
greedily segment by segment, then by moving one day, or two days together,
within their segments while that lowers the error. Ties go to the day with the higher peak, so features that
are equal on every day give the peak-based choice.
"""
function select_matching_days(day_means::AbstractMatrix{Float64},
                              daily_peaks::AbstractVector{Float64},
                              num_days::Int, min_separation::Int)::Vector{Int}
    D = size(day_means, 1)
    n = min(num_days, D)
    (D >= 1 && n >= 1) || return Int[]
    annual = vec(sum(day_means; dims = 1)) ./ D
    used = [f for f in axes(day_means, 2) if annual[f] > 1e-12]
    X = day_means[:, used] ./ annual[used]'   # relative daily means, year = 1
    F = length(used)
    sep = max(min_separation, 1)
    # `d` is `sep` days from every chosen day but those in slots i1, i2
    sep_ok(d, chosen, i1, i2) = all(i == i1 || i == i2 || abs(d - chosen[i]) >= sep
                                    for i in eachindex(chosen))
    # Σ_f (mean of the days − 1)² with feature sums S and the day count m
    err(S, m) = sum(abs2(S[f] / m - 1) for f in 1:F; init = 0.0)
    trial = zeros(F)

    peak = argmax(daily_peaks)
    chosen = [peak]
    S = X[peak, :]
    seg = D ÷ n
    segment(s) = ((s - 1) * seg + 1):(s == n ? D : s * seg)
    peak_seg = min((peak - 1) ÷ seg + 1, n)
    ranges = UnitRange{Int}[1:1]   # candidate days of each slot (peak: fixed)

    # Greedy start: segment by segment, the day that brings the running
    # mean closest to the year
    for s in 1:n
        s == peak_seg && continue
        r = segment(s)
        any(d -> sep_ok(d, chosen, 0, 0), r) || (r = 1:D)
        best, best_e = 0, Inf
        for d in r
            sep_ok(d, chosen, 0, 0) || continue
            trial .= S .+ @view X[d, :]
            e = err(trial, length(chosen) + 1)
            if e < best_e - 1e-12 ||
               (abs(e - best_e) <= 1e-12 && daily_peaks[d] > daily_peaks[best])
                best, best_e = d, e
            end
        end
        best == 0 && continue
        push!(chosen, best)
        push!(ranges, r)
        S .+= @view X[best, :]
    end

    # Improve while the error drops (the peak day stays): one day at a
    # time, then two at a time when no single change helps (a seasonal
    # profile can need two days moved together)
    m = length(chosen)
    cur = err(S, m)
    for _ in 1:100
        improved = false
        for i in 2:m
            base = S .- @view X[chosen[i], :]
            for d in ranges[i]
                (d == chosen[i] || !sep_ok(d, chosen, i, i)) && continue
                trial .= base .+ @view X[d, :]
                e = err(trial, m)
                if e < cur - 1e-12
                    chosen[i], cur = d, e
                    S .= trial
                    improved = true
                end
            end
        end
        improved && continue
        for i in 2:m, j in (i + 1):m
            base = S .- @view(X[chosen[i], :]) .- @view(X[chosen[j], :])
            for di in ranges[i]
                sep_ok(di, chosen, i, j) || continue
                for dj in ranges[j]
                    (abs(dj - di) >= sep && sep_ok(dj, chosen, i, j)) || continue
                    trial .= base .+ @view(X[di, :]) .+ @view(X[dj, :])
                    e = err(trial, m)
                    if e < cur - 1e-12
                        chosen[i], chosen[j], cur = di, dj, e
                        S .= trial
                        improved = true
                    end
                end
            end
            improved && break   # back to single moves
        end
        improved || break
    end
    return chosen
end

"""
    horizon_mean_year(demand, num_years, timesteps_per_year) -> Matrix{Float64}

The mean over the years of each step of the year: the full years `demand`
holds, at most `num_years` (less than a year of data is returned as it is).
"""
function horizon_mean_year(demand::AbstractMatrix{Float64}, num_years::Int,
                           timesteps_per_year::Int)::Matrix{Float64}
    n_full = min(size(demand, 1) ÷ timesteps_per_year, max(num_years, 1))
    n_full < 1 && return Matrix{Float64}(demand)
    return sum(demand[(y - 1) * timesteps_per_year .+ (1:timesteps_per_year), :]
               for y in 1:n_full) ./ n_full
end

"""
    horizon_mean_profile(profile, num_years, timesteps_per_year) -> Vector{Float64}

A profile's mean over the `num_years` years of each step of the year, as the
master reads it (step `t` of year `y` is row `(y − 1) × timesteps_per_year
+ t`, wrapping around a shorter profile): a one-year profile is itself, a
multi-year profile is averaged over its yearly blocks.
"""
function horizon_mean_profile(profile::AbstractVector{Float64}, num_years::Int,
                              timesteps_per_year::Int)::Vector{Float64}
    L = length(profile)
    ny = L <= timesteps_per_year ? 1 : max(num_years, 1)
    return [sum(profile[mod1((y - 1) * timesteps_per_year + t, L)] for y in 1:ny) / ny
            for t in 1:timesteps_per_year]
end

# Day-by-day mean of `series` over its first `num_days` days
day_means_of(series::AbstractVector{Float64}, num_days::Int, timesteps_per_day::Int) =
    [sum(@view series[(d - 1) * timesteps_per_day .+ (1:timesteps_per_day)]) /
     timesteps_per_day for d in 1:num_days]

"""
    representative_day_offsets(demand, num_years, num_days, min_separation,
                               timesteps_per_day, timesteps_per_year;
                               profiles=Vector{Float64}[]) -> Vector{Int}

First step (0-based, within a year) of each representative day, chosen ONCE
for the whole horizon by `select_matching_days`: the peak day of the
horizon-mean demand (`horizon_mean_year`), then one day per segment of the
year such that the days' mean system demand and the days' mean of every
renewable availability profile in `profiles` (each averaged over the years,
`horizon_mean_profile`) match their annual means. The master weighs every
day equally, so this keeps the demand energy and the renewable energy of the
modelled year equal to the real year's (peak days alone overrate demand,
and sunny/windy days overrate renewables). Every year then models the same
calendar days with its own demand and profiles (`master_rep_days`), so the
weather sample does not change from one year to the next.
"""
function representative_day_offsets(demand::Matrix{Float64}, num_years::Int, num_days::Int,
                                    min_separation::Int, timesteps_per_day::Int,
                                    timesteps_per_year::Int;
                                    profiles::AbstractVector = Vector{Float64}[])::Vector{Int}
    (size(demand, 1) > 0 && timesteps_per_year > 0) || return Int[]
    mean_year = horizon_mean_year(demand, num_years, timesteps_per_year)
    D = size(mean_year, 1) ÷ timesteps_per_day
    D >= 1 || return Int[]
    total = vec(sum(mean_year; dims = 2))
    feats = [day_means_of(total, D, timesteps_per_day)]
    for p in profiles
        isempty(p) && continue
        push!(feats, day_means_of(horizon_mean_profile(Vector{Float64}(p), num_years,
                                                       timesteps_per_year),
                                  D, timesteps_per_day))
    end
    peaks = [maximum(@view total[(d - 1) * timesteps_per_day .+ (1:timesteps_per_day)])
             for d in 1:D]
    days = select_matching_days(reduce(hcat, feats), peaks, num_days, min_separation)
    return (days .- 1) .* timesteps_per_day
end

"""
    representative_day_ratios(demand, num_years, offsets, timesteps_per_day,
                              timesteps_per_year; profiles=Vector{Float64}[],
                              weights=Float64[]) -> Vector{Float64}

Mean on the representative periods starting at `offsets` (0-based steps
within a year, `timesteps_per_day` steps each, weighted by `weights`, equal
when empty) over the annual mean: the horizon-mean system demand first, then
each of `profiles` (as in `representative_day_offsets`). 1.0 = the modelled
year has the real year's energy; NaN for a series that is zero all year.
"""
function representative_day_ratios(demand::Matrix{Float64}, num_years::Int,
                                   offsets::AbstractVector{<:Integer}, timesteps_per_day::Int,
                                   timesteps_per_year::Int;
                                   profiles::AbstractVector = Vector{Float64}[],
                                   weights::AbstractVector = Float64[])::Vector{Float64}
    series = Vector{Float64}[vec(sum(horizon_mean_year(demand, num_years,
                                                       timesteps_per_year); dims = 2))]
    for p in profiles
        isempty(p) || push!(series, horizon_mean_profile(Vector{Float64}(p), num_years,
                                                         timesteps_per_year))
    end
    w = isempty(weights) ? ones(length(offsets)) : Float64.(weights)
    return map(series) do s
        L = length(s)
        annual = sum(s) / L
        rep = sum(w[k] * sum(s[mod1(o + t, L)] for t in 1:timesteps_per_day)
                  for (k, o) in enumerate(offsets); init = 0.0) /
              (timesteps_per_day * sum(w; init = 0.0))
        annual > 1e-12 ? rep / annual : NaN
    end
end

"""
    master_re_profiles(input) -> Vector{Vector{Float64}}

The distinct availability profiles of `input`'s renewable generators and
candidate technologies, each the mean over the buses where the unit has
capacity (rated power, investment limit), or over all its columns when it
has none there.
"""
master_re_profiles(input::MasterProblemInput)::Vector{Vector{Float64}} =
    first(master_re_profiles_with_capacity(input))

"""
    master_re_profiles_with_capacity(input) -> (profiles, capacities)

`master_re_profiles` and, for each profile, the renewable capacity behind
it (MW): the rated power of the existing units plus the investment limit of
the candidate technologies that share it, over all buses.
"""
function master_re_profiles_with_capacity(input::MasterProblemInput)
    out = Vector{Float64}[]
    caps = Float64[]
    function add!(avail::AbstractMatrix, cap::AbstractVector)
        size(avail, 1) == 0 && return
        cols = [b for b in axes(avail, 2) if b <= length(cap) && cap[b] > 0]
        isempty(cols) && (cols = collect(axes(avail, 2)))
        p = vec(sum(avail[:, cols]; dims = 2)) ./ length(cols)
        k = findfirst(==(p), out)
        if k === nothing
            push!(out, p)
            push!(caps, 0.0)
            k = length(out)
        end
        caps[k] += sum(max.(cap, 0.0); init = 0.0)
    end
    for g in input.generators
        g.type == "Renewable" && add!(g.availability, g.rated_power)
    end
    for t in input.technologies
        t.type == "Renewable" && add!(t.availability, t.invest_max)
    end
    return out, caps
end

"""
    master_rep_day_offsets(input) -> Vector{Int}

The representative days of `input` as steps within a year (0-based):
`input.rep_day_offsets` when given (the runner chooses them once, for every
master of a run), otherwise `representative_day_offsets` of
`input.base_demand` over `input.years` and the renewable profiles of its
generators and candidate technologies (`master_re_profiles`).
"""
function master_rep_day_offsets(input::MasterProblemInput)::Vector{Int}
    isempty(input.rep_day_offsets) || return input.rep_day_offsets
    tres = input.temporal_resolution_hours
    tpy = (isempty(input.hours_per_year) ? 8760 : input.hours_per_year[1]) ÷ tres
    return representative_day_offsets(input.base_demand, length(input.years),
        input.representative_days_per_year, input.min_day_separation, 24 ÷ tres, tpy;
        profiles = master_re_profiles(input))
end

"""
    master_rep_days(input, y_idx; offsets=master_rep_day_offsets(input)) -> Vector{Int}

First master step of each representative day of year `y_idx`: the same
days of the year (`offsets`) in the year's block of `base_demand` (and of
multi-year profiles). Empty when `base_demand` does not reach the year.
"""
function master_rep_days(input::MasterProblemInput, y_idx::Int;
                         offsets::Vector{Int} = master_rep_day_offsets(input))::Vector{Int}
    tpy = input.hours_per_year[y_idx] ÷ input.temporal_resolution_hours
    year_start = (y_idx - 1) * tpy
    year_start < size(input.base_demand, 1) || return Int[]
    return [year_start + o + 1 for o in offsets]
end

# =============================================================================
# Critical days and storage firm hours (round 33)
# =============================================================================

# Storage firm hours when there is no renewable profile to derive them from
const DEFAULT_STORAGE_FIRM_HOURS = 4.0

"""
    re_supply_index(profiles, capacities, num_years, timesteps_per_year)
        -> (index, weights, means)

The renewable supply a system can expect at each step of the year, relative
to its annual mean (the index has mean 1): the horizon mean of each profile
(`horizon_mean_profile`) × its capacity (MW, existing plus candidate
potential, `master_re_profiles_with_capacity`), summed, so each profile
weighs by the renewable energy it can give. Profiles without capacity (or
no capacities at all) weigh by their relative availability, equally.
`weights[p]` is the MW behind profile `p` in the index (0 for a profile
with no energy), `means[p]` its horizon-mean profile. The index is empty
when no profile has energy.
"""
function re_supply_index(profiles::AbstractVector, capacities::AbstractVector,
                         num_years::Int, timesteps_per_year::Int)
    means = Vector{Float64}[]
    weights = Float64[]
    for (k, p) in enumerate(profiles)
        isempty(p) && continue
        m = horizon_mean_profile(Vector{Float64}(p), num_years, timesteps_per_year)
        push!(means, m)
        push!(weights, k <= length(capacities) ? max(Float64(capacities[k]), 0.0) : 0.0)
    end
    avg = [sum(m) / length(m) for m in means]
    if !any(w * a > 1e-12 for (w, a) in zip(weights, avg))
        weights = [a > 1e-12 ? 1.0 / a : 0.0 for a in avg]   # equal relative weights
    end
    energy = sum(w * a for (w, a) in zip(weights, avg); init = 0.0)
    energy > 1e-12 || return Float64[], zeros(length(means)), means
    index = sum(w .* m for (w, m) in zip(weights, means)) ./ energy
    return index, weights, means
end

# Longest run of `true` in `flags` read cyclically (a day's last step is
# followed by its first: the master's days are cyclic), at most its length
function longest_cyclic_run(flags::AbstractVector{Bool})::Int
    n = length(flags)
    all(flags) && return n
    best = run = 0
    for t in 1:(2 * n)
        run = flags[mod1(t, n)] ? run + 1 : 0
        best = max(best, run)
    end
    return min(best, n)
end

# Largest share of the `num_days` days on which `profile` gives at most 1 %
# of its peak at the same step of the day: about 1 for solar (night), low
# for wind
function dark_hour_share(profile::AbstractVector{Float64}, num_days::Int,
                         timesteps_per_day::Int)::Float64
    thr = 0.01 * maximum(profile)
    L = length(profile)
    return maximum(count(profile[mod1((d - 1) * timesteps_per_day + k, L)] <= thr
                         for d in 1:num_days) / num_days for k in 1:timesteps_per_day)
end

"""
    critical_day_plan(demand, num_years, num_critical, min_separation,
                      timesteps_per_day, timesteps_per_year;
                      profiles=Vector{Float64}[], capacities=Float64[],
                      exclude=Int[]) -> (offsets, firm_hours)

The critical days of a development run, chosen ONCE for the horizon like the
representative days: the `num_critical` days with the largest net load,
the day mean of

    demand / annual mean demand − renewable supply index

on the horizon-mean system demand (`horizon_mean_year`) and the horizon-mean
renewable profiles weighted by the capacity behind them (`re_supply_index`,
renewable supply scaled to the annual demand): the days on which renewables
cover the least of the demand. Days already modelled (`exclude`, first steps
0-based) are skipped, and the critical days are `min_separation` days apart
when the year allows it (one dark spell gives one day). `offsets` are the
first steps (0-based, within a year), the most critical first.

`firm_hours` is the length of the critical period storage must cover on
those days (the worst day when `num_critical` is 0), in hours: the longest
stretch of a critical day without solar (the steps where the profiles that
are dark at the same hour on at least 90 % of days, the solar ones, give at
most 1 % of their peak), read cyclically within the day; with no solar
profile, the longest stretch whose renewable supply stays below the demand;
$(DEFAULT_STORAGE_FIRM_HOURS) h without renewable profiles. At least one
step, at most 24 h.
"""
function critical_day_plan(demand::Matrix{Float64}, num_years::Int, num_critical::Int,
                           min_separation::Int, timesteps_per_day::Int,
                           timesteps_per_year::Int;
                           profiles::AbstractVector = Vector{Float64}[],
                           capacities::AbstractVector = Float64[],
                           exclude::AbstractVector = Int[])
    tpd = max(timesteps_per_day, 1)
    step_h = 24.0 / tpd
    (size(demand, 1) > 0 && timesteps_per_year > 0) ||
        return Int[], DEFAULT_STORAGE_FIRM_HOURS
    mean_year = horizon_mean_year(demand, num_years, timesteps_per_year)
    D = min(size(mean_year, 1), timesteps_per_year) ÷ tpd
    D >= 1 || return Int[], DEFAULT_STORAGE_FIRM_HOURS
    L = D * tpd
    total = vec(sum(mean_year; dims = 2))[1:L]
    dbar = sum(total) / L
    d_rel = dbar > 1e-12 ? total ./ dbar : zeros(L)
    s, w, means = re_supply_index(profiles, capacities, num_years, timesteps_per_year)
    s_rel = isempty(s) ? zeros(L) : [s[mod1(t, length(s))] for t in 1:L]
    net = day_means_of(d_rel .- s_rel, D, tpd)

    # The most critical days, apart from the modelled ones and each other
    taken = Set(o ÷ tpd + 1 for o in exclude)
    order = sortperm(net; rev = true)
    chosen = Int[]
    for sep in unique([max(min_separation, 1), 1])
        for d in order
            length(chosen) >= num_critical && break
            (d in taken || d in chosen) && continue
            all(abs(d - c) >= sep for c in chosen) || continue
            push!(chosen, d)
        end
    end

    # The critical period storage must cover on those days
    firm_days = isempty(chosen) ? [order[1]] : chosen
    dark = if isempty(s)
        nothing
    else
        solar = [k for (k, m) in enumerate(means)
                 if w[k] > 0 && maximum(m) > 1e-12 && dark_hour_share(m, D, tpd) >= 0.9]
        if isempty(solar)
            s_rel .< d_rel
        else
            sol = sum(w[k] .* means[k] for k in solar)
            sol_rel = [sol[mod1(t, length(sol))] for t in 1:L]
            sol_rel .<= 0.01 * maximum(sol_rel)
        end
    end
    firm_hours = if dark === nothing
        DEFAULT_STORAGE_FIRM_HOURS
    else
        steps = maximum(longest_cyclic_run(dark[(d - 1) * tpd .+ (1:tpd)]) for d in firm_days)
        clamp(steps * step_h, step_h, 24.0)
    end
    return (chosen .- 1) .* tpd, firm_hours
end

"""
    representative_day_weights(demand, num_years, offsets, critical_offsets,
                               timesteps_per_day, timesteps_per_year;
                               profiles=Vector{Float64}[], prior=Float64[])
        -> Vector{Float64}

Days of the year each modelled day stands for: the representative days (or
TSAM periods) starting at `offsets`, then the critical days starting at
`critical_offsets` (0-based steps within a year). Each critical day stands
for itself (1 day). The representative days share the rest of the year:
their `prior` weights (cluster sizes; equal when empty), scaled to the days
left, then corrected (least change, a ridge least-squares step with the
weights' sum kept) so that the year the modelled days stand for has the
same mean system demand and mean of every renewable profile in `profiles` as
the representative days alone with their prior weights: adding the critical
days does not change the year's demand or renewable energy. A weight never
falls below a fifth of its prior; the correction is dropped if it does not
lower the mismatch. Without critical days, the prior (or equal) weights.
"""
function representative_day_weights(demand::Matrix{Float64}, num_years::Int,
                                    offsets::AbstractVector{<:Integer},
                                    critical_offsets::AbstractVector{<:Integer},
                                    timesteps_per_day::Int, timesteps_per_year::Int;
                                    profiles::AbstractVector = Vector{Float64}[],
                                    prior::AbstractVector = Float64[])::Vector{Float64}
    tpd = max(timesteps_per_day, 1)
    D = timesteps_per_year ÷ tpd
    nr, nc = length(offsets), length(critical_offsets)
    nr >= 1 || return ones(nc)
    p0 = length(prior) == nr ? Float64.(prior) : ones(nr)
    sum(p0) > 0 || (p0 = ones(nr))
    p0 .*= D / sum(p0)                 # the representative days alone: the year
    nc >= 1 || return p0
    nc < D || return vcat(fill(1e-9, nr), ones(nc))
    p = p0 .* ((D - nc) / D)           # the year's days the critical ones leave

    # Relative day means (annual mean = 1) of each series with energy
    series = Vector{Float64}[vec(sum(horizon_mean_year(demand, num_years,
                                                       timesteps_per_year); dims = 2))]
    for q in profiles
        isempty(q) || push!(series, horizon_mean_profile(Vector{Float64}(q), num_years,
                                                         timesteps_per_year))
    end
    day_rel(sv, o, annual) = sum(sv[mod1(o + t, length(sv))] for t in 1:tpd) / tpd / annual
    rows_r = Vector{Float64}[]
    rows_c = Vector{Float64}[]
    for sv in series
        annual = sum(sv) / length(sv)
        annual > 1e-12 || continue
        push!(rows_r, [day_rel(sv, o, annual) for o in offsets])
        push!(rows_c, [day_rel(sv, o, annual) for o in critical_offsets])
    end
    isempty(rows_r) && return vcat(p, ones(nc))
    A = permutedims(reduce(hcat, rows_r))      # features × representative days
    C = permutedims(reduce(hcat, rows_c))      # features × critical days
    target = A * p0                            # the representative days' year
    resid(wr) = (A * wr .+ C * ones(nc) .- target) ./ D
    # δ = p .* z with Σ δ = 0: min ‖resid(p + δ)‖² + λ‖z‖²
    M = (A .* p') ./ D
    r = -resid(p)
    λ = 1e-4
    K = [M' * M + λ * I  p; p'  0.0]
    z = try
        (K \ vcat(M' * r, 0.0))[1:nr]
    catch
        zeros(nr)
    end
    wr = p .* (1 .+ max.(z, -0.8))
    wr .*= (D - nc) / sum(wr)
    if sum(abs2, resid(wr)) >= sum(abs2, resid(p))
        wr = p
    end
    return vcat(wr, ones(nc))
end

"""
    master_day_plan(input) -> (offsets, weights, num_rep)

The days the master models in every year (first steps, 0-based, within a
year) and the days of the year each stands for: the representative days
(`master_rep_day_offsets`), then the critical days. The critical days are
`input.critical_day_offsets` when the representative days are given (the
runner chooses both once for a run), otherwise `input.critical_days` days
chosen by `critical_day_plan`. The weights are `input.rep_day_weights` when
they match, otherwise `representative_day_weights` (the year's days shared
equally by the representative days when there are no critical days).
`num_rep` is the number of representative days.
"""
function master_day_plan(input::MasterProblemInput)
    rep = master_rep_day_offsets(input)
    tres = input.temporal_resolution_hours
    tpd = 24 ÷ tres
    tpy = (isempty(input.hours_per_year) ? 8760 : input.hours_per_year[1]) ÷ tres
    ny = length(input.years)
    crit = if !isempty(input.rep_day_offsets)
        input.critical_day_offsets
    elseif input.critical_days > 0
        profiles, caps = master_re_profiles_with_capacity(input)
        first(critical_day_plan(input.base_demand, ny, input.critical_days,
            input.min_day_separation, tpd, tpy; profiles, capacities = caps, exclude = rep))
    else
        Int[]
    end
    offsets = vcat(rep, crit)
    weights = if length(input.rep_day_weights) == length(offsets)
        input.rep_day_weights
    elseif isempty(crit)
        fill(tpy / tpd / max(length(rep), 1), length(rep))
    else
        representative_day_weights(input.base_demand, ny, rep, crit, tpd, tpy;
            profiles = master_re_profiles(input))
    end
    return offsets, Float64.(weights), length(rep)
end

"""
    master_storage_firm_hours(input) -> Float64

Hours a battery must sustain its discharge to count as firm power in the
capacity adequacy constraint: `input.storage_firm_hours` when positive,
otherwise derived from the data (`critical_day_plan`: the longest stretch
without solar on the critical days).
"""
function master_storage_firm_hours(input::MasterProblemInput)::Float64
    input.storage_firm_hours > 0 && return input.storage_firm_hours
    tres = input.temporal_resolution_hours
    tpd = 24 ÷ tres
    tpy = (isempty(input.hours_per_year) ? 8760 : input.hours_per_year[1]) ÷ tres
    profiles, caps = master_re_profiles_with_capacity(input)
    return last(critical_day_plan(input.base_demand, length(input.years),
        max(input.critical_days, length(input.critical_day_offsets), 1),
        input.min_day_separation, tpd, tpy;
        profiles, capacities = caps, exclude = master_rep_day_offsets(input)))
end

# =============================================================================
# Investment Period Helpers
# =============================================================================

"""
    inv_period_year(y_idx, years_per_inv_period)

Return the investment period start year for a given year index.
Investment decisions are grouped into periods of `years_per_inv_period` years.
E.g., with years_per_inv_period=5: years 1-5 → period start 1, years 6-10 → 6.
"""
@inline function inv_period_year(y_idx::Int, years_per_inv_period::Int)::Int
    return ((y_idx - 1) ÷ years_per_inv_period) * years_per_inv_period + 1
end

"""
    is_inv_period_start(y_idx, years_per_inv_period)

Return true if y_idx is the first year of an investment period.
"""
@inline function is_inv_period_start(y_idx::Int, years_per_inv_period::Int)::Bool
    return (y_idx - 1) % years_per_inv_period == 0
end

# =============================================================================
# Variable Creation
# =============================================================================

"""
    build_master_variables!(model, input)

Create all Master Problem decision variables.
"""
function build_master_variables!(
    model::Model,
    input::MasterProblemInput
)::MasterProblemVariables
    num_years = length(input.years)
    n_buses = input.network.num_buses
    n_gen = length(input.generators)
    n_bat = length(input.batteries)

    # Investment period grouping from investment_resolution_hours
    years_per_inv_period = max(1, input.investment_resolution_hours ÷ 8760)
    num_inv_periods = cld(num_years, years_per_inv_period)  # ceiling division
    @info "Investment periods: $(num_inv_periods) periods of $(years_per_inv_period) year(s) each"

    # Technology investment variables (per-technology, not per-generator)
    n_tech = length(input.technologies)
    n_bat_tech = length(input.battery_technologies)
    tech_investment = Dict{Int, Dict{Int, Vector{VariableRef}}}()
    bat_tech_power_investment = Dict{Int, Dict{Int, Vector{VariableRef}}}()
    bat_tech_capacity_investment = Dict{Int, Dict{Int, Vector{VariableRef}}}()
    transfer_investment = Dict{Int, Dict{Tuple{Int,Int}, VariableRef}}()
    corridors = Set{Tuple{Int,Int}}()
    for (i, j) in TransmissionDC(input.network).lines
        push!(corridors, (i, j), (j, i))
    end

    # Life extension variables
    gen_life_extension = Dict{Int, Dict{Int, Vector{Union{VariableRef, Nothing}}}}()
    bat_life_extension = Dict{Int, Dict{Int, Vector{Union{VariableRef, Nothing}}}}()

    # Renewable generation over demand per (system, year), the growth cap's
    # link between years (define_re_share!); with no system_node_ranges,
    # system 1 covers all buses
    re_energy_ratio = Dict{Tuple{Int,Int}, Union{VariableRef, AffExpr}}()

    # Slack variables (the RE target's come with the representative days,
    # add_re_target_constraints!)
    slack_re_target = Dict{Tuple{Int,Int,Int}, VariableRef}()
    slack_capacity = Dict{Tuple{Int,Int}, VariableRef}()
    slack_budget = Dict{Int, VariableRef}()

    b2n = input.network.bus_to_node  # bus index → node index mapping

    for y_idx in 1:num_years
        # Only create investment variables at investment period boundaries.
        # Non-start years share the same variables as their period's start year.
        if !is_inv_period_start(y_idx, years_per_inv_period)
            # Skip investment variable creation — cumulative sums iterate
            # only over period-start years via step `years_per_inv_period`.
        else
            # Technology investment per bus (per-technology, not per-generator)
            tech_investment[y_idx] = Dict{Int, Vector{VariableRef}}()
            for t in 1:n_tech
                tech = input.technologies[t]
                tech_investment[y_idx][t] = @variable(model,
                    [b=1:n_buses],
                    lower_bound = 0,
                    upper_bound = tech.invest_max[b],
                    base_name = "tech_inv_y$(y_idx)_t$(t)"
                )
            end

            # Battery technology power investment per bus
            bat_tech_power_investment[y_idx] = Dict{Int, Vector{VariableRef}}()
            for bt in 1:n_bat_tech
                btech = input.battery_technologies[bt]
                bat_tech_power_investment[y_idx][bt] = @variable(model,
                    [b=1:n_buses],
                    lower_bound = 0,
                    upper_bound = btech.invest_max_power[b],
                    base_name = "bat_tech_pow_inv_y$(y_idx)_bt$(bt)"
                )
            end

            # Battery technology capacity investment per bus
            bat_tech_capacity_investment[y_idx] = Dict{Int, Vector{VariableRef}}()
            for bt in 1:n_bat_tech
                btech = input.battery_technologies[bt]
                bat_tech_capacity_investment[y_idx][bt] = @variable(model,
                    [b=1:n_buses],
                    lower_bound = 0,
                    upper_bound = btech.invest_max_capacity[b],
                    base_name = "bat_tech_cap_inv_y$(y_idx)_bt$(bt)"
                )
            end

            # Transmission investment, only on corridors that exist as a line
            # or transformer (a candidate corridor may start at 0 MW). Capacity
            # bought between buses with no line never reaches the dispatch.
            transfer_investment[y_idx] = Dict{Tuple{Int,Int}, VariableRef}()
            for i in 1:n_buses
                for j in 1:n_buses
                    if i != j && (i, j) in corridors &&
                       (input.network.connections[i, j] > 0 || input.network.transference_invest_max[i] > 0)
                        # Upper bound from transference_invest_max (use bus i as reference)
                        max_inv = input.network.transference_invest_max[i]
                        transfer_investment[y_idx][(i, j)] = @variable(model,
                            lower_bound = 0,
                            upper_bound = max_inv,
                            base_name = "trans_inv_y$(y_idx)_$(i)_$(j)"
                        )
                    end
                end
            end
        end  # is_inv_period_start

        # Life extension variables: continuous variables for units past lifetime
        # Allows optimizer to choose how much capacity to keep online (LP-friendly)
        gen_life_extension[y_idx] = Dict{Int, Vector{Union{VariableRef, Nothing}}}()
        for g in 1:n_gen
            gen = input.generators[g]
            gen_life_extension[y_idx][g] = Vector{Union{VariableRef, Nothing}}(nothing, n_buses)
            for n in 1:n_buses
                rated_power = gen.rated_power[n]
                lifetime = gen.life_time[n]
                initial_age = gen.initial_age[n]
                age_at_year = initial_age + y_idx - 1
                degradation_rate = gen.degradation_rate[n]
                if age_at_year >= lifetime && rated_power > 0
                    deg_factor = (1.0 - degradation_rate) ^ age_at_year
                    gen_life_extension[y_idx][g][n] = @variable(model,
                        lower_bound = 0,
                        upper_bound = rated_power * deg_factor,
                        base_name = "gen_life_ext_y$(y_idx)_g$(g)_n$(n)"
                    )
                end
            end
        end

        bat_life_extension[y_idx] = Dict{Int, Vector{Union{VariableRef, Nothing}}}()
        for bi in 1:n_bat
            bat = input.batteries[bi]
            bat_life_extension[y_idx][bi] = Vector{Union{VariableRef, Nothing}}(nothing, n_buses)
            for n in 1:n_buses
                base_power = bat.max_discharge_power[n]
                lifetime = bat.life_time[n]
                initial_age = bat.initial_age[n]
                age_at_year = initial_age + y_idx - 1
                if age_at_year >= lifetime && base_power > 0
                    bat_life_extension[y_idx][bi][n] = @variable(model,
                        lower_bound = 0,
                        upper_bound = base_power * battery_degradation_factor(bat, n, y_idx),
                        base_name = "bat_life_ext_y$(y_idx)_b$(bi)_n$(n)"
                    )
                end
            end
        end

        # Renewable generation over demand per system: no upper bound, the
        # renewable energy a system uses may pass its demand (storage
        # losses, exports)
        for s_idx in eachindex(master_systems(input))
            re_energy_ratio[(s_idx, y_idx)] = @variable(model,
                lower_bound = 0,
                base_name = "re_ratio_s$(s_idx)_y$(y_idx)"
            )
        end

        # Slack variables
        slack_budget[y_idx] = @variable(model,
            lower_bound = 0,
            base_name = "slack_budget_y$(y_idx)"
        )

        # Slack capacity per NODE (not per bus) — generators on different buses
        # of the same node can serve demand on any bus via internal transmission
        for ni in 1:input.network.num_nodes
            slack_capacity[(y_idx, ni)] = @variable(model,
                lower_bound = 0,
                base_name = "slack_cap_y$(y_idx)_node$(ni)"
            )
        end
    end

    # Fuel storage, route transport and electrolyzers: build_master_fuel_variables!
    # (master_fuel.jl), part of the returned variables below.

    # Reservoir capacity investment variables (MWh), at the investment-period
    # starts like the other builds; other years keep an empty dict
    reservoir_investment = Dict{Int, Dict{Int, Vector{VariableRef}}}()
    for y_idx in 1:num_years
        reservoir_investment[y_idx] = Dict{Int, Vector{VariableRef}}()
        is_inv_period_start(y_idx, years_per_inv_period) || continue
        for g in 1:n_gen
            gen = input.generators[g]
            if any(gen.reservoir_invest_max .> 0)
                reservoir_investment[y_idx][g] = @variable(model,
                    [b=1:n_buses],
                    lower_bound = 0,
                    upper_bound = gen.reservoir_invest_max[b],
                    base_name = "res_inv_y$(y_idx)_g$(g)"
                )
            end
        end
    end

    # Converter investment (MW) at period starts, for AC/DC and frequency
    # converters that allow it (invest_max_power > 0)
    converter_investment = Dict{Int, Dict{Tuple{String,Int}, VariableRef}}()
    for y_idx in 1:years_per_inv_period:num_years
        converter_investment[y_idx] = Dict{Tuple{String,Int}, VariableRef}()
        for (key, conv) in master_converters(input.network)
            conv.invest_max_power > 0 || continue
            converter_investment[y_idx][key] = @variable(model,
                lower_bound = 0, upper_bound = conv.invest_max_power,
                base_name = "conv_inv_y$(y_idx)_$(key[1])$(key[2])")
        end
    end

    return MasterProblemVariables(
        tech_investment,
        bat_tech_power_investment,
        bat_tech_capacity_investment,
        transfer_investment,
        gen_life_extension,
        bat_life_extension,
        re_energy_ratio,
        Dict{Tuple{Int,Int}, Tuple{Model, PowerSystemVariables}}(),
        Dict{Int, Vector{AffExpr}}(),
        slack_re_target,
        slack_capacity,
        slack_budget,
        Dict{Int, Dict{Tuple{Int,Int,Int}, VariableRef}}(),  # inter_period_soc (TSAM)
        Dict{Int, Dict{Tuple{Int,Int,Int}, VariableRef}}(),  # inter_period_reservoir (TSAM seasonal hydro)
        reservoir_investment,
        years_per_inv_period,
        converter_investment,
        Dict{Tuple{Int,Int,Int}, VariableRef}(),  # co2_budget_violation
        build_master_fuel_variables!(model, input, years_per_inv_period),  # M2 (#89)
        Dict{Tuple, AffExpr}()  # capacity_state: add_capacity_states!
    )
end

"""
    master_converters(network)

The AC/DC and frequency converters of `network` as `(key, converter)` pairs,
keyed `("acdc", c)` / `("freq", c)` like `converter_investment`.
"""
master_converters(network::NetworkConfig) = vcat(
    [(("acdc", c), conv) for (c, conv) in enumerate(network.acdc_converters)],
    [(("freq", c), conv) for (c, conv) in enumerate(network.freq_converters)])

"""
    tech_capacity_built(vars, input, t, b, year_idx) -> AffExpr

MW of candidate technology `t` at bus `b` in service in year `year_idx`:
each build at an investment-period start up to that year, degraded by its
age, `Σ (1 − degradation_rate)^age × tech_investment`, while younger than
the technology's `life_time`.
"""
tech_capacity_built(vars::MasterProblemVariables, input::MasterProblemInput,
                    t::Int, b::Int, year_idx::Int; state::Bool = true) =
    capacity_in_service(vars, (:tech, year_idx, t, b); state) do
        tech = input.technologies[t]
        cap = AffExpr(0.0)
        for y in 1:vars.years_per_inv_period:year_idx
            age = year_idx - y
            age < tech.life_time[b] || continue
            add_to_expression!(cap, vars.tech_investment[y][t][b],
                               (1.0 - tech.degradation_rate[b])^age)
        end
        cap
    end

"""
    bat_tech_degradation_factor(btech, b, age) -> Float64

Share of the power and energy of battery technology `btech` built at bus `b`
left `age` years after its build year: `(1 − degradation_rate)^age`, the
convention of `battery_degradation_factor` (age 0 in the build year, as an
existing battery of `initial_age` 0 in year 1).
"""
function bat_tech_degradation_factor(btech, b::Int, age::Real)::Float64
    d = length(btech.degradation_rate) >= b ? btech.degradation_rate[b] : 0.0
    d > 0 || return 1.0
    return (1.0 - d)^age
end

"""
    bat_tech_power_built(vars, input, bt, b, year_idx) -> AffExpr
    bat_tech_energy_built(vars, input, bt, b, year_idx) -> AffExpr

Discharge power (MW) and energy (MWh) of battery technology `bt` at bus `b`
in service in year `year_idx`: each build at an investment-period start up
to that year, degraded by its age (`bat_tech_degradation_factor`, as the
dispatch ages the battery it becomes), while younger than the technology's
`life_time`. Charge power equals discharge power.
"""
bat_tech_power_built(vars::MasterProblemVariables, input::MasterProblemInput,
                     bt::Int, b::Int, year_idx::Int; state::Bool = true) =
    capacity_in_service(vars, (:bat_tech_power, year_idx, bt, b); state) do
        _bat_tech_built(vars, input, bt, b, year_idx, vars.bat_tech_power_investment)
    end

bat_tech_energy_built(vars::MasterProblemVariables, input::MasterProblemInput,
                      bt::Int, b::Int, year_idx::Int; state::Bool = true) =
    capacity_in_service(vars, (:bat_tech_energy, year_idx, bt, b); state) do
        _bat_tech_built(vars, input, bt, b, year_idx, vars.bat_tech_capacity_investment)
    end

"""
    bat_tech_power_rated(vars, input, bt, b, year_idx) -> AffExpr

Rated (not degraded) discharge power of battery technology `bt` at bus `b`
built up to year `year_idx` and still within its `life_time`: what its
fixed O&M is charged on, as for an existing battery.
"""
bat_tech_power_rated(vars::MasterProblemVariables, input::MasterProblemInput,
                     bt::Int, b::Int, year_idx::Int) =
    _bat_tech_built(vars, input, bt, b, year_idx, vars.bat_tech_power_investment;
                    degraded = false)

function _bat_tech_built(vars, input, bt, b, year_idx, inv; degraded::Bool = true)
    btech = input.battery_technologies[bt]
    cap = AffExpr(0.0)
    for y in 1:vars.years_per_inv_period:year_idx
        age = year_idx - y
        age < btech.life_time[b] || continue
        add_to_expression!(cap, inv[y][bt][b],
                           degraded ? bat_tech_degradation_factor(btech, b, age) : 1.0)
    end
    return cap
end

"""
    transfer_capacity_built(vars, from, to, year_idx) -> Union{Nothing, AffExpr}

MW built on the corridor between buses `from` and `to` up to year
`year_idx` (transmission builds do not retire), on the investment variable
`corridor_investment` picks; `nothing` when the corridor cannot be expanded.
"""
function transfer_capacity_built(vars::MasterProblemVariables, from::Int, to::Int,
                                 year_idx::Int)
    inv = get(vars.transfer_investment, 1, nothing)
    inv === nothing && return nothing
    i, j = haskey(inv, (from, to)) ? (from, to) :
           haskey(inv, (to, from)) ? (to, from) : (0, 0)
    i == 0 && return nothing
    return capacity_in_service(vars, (:transfer, year_idx, i, j)) do
        cap = AffExpr(0.0)
        for y in 1:vars.years_per_inv_period:year_idx
            v = get(get(vars.transfer_investment, y, Dict{Tuple{Int,Int}, VariableRef}()),
                    (i, j), nothing)
            v === nothing || add_to_expression!(cap, v)
        end
        cap
    end
end

"""
    transfer_expansion_max(vars, from, to, year_idx) -> Float64

The most MW the corridor between buses `from` and `to` can have gained by
year `year_idx`: the upper bounds of its investment variables at the
investment-period starts (`Inf` if one has none, 0 without any). It bounds
the last loss segment of the corridor's lines (`add_dc_constraints!`).
"""
function transfer_expansion_max(vars::MasterProblemVariables, from::Int, to::Int,
                                year_idx::Int)::Float64
    total = 0.0
    for y in 1:vars.years_per_inv_period:year_idx
        inv = get(vars.transfer_investment, y, nothing)
        inv === nothing && continue
        v = get(inv, (from, to), get(inv, (to, from), nothing))
        v === nothing && continue
        total += has_upper_bound(v) ? max(upper_bound(v), 0.0) : Inf
    end
    return total
end

"""
    reservoir_capacity_built(vars, g, b, year_idx) -> AffExpr

Reservoir capacity (MWh) of generator `g` at bus `b` built at the
investment-period starts up to year `year_idx`. Reservoirs do not retire.
"""
reservoir_capacity_built(vars::MasterProblemVariables, g::Int, b::Int, year_idx::Int) =
    capacity_in_service(vars, (:reservoir, year_idx, g, b)) do
        built = AffExpr(0.0)
        for y in 1:vars.years_per_inv_period:year_idx
            v = get(get(vars.reservoir_investment, y, Dict{Int, Vector{VariableRef}}()), g, nothing)
            v === nothing || add_to_expression!(built, v[b])
        end
        built
    end

"""
    reservoir_in_service(vars, gen, g, b, year_idx) -> Union{Float64, AffExpr}

In-service reservoir capacity (MWh) of generator `gen` (index `g`) at bus `b`
in year `year_idx`: the existing capacity plus, when the reservoir can be
enlarged, the capacity built so far. Every master constraint that scales with
the reservoir size (level bounds, TSAM inter-day levels, head fraction,
initial/end level) uses this one.
"""
function reservoir_in_service(vars::MasterProblemVariables, gen, g::Int, b::Int, year_idx::Int)
    cap = gen.reservoir_capacity[b]
    (cap > 0 && b <= length(gen.reservoir_invest_max) && gen.reservoir_invest_max[b] > 0) ||
        return cap
    return cap + reservoir_capacity_built(vars, g, b, year_idx)
end

"""
    battery_degradation_factor(bat, b, year_idx) -> Float64

Share of existing battery `bat`'s rated power and energy at bus `b` left in
year `year_idx`: `(1 − degradation_rate)^age` with
`age = initial_age + year_idx − 1`, as the dispatch's `battery_power_limits`
ages it (year 1 is the dispatch's base year).
"""
function battery_degradation_factor(bat, b::Int, year_idx::Int)::Float64
    d = length(bat.degradation_rate) >= b ? bat.degradation_rate[b] : 0.0
    d > 0 || return 1.0
    return (1.0 - d)^(bat.initial_age[b] + (year_idx - 1))
end

"""
    battery_power_in_service(vars, bat, bi, b, year_idx) -> AffExpr
    battery_energy_in_service(vars, bat, bi, b, year_idx) -> AffExpr

Discharge power (MW) and energy capacity (MWh) of existing battery `bat`
(index `bi`) at bus `b` in service in year `year_idx`: its rated values
degraded by its age (`battery_degradation_factor`, as the dispatch) while
younger than its `life_time`; afterwards the MW the master extends its life
by (`bat_life_extension`, at most the degraded power), the energy at the
battery's own energy-to-power ratio; 0 if not extended. Every master
constraint on an existing battery's size (the day's power and SOC bounds,
the TSAM inter-day SOC, the firm capacity, the reported capacity) uses these.
"""
function battery_power_in_service(vars::MasterProblemVariables, bat, bi::Int, b::Int,
                                  year_idx::Int)
    cap = AffExpr(0.0)
    base_power = bat.max_discharge_power[b]
    base_power > 0 || return cap
    if bat.initial_age[b] + (year_idx - 1) < bat.life_time[b]
        add_to_expression!(cap, base_power * battery_degradation_factor(bat, b, year_idx))
    else
        ext = vars.bat_life_extension[year_idx][bi][b]
        ext === nothing || add_to_expression!(cap, ext)
    end
    return cap
end

function battery_energy_in_service(vars::MasterProblemVariables, bat, bi::Int, b::Int,
                                   year_idx::Int)
    cap = AffExpr(0.0)
    base_cap = bat.capacity[b]
    base_cap > 0 || return cap
    base_power = bat.max_discharge_power[b]
    if bat.initial_age[b] + (year_idx - 1) < bat.life_time[b]
        add_to_expression!(cap, base_cap * battery_degradation_factor(bat, b, year_idx))
    elseif base_power > 0
        ext = vars.bat_life_extension[year_idx][bi][b]
        ext === nothing || add_to_expression!(cap, ext, base_cap / base_power)
    end
    return cap
end

"""
    converter_capacity(vars, conv, key, year_idx)

Converter capacity in year `year_idx`: its rated power plus the MW built at
the investment-period starts up to that year (`converter_built`). As in the
dispatch, a converter does not age or retire.
"""
function converter_capacity(vars::MasterProblemVariables, conv, key::Tuple{String,Int},
                            year_idx::Int)
    cap = converter_built(vars, key, year_idx)
    add_to_expression!(cap, conv.rated_power_mva)
    return cap
end

converter_built(vars::MasterProblemVariables, key::Tuple{String,Int}, year_idx::Int;
                state::Bool = true) =
    capacity_in_service(vars, (:converter, year_idx, key[1], key[2]); state) do
        built = AffExpr(0.0)
        for y in 1:vars.years_per_inv_period:year_idx
            v = get(get(vars.converter_investment, y, Dict{Tuple{String,Int}, VariableRef}()),
                    key, nothing)
            v === nothing || add_to_expression!(built, v)
        end
        built
    end

"""
    master_fuel_capacity(vars, input, year_idx, key) -> AffExpr

`master_fuel_extra_expr` (fuel storage, route transport or electrolyzer
capacity built and in service) through its state variable.
"""
master_fuel_capacity(vars::MasterProblemVariables, input::MasterProblemInput,
                     year_idx::Int, key::Tuple{Symbol,String,Int}) =
    capacity_in_service(vars, (:fuel, year_idx, key)) do
        master_fuel_extra_expr(vars.fuel, input, year_idx, key)
    end

"""
    capacity_in_service(definition, vars, key; state=true) -> AffExpr

The capacity `key` stands for: its state variable when the model has one
(`add_capacity_states!`) and `state` is on, otherwise `definition()`, the
sum over the investment variables (the objective's fixed costs keep the
sum). Always a fresh expression the caller may extend.
"""
function capacity_in_service(definition::Function, vars::MasterProblemVariables, key::Tuple;
                             state::Bool = true)
    s = state ? get(vars.capacity_state, key, nothing) : nothing
    return s === nothing ? definition() : copy(s)
end

"""
    add_capacity_states!(model, vars, input)

One state variable per year for the capacity built and still in service of
each candidate technology and battery technology (power and energy) per bus,
expandable corridor, reservoir, converter and fuel build, defined by one
equality, e.g. for a technology

    cap[y, t, b] = Σ_{y' ≤ y, age < life_time} (1 − degradation_rate)^(y − y') × tech_investment[y', t, b]

The hourly rows of the representative days (output, reserve, curtailment,
battery, line, reservoir, converter and fuel limits) then hold `cap[y, …]`
instead of the sum, so each investment variable appears only in these
definitions and in a few yearly rows (build limits, budget, adequacy,
objective) rather than in every hour of every later year: the same LP, with
far fewer nonzeros and no dense investment columns for interior-point
solvers. A capacity with no investment variable (e.g. no build before its
first investment period) is the constant 0.
"""
function add_capacity_states!(model::Model, vars::MasterProblemVariables,
                              input::MasterProblemInput)
    function state!(key, expr::AffExpr, name::String)
        if isempty(expr.terms)
            vars.capacity_state[key] = AffExpr(0.0)
            return
        end
        cap = @variable(model, lower_bound = 0, base_name = name)
        @constraint(model, cap == expr, base_name = "def_" * name)
        vars.capacity_state[key] = AffExpr(0.0, cap => 1.0)
    end
    n_buses = input.network.num_buses
    corridors = collect(keys(get(vars.transfer_investment, 1, Dict{Tuple{Int,Int}, VariableRef}())))
    reservoirs = collect(keys(get(vars.reservoir_investment, 1, Dict{Int, Vector{VariableRef}}())))
    converters = collect(keys(get(vars.converter_investment, 1, Dict{Tuple{String,Int}, VariableRef}())))
    fuel_keys = vars.fuel === nothing ? MasterFuelKey[] : master_fuel_keys(input)
    for y in eachindex(input.years)
        for t in eachindex(input.technologies), b in 1:n_buses
            state!((:tech, y, t, b), tech_capacity_built(vars, input, t, b, y),
                   "cap_tech_y$(y)_t$(t)_b$(b)")
        end
        for bt in eachindex(input.battery_technologies), b in 1:n_buses
            state!((:bat_tech_power, y, bt, b), bat_tech_power_built(vars, input, bt, b, y),
                   "cap_bt_pow_y$(y)_bt$(bt)_b$(b)")
            state!((:bat_tech_energy, y, bt, b), bat_tech_energy_built(vars, input, bt, b, y),
                   "cap_bt_en_y$(y)_bt$(bt)_b$(b)")
        end
        for (i, j) in corridors
            state!((:transfer, y, i, j), transfer_capacity_built(vars, i, j, y),
                   "cap_trans_y$(y)_$(i)_$(j)")
        end
        for g in reservoirs, b in 1:n_buses
            state!((:reservoir, y, g, b), reservoir_capacity_built(vars, g, b, y),
                   "cap_res_y$(y)_g$(g)_b$(b)")
        end
        for key in converters
            state!((:converter, y, key[1], key[2]), converter_built(vars, key, y),
                   "cap_conv_y$(y)_$(key[1])$(key[2])")
        end
        for k in fuel_keys
            state!((:fuel, y, k), master_fuel_capacity(vars, input, y, k),
                   "cap_fuel_y$(y)_$(k[1])_$(k[2])_$(k[3])")
        end
    end
    return nothing
end

# =============================================================================
# Constraints
# =============================================================================

"""
    add_investment_constraints!(model, vars, input)

Add cumulative investment limit constraints.
"""
function add_investment_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)
    num_years = length(input.years)
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node
    n_gen = length(input.generators)
    n_bat = length(input.batteries)

    # Technology cumulative investment limits per bus
    n_tech = length(input.technologies)
    n_bat_tech = length(input.battery_technologies)
    ypp = vars.years_per_inv_period  # shorthand
    for t in 1:n_tech
        tech = input.technologies[t]
        for b in 1:n_buses
            max_total = tech.invest_max[b]
            if max_total > 0
                cumulative = sum(vars.tech_investment[y][t][b] for y in 1:ypp:num_years)
                @constraint(model,
                    cumulative <= max_total,
                    base_name = "cumul_tech_t$(t)_b$(b)"
                )
            end
        end
    end

    # Battery technology cumulative investment limits per bus
    for bt in 1:n_bat_tech
        btech = input.battery_technologies[bt]
        for b in 1:n_buses
            # Power limit
            max_power = btech.invest_max_power[b]
            if max_power > 0
                cumulative_power = sum(vars.bat_tech_power_investment[y][bt][b] for y in 1:ypp:num_years)
                @constraint(model,
                    cumulative_power <= max_power,
                    base_name = "cumul_bat_tech_pow_bt$(bt)_b$(b)"
                )
            end

            # Capacity limit
            max_cap = btech.invest_max_capacity[b]
            if max_cap > 0
                cumulative_cap = sum(vars.bat_tech_capacity_investment[y][bt][b] for y in 1:ypp:num_years)
                @constraint(model,
                    cumulative_cap <= max_cap,
                    base_name = "cumul_bat_tech_cap_bt$(bt)_b$(b)"
                )
            end
        end
    end

    # Battery technology duration constraints (energy-to-power ratio)
    # Only at investment period boundaries (same variables are shared within a period)
    for bt in 1:n_bat_tech
        btech = input.battery_technologies[bt]
        min_dur = btech.min_duration_hours
        max_dur = btech.max_duration_hours

        for y_idx in 1:ypp:num_years
            for b in 1:n_buses
                if min_dur > 0
                    @constraint(model,
                        vars.bat_tech_capacity_investment[y_idx][bt][b] >= min_dur * vars.bat_tech_power_investment[y_idx][bt][b],
                        base_name = "bat_tech_min_dur_bt$(bt)_b$(b)_y$(y_idx)"
                    )
                end
                if max_dur < Inf && max_dur > 0
                    @constraint(model,
                        vars.bat_tech_capacity_investment[y_idx][bt][b] <= max_dur * vars.bat_tech_power_investment[y_idx][bt][b],
                        base_name = "bat_tech_max_dur_bt$(bt)_b$(b)_y$(y_idx)"
                    )
                end
            end
        end
    end

    # Reservoir cumulative investment limits per bus
    for g in 1:n_gen
        gen = input.generators[g]
        for b in 1:n_buses
            max_res = gen.reservoir_invest_max[b]
            if max_res > 0 && all(haskey(vars.reservoir_investment[y], g) for y in 1:ypp:num_years)
                cumulative_res = sum(vars.reservoir_investment[y][g][b] for y in 1:ypp:num_years)
                @constraint(model,
                    cumulative_res <= max_res,
                    base_name = "cumul_res_g$(g)_b$(b)"
                )
            end
        end
    end

    # Converter cumulative investment limit
    for (key, conv) in master_converters(input.network)
        conv.invest_max_power > 0 || continue
        @constraint(model,
            sum(vars.converter_investment[y][key] for y in 1:ypp:num_years) <=
                conv.invest_max_power,
            base_name = "cumul_conv_$(key[1])$(key[2])")
    end

    # Per-node aggregate cumulative investment constraint
    # When multiple buses map to the same node, the total investment across
    # all buses at that node must not exceed the per-node limit.
    node_to_buses_inv = Dict{Int, Vector{Int}}()
    for b in 1:n_buses
        push!(get!(node_to_buses_inv, b2n[b], Int[]), b)
    end
    for t in 1:n_tech
        tech = input.technologies[t]
        for (ni, buses_at_node) in node_to_buses_inv
            length(buses_at_node) <= 1 && continue
            max_inv = tech.invest_max[buses_at_node[1]]
            max_inv > 0 || continue
            @constraint(model,
                sum(vars.tech_investment[y][t][b] for y in 1:ypp:num_years, b in buses_at_node) <= max_inv,
                base_name = "cumul_node_tech_t$(t)_n$(ni)")
        end
    end
    for bt in 1:n_bat_tech
        btech = input.battery_technologies[bt]
        for (ni, buses_at_node) in node_to_buses_inv
            length(buses_at_node) <= 1 && continue
            max_pow = btech.invest_max_power[buses_at_node[1]]
            max_cap = btech.invest_max_capacity[buses_at_node[1]]
            if max_pow > 0
                @constraint(model,
                    sum(vars.bat_tech_power_investment[y][bt][b] for y in 1:ypp:num_years, b in buses_at_node) <= max_pow,
                    base_name = "cumul_node_bat_tech_pow_bt$(bt)_n$(ni)")
            end
            if max_cap > 0
                @constraint(model,
                    sum(vars.bat_tech_capacity_investment[y][bt][b] for y in 1:ypp:num_years, b in buses_at_node) <= max_cap,
                    base_name = "cumul_node_bat_tech_cap_bt$(bt)_n$(ni)")
            end
        end
    end
end

"""
    corridor_investment(inv, from, to)

Investment variable (or value) of the corridor between buses `from` and `to`,
whichever way round it is keyed: lines are stored low→high, while a variable
exists only where its first bus allows investment (e.g. zone→grid).
Returns `nothing` when the corridor has no investment option.
"""
function corridor_investment(inv::AbstractDict, from::Int, to::Int)
    haskey(inv, (from, to)) && return inv[(from, to)]
    return get(inv, (to, from), nothing)
end

"""
    corridor_cost_terms(inv, costs)

`(variable, cost per MW)` once per corridor. When both directions of a
corridor exist they are held equal (TXN-1), so only the low→high one is
charged. A corridor costs the higher of its two ends' `costs` (an end left
at 0 does not make the line cheaper), also when only one direction exists,
as for a development zone's interconnection.
"""
function corridor_cost_terms(inv::AbstractDict, costs::AbstractVector)
    terms = Tuple{Any, Float64}[]
    for ((i, j), var) in inv
        haskey(inv, (j, i)) && j < i && continue
        push!(terms, (var, max(costs[i], costs[j])))
    end
    return terms
end

"""
    add_transmission_symmetry_constraints!(model, vars, input)

Add symmetry constraints for transmission investment.
Ensures transfer_investment[i,j] == transfer_investment[j,i].
"""
function add_transmission_symmetry_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)
    num_years = length(input.years)
    n_buses = input.network.num_buses
    ypp = vars.years_per_inv_period

    # Only add at investment period starts (same variables within period)
    for y_idx in 1:ypp:num_years
        for i in 1:n_buses
            for j in (i+1):n_buses
                # Only add constraint if both directions exist
                if haskey(vars.transfer_investment[y_idx], (i, j)) &&
                   haskey(vars.transfer_investment[y_idx], (j, i))
                    @constraint(model,
                        vars.transfer_investment[y_idx][(i, j)] ==
                        vars.transfer_investment[y_idx][(j, i)],
                        base_name = "trans_sym_$(i)_$(j)_y$(y_idx)"
                    )
                end
            end
        end
    end
end

"""
    add_budget_constraints!(model, vars, input; operating=nothing)

Annual budget, at each investment-period start: the overnight cost of the
technologies, battery technologies (power and energy), transmission
corridors and reservoirs built that year (`master_investment_terms`, base
costs) stays within `max_annual_investment` plus the slack `slack_budget`.
The budget limits the capital committed in the build year, not the
annuities the objective charges. With `operating` (year index =>
expression, `budget_operating_cost`; `budget_scope: total`) the year's
operating cost counts against the same budget. The slack is priced at
`BUDGET_SLACK_PENALTY` per unit over the budget (`master_slack_cost`), so
the budget binds unless keeping it would cost more than that, and never
makes the master infeasible.
"""
function add_budget_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput;
    operating::Union{Nothing, AbstractDict} = nothing
)
    annual_cost = Dict{Int, AffExpr}(y => AffExpr(0.0)
        for y in 1:vars.years_per_inv_period:length(input.years))
    for t in master_investment_terms(vars, input)
        t.kind in BUDGET_KINDS || continue
        add_to_expression!(annual_cost[t.year], t.var, t.cost)
    end
    if operating !== nothing
        for (y_idx, cost) in annual_cost
            op = get(operating, y_idx, nothing)
            op === nothing || add_to_expression!(cost, op)
        end
    end
    for (y_idx, cost) in sort(collect(annual_cost); by = first)
        @constraint(model,
            cost <= input.max_annual_investment + vars.slack_budget[y_idx],
            base_name = "budget_y$(y_idx)"
        )
    end
end

"""
    year_day_spend(model, ps_vars, input, hours, scale, weight, tech_output,
                   tech_spend, fuel_day) -> AffExpr

A representative day's (or TSAM period's) money scaled to the year: its
`day_operating_spend` × `scale` (step length × the days it stands for) and
its fuel block's `master_fuel_day_spend` × `weight` (already per day).
"""
function year_day_spend(model::Model, ps_vars::PowerSystemVariables,
                        input::MasterProblemInput, hours::Int, scale::Float64,
                        weight::Float64, tech_output, tech_spend, fuel_day::AffExpr)
    spend = AffExpr(0.0)
    add_to_expression!(spend,
        day_operating_spend(ps_vars, input, hours; tech_output, tech_spend), scale)
    add_to_expression!(spend, master_fuel_day_spend(model, input, fuel_day), weight)
    return spend
end

"""
    record_year_operating_cost!(model, scenario_idx, y_idx, day_spends)

Keep the year's representative-day (or TSAM-period) operating money, each
day's `year_day_spend`, on the model (`model.ext[:year_operating_cost]`, by
(scenario, year)) for the `budget_scope: total` budget
(`budget_operating_cost`).
"""
function record_year_operating_cost!(model::Model, scenario_idx::Int, y_idx::Int,
                                     day_costs::AbstractVector)
    total = AffExpr(0.0)
    foreach(c -> add_to_expression!(total, c), day_costs)
    get!(() -> Dict{Tuple{Int,Int}, AffExpr}(), model.ext,
         :year_operating_cost)[(scenario_idx, y_idx)] = total
    return nothing
end

"""
    budget_operating_cost(model, vars, input; scenarios=nothing) -> Dict{Int, AffExpr}

Each year's operating money, for the `budget_scope: total` budget
(`add_budget_constraints!`): what the representative days (or TSAM periods)
spend, scaled to the year (`year_day_spend`: fuel, maintenance, start-ups,
converter and line-flow costs, the fuel block's fuel bought and transported
and electrolyzer costs), plus the year's asset costs (`build_annual_asset_cost`:
fixed O&M, life extension, decommissioning). Modelling penalties (unserved
energy, reserve, inertia and N-1 shortfalls, curtailment, RE shortfall, SOC
and spillage, fuel shortfall, slacks), battery degradation and the CO2 price
and CO2-budget violations (a carbon price is a transfer to the state) are
not in it. With stochastic `scenarios`, each scenario's money (its own
multipliers) weighted by its probability.
"""
function budget_operating_cost(model::Model, vars::MasterProblemVariables,
                               input::MasterProblemInput;
                               scenarios = nothing)
    recorded = get(model.ext, :year_operating_cost, Dict{Tuple{Int,Int}, AffExpr}())
    out = Dict{Int, AffExpr}()
    cases = scenarios === nothing ? [(0, 1.0, nothing)] :
        [(k, sc.probability, sc.multipliers) for (k, sc) in enumerate(scenarios)]
    for y_idx in eachindex(input.years), (k, prob, mult) in cases
        cost = get!(out, y_idx, AffExpr(0.0))
        add_to_expression!(cost, build_annual_asset_cost(vars, input, y_idx; mult = mult), prob)
        day = get(recorded, (k, y_idx), nothing)
        day === nothing || add_to_expression!(cost, day, prob)
    end
    return out
end

"""
    add_retirement_cascade_constraints!(model, vars, input)

Add cascade constraints ensuring life extension capacity can only decrease over time.
Once a unit begins retirement, it cannot reverse the decision in later years.
"""
function add_retirement_cascade_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)
    num_years = length(input.years)
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_buses = input.network.num_buses

    for y_idx in 2:num_years
        # Generator life extension cascade: can only decrease over time
        for g in 1:n_gen
            for n in 1:n_buses
                curr = vars.gen_life_extension[y_idx][g][n]
                prev = vars.gen_life_extension[y_idx - 1][g][n]
                if curr !== nothing && prev !== nothing
                    @constraint(model, curr <= prev,
                        base_name = "cascade_gen_g$(g)_n$(n)_y$(y_idx)")
                end
            end
        end

        # Battery life extension cascade
        for bi in 1:n_bat
            for n in 1:n_buses
                curr = vars.bat_life_extension[y_idx][bi][n]
                prev = vars.bat_life_extension[y_idx - 1][bi][n]
                if curr !== nothing && prev !== nothing
                    @constraint(model, curr <= prev,
                        base_name = "cascade_bat_b$(bi)_n$(n)_y$(y_idx)")
                end
            end
        end
    end
end

"""
    master_systems(input) -> Vector{SystemNodeRange}

The systems an RE target holds for: `input.system_node_ranges` (one per
system of a merged run), or one system over all buses.
"""
master_systems(input::MasterProblemInput) = isempty(input.system_node_ranges) ?
    [SystemNodeRange("global", 1, input.network.num_buses, input.initial_re_penetration)] :
    input.system_node_ranges

"""
    system_day_demand(input, year_idx, start_hour, hours) -> Vector{Float64}

Demand of each system (`master_systems`) over a representative day or TSAM
period starting at master step `start_hour`: its buses' shares
(`demand_fraction`) of their node's demand, with the year's demand growth,
summed over the steps (MW × steps; the caller scales it to the year).
"""
function system_day_demand(input::MasterProblemInput, year_idx::Int,
                           start_hour::Int, hours::Int)
    D = input.base_demand
    rows = size(D, 1)
    growth = (1.0 + input.demand_growth)^(year_idx - 1)
    systems = master_systems(input)
    out = zeros(length(systems))
    for (s, sr) in enumerate(systems), b in sr.first_bus:(sr.first_bus + sr.num_buses - 1)
        node = input.network.bus_to_node[b]
        frac = input.network.buses[b].demand_fraction
        for t in 1:hours
            out[s] += D[rows > 0 ? mod1(start_hour + t - 1, rows) : 1, node] * frac
        end
    end
    return out .* growth
end

"""
    system_day_output(ps_vars, tech_output, input, hours, keep) -> Vector{AffExpr}

Output of each system (`master_systems`) over a representative day or TSAM
period, summed over its steps: its existing generators and candidate
technologies `u` with `keep(u)`, at the system's buses. Battery discharge is
not generation.

    system_day_renewable(…) = system_day_output(…, is_renewable_unit)
    system_day_generation(…) = system_day_output(…, all units)

are the numerator and denominator of the RE share (`is_renewable_unit`), as
the dispatch has them.
"""
function system_day_output(ps_vars::PowerSystemVariables, tech_output,
                           input::MasterProblemInput, hours::Int, keep)
    systems = master_systems(input)
    out = [AffExpr(0.0) for _ in systems]
    for (s, sr) in enumerate(systems)
        buses = sr.first_bus:(sr.first_bus + sr.num_buses - 1)
        for (g, gen) in enumerate(input.generators)
            keep(gen) || continue
            for b in ps_vars.buses_of_gen[g]
                b in buses || continue
                for t in 1:hours
                    add_to_expression!(out[s], ps_vars.gen_output[g, b, t])
                end
            end
        end
        tech_output === nothing && continue
        for (tc, tech) in enumerate(input.technologies)
            keep(tech) || continue
            for b in buses, t in 1:hours
                add_to_expression!(out[s], tech_output[tc, b, t])
            end
        end
    end
    return out
end

system_day_renewable(ps_vars::PowerSystemVariables, tech_output,
                     input::MasterProblemInput, hours::Int) =
    system_day_output(ps_vars, tech_output, input, hours, is_renewable_unit)

system_day_generation(ps_vars::PowerSystemVariables, tech_output,
                      input::MasterProblemInput, hours::Int) =
    system_day_output(ps_vars, tech_output, input, hours, _ -> true)

"""
    master_bus_systems(input) -> Vector{Int}

System (`master_systems`) of each bus; 0 for a bus outside every system.
"""
function master_bus_systems(input::MasterProblemInput)
    out = zeros(Int, input.network.num_buses)
    for (s, sr) in enumerate(master_systems(input))
        out[sr.first_bus:(sr.first_bus + sr.num_buses - 1)] .= s
    end
    return out
end

"""
    system_day_curtailment(ps_vars, tech_output, tech_cap, input, start_hour, hours)
        -> Vector{AffExpr}

Curtailment of each system (`master_systems`) over a representative day or
TSAM period starting at master step `start_hour`, summed over its steps
(MW × steps), as the curtailment limit counts it: that of the existing
variable renewables at its nodes (`add_curtailment_constraints!`) and of the
candidate variable renewable technologies at its buses, their capacity
`tech_cap(tc, b)` (a number or an expression) × availability less their
output.
"""
function system_day_curtailment(ps_vars::PowerSystemVariables, tech_output, tech_cap,
                                input::MasterProblemInput, start_hour::Int, hours::Int)
    sys = master_bus_systems(input)
    out = [AffExpr(0.0) for _ in master_systems(input)]
    node_sys = zeros(Int, size(ps_vars.curtailment, 1))
    for (b, n) in enumerate(input.network.bus_to_node)
        n <= length(node_sys) && sys[b] > 0 && (node_sys[n] = sys[b])
    end
    for n in eachindex(node_sys)
        node_sys[n] > 0 || continue
        for t in 1:hours
            add_to_expression!(out[node_sys[n]], ps_vars.curtailment[n, t])
        end
    end
    tech_output === nothing && return out
    for (tc, tech) in enumerate(input.technologies)
        is_variable_renewable(tech, 1) || continue
        for b in 1:input.network.num_buses
            sys[b] > 0 || continue
            cap = tech_cap(tc, b)
            for t in 1:hours
                add_to_expression!(out[sys[b]],
                    tech_availability_at(tech, start_hour + t - 1, b), cap)
                add_to_expression!(out[sys[b]], -1.0, tech_output[tc, b, t])
            end
        end
    end
    return out
end

"""
    CurtailmentYear()

A year's representative days or TSAM periods for its curtailment limit
(`add_curtailment_limit!`): per period (`add_period_curtailment!`) the
curtailment (`system_day_curtailment`), renewable generation
(`system_day_renewable`) and the curtailment the RE growth cap forces
(`re_cap_exempt_energy`) of each system (MW × steps), and the period's
`scale` (the year its steps stand for: `rep_day_scaling`, or step length ×
TSAM weight).
"""
struct CurtailmentYear
    periods::Vector{Tuple{Vector{AffExpr}, Vector{AffExpr}, Vector{Float64}, Float64}}
end
CurtailmentYear() =
    CurtailmentYear(Tuple{Vector{AffExpr}, Vector{AffExpr}, Vector{Float64}, Float64}[])

add_period_curtailment!(year::CurtailmentYear, curt::Vector{AffExpr}, re::Vector{AffExpr},
                        exempt::Vector{Float64}, scale::Float64) =
    push!(year.periods, (curt, re, exempt, scale))

"""
    curtailment_year_sums(year) -> (curt, re, exempt, smax)

Each system's curtailment, renewable generation and exemption over the
periods of `year`, each period × its scale over the largest one `smax`:
the year's (MWh/year) / `smax`. Representative days all stand for as many
days, so for them it is the plain sum over the days.
"""
function curtailment_year_sums(year::CurtailmentYear)
    isempty(year.periods) && return (AffExpr[], AffExpr[], Float64[], 1.0)
    smax = maximum(p[4] for p in year.periods)
    n = length(year.periods[1][1])
    curt = [AffExpr(0.0) for _ in 1:n]
    re = [AffExpr(0.0) for _ in 1:n]
    exempt = zeros(n)
    for (c, r, e, w) in year.periods, s in 1:n
        add_to_expression!(curt[s], c[s], w / smax)
        add_to_expression!(re[s], r[s], w / smax)
        exempt[s] += e[s] * w / smax
    end
    return curt, re, exempt, smax
end

"""
Floor of the price (M\$ per MWh/year, i.e. 10⁴ \$/MWh) of curtailment above
a master's annual curtailment limit (`curtailment_excess_penalty`).
"""
const CURTAILMENT_EXCESS_PENALTY = 1.0e-2

"""
How many times the dearest per-MWh price the curtailment excess must beat
(`curtailment_excess_penalty`): 100, the floor's margin over the default RE
shortfall price.
"""
const CURTAILMENT_EXCESS_FACTOR = 100.0

"""
    curtailment_excess_penalty(input) -> Float64

Price (M\$ per MWh/year) of curtailment above a master's annual curtailment
limit (`add_curtailment_limit!`): `CURTAILMENT_EXCESS_PENALTY` (10⁴ \$/MWh),
and at least `CURTAILMENT_EXCESS_FACTOR` × the RE shortfall price
(`re_shortfall_penalty`) and the curtailment cost, with a stochastic
scenario's multipliers. A fixed price was beaten by a shortfall price above
it: the master then curtailed above the limit to meet its RE target. Well
above them and what avoiding a MWh of curtailment usually costs (the
limit's dual), the master pays it only where nothing else can hold the
limit.
"""
curtailment_excess_penalty(input::MasterProblemInput) =
    max(CURTAILMENT_EXCESS_PENALTY,
        CURTAILMENT_EXCESS_FACTOR * max(re_shortfall_penalty(input), input.curtailment_cost))

"""
    add_curtailment_limit!(model, input, year, y_idx; scenario_idx=0) -> AffExpr

The curtailment limit (CURT-1) of year `y_idx`, per system: its curtailment
of existing and candidate variable renewables over the year is at most
`max_curtailment_ratio` × its renewable generation over the year, plus what
the RE growth cap forces it to curtail (RE-3c, `re_cap_exempt_energy`), plus
an excess `Z_σ` (MWh/year) priced at `curtailment_excess_penalty`:

    Σ_p w_p C_σ,p ≤ κ × Σ_p w_p E_σ,p + Σ_p w_p X_σ,p + Z_σ  (max_curtailment_s{σ}_y{y})

over the year's representative days or TSAM periods `p` with weights `w_p`
(`year`, `curtailment_year_sums`: divided by the largest weight). Single
days may curtail more. The excess keeps a master feasible when existing
renewables must curtail more than the limit allows (a repaired loss model
whose lines can no longer discard energy, say); `optimize_master!` warns
about any (`warn_curtailment_excess`). Only a system with renewable
generation to measure it on has one, and none when
`max_curtailment_ratio` ≥ 1. Returns the excess's cost, an operating cost
of the year.
"""
function add_curtailment_limit!(model::Model, input::MasterProblemInput,
                                year::CurtailmentYear, y_idx::Int; scenario_idx::Int = 0)
    cost = AffExpr(0.0)
    input.max_curtailment_ratio < 1.0 || return cost
    curt, re, exempt, smax = curtailment_year_sums(year)
    tag = scenario_idx > 0 ? "_sc$(scenario_idx)" : ""
    record = get!(() -> CurtailmentExcess[], model.ext, :curtailment_excess)
    systems = master_systems(input)
    price = curtailment_excess_penalty(input)
    for s in eachindex(curt)
        isempty(re[s].terms) && continue
        excess = @variable(model, lower_bound = 0,
            base_name = "curtailment_excess$(tag)_s$(s)_y$(y_idx)")
        @constraint(model,
            curt[s] <= input.max_curtailment_ratio * re[s] + exempt[s] + excess / smax,
            base_name = "max_curtailment$(tag)_s$(s)_y$(y_idx)")
        add_to_expression!(cost, price, excess)
        push!(record, CurtailmentExcess(scenario_idx, systems[s].name, input.years[y_idx],
                                        input.max_curtailment_ratio, excess, re[s], smax, price))
    end
    return cost
end

# A system-year's curtailment above its limit (`add_curtailment_limit!`):
# scenario (0 outside a stochastic master), system name, year, the limit's
# ratio, the excess (MWh/year), the renewable generation (MWh/year / smax)
# and the excess's price (M$/MWh, curtailment_excess_penalty)
struct CurtailmentExcess
    scenario::Int
    system::String
    year::Int
    ratio::Float64
    excess::VariableRef
    re::AffExpr
    smax::Float64
    price::Float64
end

"""
    curtailment_excess(model) -> Vector{NamedTuple}

Curtailment above the annual limit in a solved master
(`add_curtailment_limit!`): per scenario (0 outside a stochastic master),
system and year whose excess passes the limit's tolerance
(`curtailment_tolerance` of its renewable generation), the system's name,
the year, the limit's ratio, the excess and the renewable generation
(MWh/year), and the excess's price (M\$/MWh, `curtailment_excess_penalty`).
"""
function curtailment_excess(model::Model)
    out = NamedTuple{(:scenario, :system, :year, :ratio, :excess_mwh, :re_mwh, :price),
                     Tuple{Int, String, Int, Float64, Float64, Float64, Float64}}[]
    has_values(model) || return out
    for e in get(model.ext, :curtailment_excess, CurtailmentExcess[])
        z = value(e.excess)
        re_mwh = value(e.re) * e.smax
        z > curtailment_tolerance(re_mwh) || continue
        push!(out, (scenario = e.scenario, system = e.system, year = e.year,
                    ratio = e.ratio, excess_mwh = z, re_mwh = re_mwh, price = e.price))
    end
    return out
end

"""
    curtailment_excess_mwh(model) -> Float64

Total curtailment (MWh/year, over systems, years and scenarios) a solved
master puts above its annual limits (`curtailment_excess`); 0 within them.
"""
curtailment_excess_mwh(model::Model) =
    sum(e.excess_mwh for e in curtailment_excess(model); init = 0.0)

"""
    excess_grew(before, after) -> Bool

Whether a re-solve curtails more above the annual limits (`after`, MWh/year)
than the plan before it (`before`), beyond the solver's noise.
"""
excess_grew(before::Real, after::Real) = after > before * (1 + 1e-6) + 1e-3

"""
    warn_curtailment_excess(model)

A warning for each system and year of a solved master that curtails more
than its annual limit allows (`curtailment_excess`), with the excess and
the renewable generation the limit is measured on.
"""
function warn_curtailment_excess(model::Model)
    for e in curtailment_excess(model)
        sc = e.scenario > 0 ? ", scenario $(e.scenario)" : ""
        @warn "Master problem: system $(e.system), year $(e.year)$(sc) curtails " *
              "$(round(e.excess_mwh / 1e3; sigdigits = 4)) GWh/yr above its annual " *
              "curtailment limit (max_curtailment_ratio = $(e.ratio) of " *
              "$(round(e.re_mwh / 1e3; sigdigits = 4)) GWh/yr of renewable " *
              "generation), at $(round(Int, e.price * 1e6)) \$/MWh: " *
              "the plan could not keep within it"
    end
    return
end

"""
    hold_curtailment_excess!(model)

Bound each curtailment excess of a solved master (`add_curtailment_limit!`)
by its value plus the limit's tolerance (`curtailment_tolerance`). MGA and
SPORES explore their alternatives under this: they curtail above a limit
only as far as the cost-optimal plan has to, as under a hard limit, rather
than buy curtailment above it with the cost slack.
"""
function hold_curtailment_excess!(model::Model)
    for e in get(model.ext, :curtailment_excess, CurtailmentExcess[])
        set_upper_bound(e.excess, max(value(e.excess), 0.0) +
                                  curtailment_tolerance(value(e.re) * e.smax))
    end
    return
end

"""
    re_shortfall_penalty(input) -> Float64

Price of a MWh of renewable energy missing from an RE target: the
configured `fre_penetration_loss_penalty` (`penalties.fre_penetration_loss`,
a stochastic scenario's × `penalty_fre_penetration`), or the dispatch's
default of 100 \$/MWh (1e-4 in the master's M\$) when it is not positive.
"""
re_shortfall_penalty(input::MasterProblemInput) =
    input.fre_penetration_loss_penalty > 0 ? input.fre_penetration_loss_penalty : 1e-4

"""
    re_cap_penalty(input) -> Float64

Price of a MWh of renewable energy a system uses above its growth cap
(`add_re_growth_cap!`): the value of lost load (`loss_of_load_penalty`), and
at least the RE shortfall price (`re_shortfall_penalty`). Renewable energy
above the cap costs as much as load shed, so the master curtails it
instead (existing renewables beyond the curtailment limit,
`re_cap_exemption`); the slack only keeps the master feasible where
renewable output cannot be curtailed (a reservoir's minimum release, say).
"""
re_cap_penalty(input::MasterProblemInput) =
    max(input.loss_of_load_penalty, re_shortfall_penalty(input))

"""
    re_share_anchor(input, s) -> Float64

Actual RE share of system `s` in the year before the first modelled year,
from which year 1's growth cap is measured: `input.re_share_anchor[s]` when
given (a myopic year: the share the previous year achieved), otherwise the
system's initial share; clamped to [0, 1].
"""
function re_share_anchor(input::MasterProblemInput, s::Int)
    a = s <= length(input.re_share_anchor) ? input.re_share_anchor[s] :
        master_systems(input)[s].initial_re
    return clamp(Float64(a), 0.0, 1.0)
end

"""
    re_increment_floor(initial_re, min_inc, y_idx) -> Float64

Least RE share the minimum increment asks of year `y_idx` (RE-3):
`initial_re + y_idx × min_inc`, at most 1 (the share cannot pass 100 %),
with `initial_re` clamped to [0, 1]. Year 0 is the anchor itself.
"""
re_increment_floor(initial_re::Real, min_inc::Real, y_idx::Int) =
    min(clamp(Float64(initial_re), 0.0, 1.0) + y_idx * max(Float64(min_inc), 0.0), 1.0)

"""
    re_required_share(input, targets, s, y_idx) -> Float64

Least RE share system `s` must reach in year `y_idx` before its shortfall is
priced (`add_re_target_constraints!`): its RE-1 target `targets[(s, y_idx)]`
and, with a minimum increment, the floor `re_increment_floor` from the
system's initial share (RE-3), whichever is higher.
"""
function re_required_share(input::MasterProblemInput,
                           targets::Dict{Tuple{Int,Int}, Float64}, s::Int, y_idx::Int)
    required = targets[(s, y_idx)]
    input.min_re_increment > 0 || return required
    return max(required, re_increment_floor(master_systems(input)[s].initial_re,
                                            input.min_re_increment, y_idx))
end

"""
    existing_vre_available(input, y_idx, start_hour, hours) -> Vector{Float64}

Energy (MW × steps) the existing variable renewables (`is_variable_renewable`)
of each system (`master_systems`) can produce over a representative day or
TSAM period starting at master step `start_hour`: the power they have in
service in year `y_idx` within their lifetime (rated × degradation, as the
day model) × availability × scheduled outages. Life-extended units and candidate technologies
are investment decisions and do not count.
"""
function existing_vre_available(input::MasterProblemInput, y_idx::Int,
                                start_hour::Int, hours::Int)
    systems = master_systems(input)
    out = zeros(length(systems))
    for (s, sr) in enumerate(systems), (g, gen) in enumerate(input.generators)
        gen.type == "Renewable" || continue
        rows = size(gen.availability, 1)
        for b in sr.first_bus:(sr.first_bus + sr.num_buses - 1)
            b <= length(gen.rated_power) && gen.rated_power[b] > 0 || continue
            is_variable_renewable(gen, b) || continue
            age = gen.initial_age[b] + (y_idx - 1)
            age < gen.life_time[b] || continue
            power = gen.rated_power[b] * (1.0 - gen.degradation_rate[b])^age
            node = input.network.bus_to_node[b]
            for t in 1:hours
                h = start_hour + t - 1
                avail = rows == 0 ? 1.0 : gen.availability[mod1(h, rows), b]
                out[s] += power * avail *
                          master_outage_factor(input.gen_outage_factors, g, node, h)
            end
        end
    end
    return out
end

"""
    re_cap_exemption(input, targets, y_idx, periods, prev_floor) -> (θ, floor)

Curtailment the RE growth cap forces on existing renewables (round 24).
When a system's existing variable renewables, curtailed at most
`max_curtailment_ratio`, would give it a higher RE share than its growth cap
allows, the excess is curtailed: it does not count toward the share and pays
`curtailment_cost` like any curtailment, but not the curtailment limit
(`re_cap_exempt_energy`). Without this the excess could only go to the
cap's slack at the value of lost load (`re_cap_penalty`). Building
renewables above the cap still does not pay: only the existing units'
energy is exempt.

`periods` are the year's representative days (or TSAM periods) as
`(start_hour, hours, scale)`, `scale` taking a step to the year. With `A`
the existing variable renewables' available energy and `D` the demand of
system `s` over them, both scaled to the year (`D` stands for the total
generation the share is measured on, which it is but for losses and load
shed), and `k` the cap's estimate (the previous year's share plus
`max_re_increment`, at most 1), the system may curtail beyond the limit the
share

    θ[s] = max(0, 1 − (1 + max_curtailment_ratio) × k × D / A)

of each period's available energy, so that the existing units can be held
at the cap. The previous year's share is `re_share_anchor` in year 1 (a
myopic year: the share last year achieved); later, the higher of the
year's required share (`re_required_share`) and `prev_floor[s]`, the least
share its existing renewables left it, `min(A / ((1 + ratio) × D), k)`,
returned as `floor` for the next year. θ is 0 without a growth cap
(`max_re_increment` ≥ 1) or curtailment limit (`max_curtailment_ratio` ≥ 1).
"""
function re_cap_exemption(input::MasterProblemInput,
                          targets::Dict{Tuple{Int,Int}, Float64}, y_idx::Int,
                          periods, prev_floor)
    n_sys = length(master_systems(input))
    avail = zeros(n_sys)
    demand = zeros(n_sys)
    for (start_hour, hours, scale) in periods
        hours >= 1 || continue
        avail .+= scale .* existing_vre_available(input, y_idx, start_hour, hours)
        demand .+= scale .* system_day_demand(input, y_idx, start_hour, hours)
    end
    ratio = input.max_curtailment_ratio
    active = input.max_re_increment < 1.0 && ratio < 1.0
    θ = zeros(n_sys)
    floor = zeros(n_sys)
    for s in 1:n_sys
        prev = prev_floor === nothing || y_idx == 1 ? re_share_anchor(input, s) :
            max(re_required_share(input, targets, s, y_idx - 1), prev_floor[s])
        k = min(prev + max(input.max_re_increment, 0.0), 1.0)
        demand[s] > 0 || continue
        floor[s] = min(avail[s] / ((1.0 + max(ratio, 0.0)) * demand[s]), k)
        if active && avail[s] > 0
            θ[s] = max(0.0, 1.0 - (1.0 + ratio) * k * demand[s] / avail[s])
        end
    end
    return θ, floor
end

"""
    re_cap_exempt_energy(input, θ, y_idx, start_hour, hours) -> Vector{Float64}

The curtailment the RE growth cap forces on a representative day or TSAM
period (`re_cap_exemption`), per system: `θ[s]` × the energy its existing
variable renewables can produce over the period (`existing_vre_available`,
MW × steps). The year's curtailment limit allows it on top of
`max_curtailment_ratio` (`add_curtailment_limit!`); the curtailment itself
pays `curtailment_cost` as any.
"""
function re_cap_exempt_energy(input::MasterProblemInput, θ::Vector{Float64},
                              y_idx::Int, start_hour::Int, hours::Int)
    n_sys = length(master_systems(input))
    any(>(0.0), θ) || return zeros(n_sys)
    avail = existing_vre_available(input, y_idx, start_hour, hours)
    return [s <= length(θ) ? max(θ[s], 0.0) * avail[s] : 0.0 for s in 1:n_sys]
end

"""
    re_cap_prev_allowance(prev, generation, demand) -> AffExpr

Renewable generation (MWh/year) the previous year's RE share allows a
system this year before the growth cap's increment (`add_re_growth_cap!`),
with `generation` and `demand` this year's (MWh/year):

- `prev` a number, the share itself (year 1: `re_share_anchor`; a myopic
  year: the share last year achieved): `prev × generation`, exact;
- `prev = (E, D_prev)`, last year's renewable generation `E` (an expression,
  or `D_prev ×` a copy of `re_energy_ratio`) and demand: `E × demand /
  D_prev`, last year's share × this year's generation with generation taken
  to grow like demand. Both generations are decided by the model; this
  keeps the cap linear and is exact when the ratio of generation to demand
  (losses, load shed) is the same in both years.
"""
re_cap_prev_allowance(prev::Real, generation, demand::Float64) =
    Float64(prev) * generation
re_cap_prev_allowance(prev::Tuple, generation, demand::Float64) =
    (demand / prev[2]) * prev[1]

"""
    add_re_growth_cap!(model, input, s, y_idx, energy, generation, allowance;
                       scenario_idx=0) -> AffExpr

Maximum annual increment (RE-3) of system `s`'s ACTUAL RE share (renewable
over total generation, `is_renewable_unit`) in year `y_idx`. With `energy`
the system's renewable generation (MWh/year, after curtailment),
`generation` its total generation (MWh/year) and `allowance` the renewable
generation the share of the year before allows (`re_cap_prev_allowance`):

    energy ≤ allowance + Δmax × generation + over        (re_max_increment)

`over` (MWh/year, `slack_re_cap…`) is priced at `re_cap_penalty`; the
returned cost goes with the year's operating cost. Existing renewables
above the cap are curtailed instead, beyond the curtailment limit
(`re_cap_exemption`), so the slack is a last resort. Nothing when
`max_re_increment` ≥ 1.
"""
function add_re_growth_cap!(model::Model, input::MasterProblemInput, s::Int, y_idx::Int,
                            energy, generation, allowance; scenario_idx::Int = 0)
    cost = AffExpr(0.0)
    input.max_re_increment < 1.0 || return cost
    tag = scenario_idx > 0 ? "_sc$(scenario_idx)" : ""
    over = @variable(model, lower_bound = 0, base_name = "slack_re_cap$(tag)_s$(s)_y$(y_idx)")
    @constraint(model, energy - allowance <= input.max_re_increment * generation + over,
        base_name = "re_max_increment$(tag)_s$(s)_y$(y_idx)")
    get!(() -> Dict{Tuple{Int,Int,Int}, VariableRef}(), model.ext,
         :re_cap_slack)[(scenario_idx, s, y_idx)] = over
    add_to_expression!(cost, re_cap_penalty(input), over)
    return cost
end

"""
    add_re_target_constraints!(model, vars, input, y_idx, targets, re_energy,
                               re_generation, re_demand; scenario_idx=0) -> AffExpr

Annual RE bounds of each system in year `y_idx`. With `re_energy[σ]` the
system's renewable generation, `re_generation[σ]` its total generation
(`system_day_generation`) and `re_demand[σ]` its demand over the
representative days (or TSAM periods), each scaled to the year as its cost
is (MWh/year), and `slack_re_target[(scenario_idx, σ, y_idx)]` the renewable
energy missing (MWh/year):

    re_energy[σ] + slack ≥ re_required_share(σ, y) × re_generation[σ]   (re_target)

the RE-1 target or the RE-3 minimum-increment floor, whichever is higher,
on the RE share (renewable over total generation: at 100 % no fossil unit
runs), and the growth cap on the actual share (`add_re_growth_cap!`),
measured from the same scenario's year before (year 1: `re_share_anchor`;
`re_cap_prev_allowance`). The target is annual: a sunny day may make up for
a cloudy one, as over a dispatch window. Returns the cost of the slacks
(`re_shortfall_penalty` and `re_cap_penalty` per MWh), which the caller adds
to the year's operating cost (discounted and, in a stochastic master,
weighted by the scenario's probability like it). The renewable and total
generation and the demand are kept in `model.ext[:master_re]` for the actual
share (`define_re_share!`, `master_re_shares`). A system without demand
gets no constraint.
"""
function add_re_target_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    y_idx::Int,
    targets::Dict{Tuple{Int,Int}, Float64},
    re_energy::Vector{AffExpr},
    re_generation::Vector{AffExpr},
    re_demand::Vector{Float64};
    scenario_idx::Int = 0
)::AffExpr
    cost = AffExpr(0.0)
    tag = scenario_idx > 0 ? "_sc$(scenario_idx)" : ""
    record = get!(() -> Dict{Tuple{Int,Int,Int}, Tuple{AffExpr, AffExpr, Float64}}(),
                  model.ext, :master_re)
    for s in eachindex(master_systems(input))
        demand = re_demand[s]
        demand > 0 || continue
        slack = @variable(model, lower_bound = 0,
            base_name = "slack_re$(tag)_s$(s)_y$(y_idx)")
        vars.slack_re_target[(scenario_idx, s, y_idx)] = slack
        @constraint(model,
            re_energy[s] + slack >=
                re_required_share(input, targets, s, y_idx) * re_generation[s],
            base_name = "re_target$(tag)_s$(s)_y$(y_idx)")
        add_to_expression!(cost, re_shortfall_penalty(input), slack)
        prev = get(record, (scenario_idx, s, y_idx - 1), nothing)
        allowance = re_cap_prev_allowance(
            prev === nothing ? re_share_anchor(input, s) : (prev[1], prev[3]),
            re_generation[s], demand)
        add_to_expression!(cost, add_re_growth_cap!(model, input, s, y_idx, re_energy[s],
                                                    re_generation[s], allowance; scenario_idx))
        record[(scenario_idx, s, y_idx)] = (re_energy[s], re_generation[s], demand)
    end
    return cost
end

"""
    define_re_share!(model, vars, input)

Define `re_energy_ratio[σ, y]` for each system and year from the
representative days / TSAM periods (`model.ext[:master_re]`): its renewable
generation over its demand, both scaled to the year; in a stochastic master
the probability-weighted generation over the weighted demand. It carries a
year's renewable generation to the next year's growth cap
(`re_cap_prev_allowance`, the Benders copies); the RE share itself is
renewable over total generation (`master_re_shares`). A system-year without
demand gets 0. Nothing for a master without representative days.

The ratio is an expression of the day variables, not a variable with a
defining row: `demand × ratio == energy` put the year's demand (MWh, ~10⁷
for a national system) in the matrix next to the day weights, the widest
coefficient range of the master (round 36). The placeholder variables
`build_master_variables!` made are deleted.
"""
function define_re_share!(model::Model, vars::MasterProblemVariables,
                          input::MasterProblemInput)
    record = get(model.ext, :master_re, nothing)
    (record === nothing || isempty(record)) && return nothing
    scenarios = get(model.ext, :master_scenarios, nothing)
    energy = Dict{Tuple{Int,Int}, AffExpr}()
    demand = Dict{Tuple{Int,Int}, Float64}()
    for ((k, s, y), (e, _, d)) in record
        p = k == 0 || scenarios === nothing ? 1.0 : scenarios[k].probability
        add_to_expression!(get!(() -> AffExpr(0.0), energy, (s, y)), e, p)
        demand[(s, y)] = get(demand, (s, y), 0.0) + p * d
    end
    placeholders = VariableRef[]
    for key in collect(keys(vars.re_energy_ratio))
        v = vars.re_energy_ratio[key]
        v isa VariableRef && push!(placeholders, v)
        d = get(demand, key, 0.0)
        vars.re_energy_ratio[key] = d > 0 ? energy[key] / d : AffExpr(0.0)
    end
    isempty(placeholders) || delete(model, placeholders)
    return nothing
end

"""
    adequacy_peak_steps(input, y_idx, ni) -> Vector{Int}

Master steps (rows of `base_demand`) of year `y_idx` in the top 10 % of
node `ni`'s demand (at or above its 90th percentile): the peak period in
which the firm capacity of a dispatchable unit is its mean availability.
"""
function adequacy_peak_steps(input::MasterProblemInput, y_idx::Int, ni::Int)
    D = input.base_demand
    tpy = input.hours_per_year[y_idx] ÷ input.temporal_resolution_hours
    y0, y1 = (y_idx - 1) * tpy + 1, min(y_idx * tpy, size(D, 1))
    rows = y1 >= y0 ? (y0:y1) : (1:size(D, 1))
    isempty(rows) && return Int[]
    d = size(D, 2) >= ni ? D[rows, ni] : vec(maximum(D[rows, :], dims = 2))
    threshold = sort(d)[max(1, ceil(Int, 0.9 * length(d)))]
    return collect(rows)[d .>= threshold]
end

"""
    firm_capacity_credit(availability, b, peak_steps; variable) -> Float64

Share of a unit's capacity at bus `b` that counts as firm in the capacity
adequacy constraint: for a variable renewable (`variable`, see
`is_variable_renewable`), the minimum of its availability profile (its
capacity credit: 0 for solar); for any other unit (thermal, biomass,
reservoir hydro, storage), its mean availability over the node's peak
period `peak_steps` (`adequacy_peak_steps`), so derates, outages and
seasons the profile holds count, × its scheduled-outage factor `outage(t)`
at each peak step (`master_outage_factor`). 1.0 without a profile or
outage.
"""
function firm_capacity_credit(availability::AbstractMatrix, b::Int, peak_steps;
                              variable::Bool, outage = t -> 1.0)
    (size(availability, 1) == 0 || b > size(availability, 2)) &&
        return isempty(peak_steps) || variable ? 1.0 :
               sum(outage(t) for t in peak_steps) / length(peak_steps)
    variable && return minimum(@view availability[:, b])
    isempty(peak_steps) && return 1.0
    n = size(availability, 1)
    return sum(availability[mod1(t, n), b] * outage(t) for t in peak_steps) / length(peak_steps)
end

"""
    usable_energy_ratio(bat, b) -> Float64

Energy a battery (or battery technology) delivers at bus `b` from a full
charge, per MWh of its energy capacity: (soc_max − soc_min) × discharge
efficiency.
"""
usable_energy_ratio(bat::Union{BatteryConfig, BatteryTechnologyConfig}, b::Int)::Float64 =
    max(bat.soc_max[b] - bat.soc_min[b], 0.0) * bat.discharge_efficiency[b]

"""
    add_capacity_adequacy_constraints!(model, vars, input)

Add capacity adequacy constraints ensuring total capacity meets peak demand.

This is a simplified planning constraint that requires, per node:
  local_capacity + imports - exports >= peak_demand * reserve_margin

Capacity counts at its firm share (`firm_capacity_credit`) × risk
coefficient: variable renewables at their capacity credit, every other
generator, technology and storage unit at its mean availability over the
node's peak period. A battery (existing, life-extended or built from a
battery technology) counts at most the power its usable energy sustains
over the critical period, min(power, usable energy / H) with H =
`master_storage_firm_hours` and usable energy = energy × (soc_max − soc_min)
× discharge efficiency (`usable_energy_ratio`): a short battery cannot carry
a long low-renewable night at full power.

Imports between two nodes are limited to the transfer capacity between
their buses (existing lines plus cumulative transfer investment), so a node
may rely on a neighbour's surplus instead of building locally. Nodal peaks
are taken as coincident (conservative). With a slack variable to allow
infeasibility at a penalty cost.
"""
function add_capacity_adequacy_constraints!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)
    num_years = length(input.years)
    n_buses = input.network.num_buses
    n_gen = length(input.generators)
    n_bat = length(input.batteries)

    # Reserve margin (require extra capacity above peak, configurable)
    reserve_margin = input.reserve_margin
    # Hours a battery must sustain its output to count as firm
    firm_h = n_bat + length(input.battery_technologies) > 0 ?
        master_storage_firm_hours(input) : DEFAULT_STORAGE_FIRM_HOURS

    n_nodes = input.network.num_nodes
    b2n = input.network.bus_to_node
    # Node pairs joined by existing lines or by a transfer-investment option
    node_pairs = Set{Tuple{Int,Int}}()
    for i in 1:n_buses, j in 1:n_buses
        m, n = b2n[i], b2n[j]
        m == n && continue
        if input.network.connections[i, j] > 0 ||
           any(haskey(inv, (i, j)) for inv in values(vars.transfer_investment))
            push!(node_pairs, (m, n))
        end
    end

    # Calculate peak demand for each year at each node
    for y_idx in 1:num_years
        node_capacity = Dict{Int, AffExpr}()
        node_peak = Dict{Int, Float64}()
        # Get year-specific demand slice (with demand growth)
        growth_factor = (1.0 + input.demand_growth)^(y_idx - 1)
        timesteps_per_year_y = input.hours_per_year[y_idx] ÷ input.temporal_resolution_hours
        year_start = (y_idx - 1) * timesteps_per_year_y + 1
        year_end = min(y_idx * timesteps_per_year_y, size(input.base_demand, 1))

        # Capacity adequacy per NODE (aggregate all buses on same node)
        # Generators on different buses of the same node can serve demand
        # on any bus via internal transmission lines
        for ni in 1:input.network.num_nodes
            # Peak demand for this node
            if size(input.base_demand, 2) >= ni && year_end >= year_start
                peak_demand = maximum(input.base_demand[year_start:year_end, ni]) * growth_factor * reserve_margin
            elseif size(input.base_demand, 2) >= ni
                peak_demand = maximum(input.base_demand[:, ni]) * growth_factor * reserve_margin
            else
                peak_demand = maximum(input.base_demand) * growth_factor * reserve_margin
            end

            # Build cumulative capacity expression across ALL buses on this node
            total_capacity = AffExpr(0.0)
            peak = adequacy_peak_steps(input, y_idx, ni)

            for n in 1:n_buses
                # Skip buses not on this node
                input.network.bus_to_node[n] != ni && continue

                # Generator capacity: unified retirement with degradation,
                # at its firm share (firm_capacity_credit)
                for g in 1:n_gen
                    gen = input.generators[g]
                    rated_power = gen.rated_power[n]
                    lifetime = gen.life_time[n]
                    initial_age = gen.initial_age[n]
                    age_at_year = initial_age + y_idx - 1
                    degradation_rate = gen.degradation_rate[n]

                    capacity_credit = firm_capacity_credit(gen.availability, n, peak;
                        variable = is_variable_renewable(gen, n),
                        outage = t -> master_outage_factor(input.gen_outage_factors, g, ni, t))

                    # Risk coefficient: geographic hazard derating (default 1.0)
                    risk_coef = n <= length(gen.risk_coefficient) ? gen.risk_coefficient[n] : 1.0

                    # Existing: active if age < lifetime, with degradation
                    if age_at_year < lifetime && rated_power > 0
                        deg_factor = (1.0 - degradation_rate) ^ age_at_year
                        add_to_expression!(total_capacity, rated_power * deg_factor * capacity_credit * risk_coef)
                    elseif rated_power > 0
                        # Past lifetime: capacity from life extension variable (if any)
                        life_ext_var = vars.gen_life_extension[y_idx][g][n]
                        if life_ext_var !== nothing
                            add_to_expression!(total_capacity, life_ext_var, capacity_credit * risk_coef)
                        end
                    end

                end

                # Battery power in service (degraded, life-extended; existing
                # only), firm up to its usable energy over the firm hours
                for b in 1:n_bat
                    bat = input.batteries[b]
                    base_power = bat.max_discharge_power[n]

                    bat_risk = n <= length(bat.risk_coefficient) ? bat.risk_coefficient[n] : 1.0
                    bat_risk *= firm_capacity_credit(bat.availability, n, peak; variable = false,
                        outage = t -> master_outage_factor(input.bat_outage_factors, b, 0, t))
                    # Firm share of its power: min(1, usable energy / H / power);
                    # degradation and life extension keep its energy-to-power ratio
                    firm_share = base_power > 0 ? min(1.0,
                        usable_energy_ratio(bat, n) * bat.capacity[n] / base_power / firm_h) : 0.0
                    base_power > 0 && add_to_expression!(total_capacity,
                        battery_power_in_service(vars, bat, b, n, y_idx), firm_share * bat_risk)
                end

                # Technology investments: cumulative capacity with degradation
                for t in 1:length(input.technologies)
                    tech = input.technologies[t]
                    tech_lifetime = tech.life_time[n]
                    tech_deg = tech.degradation_rate[n]
                    tech_cc = firm_capacity_credit(tech.availability, n, peak;
                        variable = is_variable_renewable(tech, n))
                    tech_risk = n <= length(tech.risk_coefficient) ? tech.risk_coefficient[n] : 1.0
                    for y in 1:vars.years_per_inv_period:y_idx
                        inv_age = y_idx - y
                        if inv_age < tech_lifetime
                            deg_factor = (1.0 - tech_deg) ^ inv_age
                            add_to_expression!(total_capacity, vars.tech_investment[y][t][n], deg_factor * tech_cc * tech_risk)
                        end
                    end
                end

                # Battery technology investments: the power built and in
                # service, firm up to its usable energy over the firm hours:
                # firm ≤ power, firm ≤ usable energy / H
                for bt in 1:length(input.battery_technologies)
                    btech = input.battery_technologies[bt]
                    btech.invest_max_power[n] > 0 || continue
                    btech_risk = n <= length(btech.risk_coefficient) ? btech.risk_coefficient[n] : 1.0
                    btech_risk *= firm_capacity_credit(btech.availability, n, peak; variable = false)
                    firm = @variable(model, lower_bound = 0,
                        base_name = "bat_tech_firm_y$(y_idx)_bt$(bt)_b$(n)")
                    @constraint(model, firm <= bat_tech_power_built(vars, input, bt, n, y_idx),
                        base_name = "bat_tech_firm_power_y$(y_idx)_bt$(bt)_b$(n)")
                    @constraint(model, firm_h * firm <=
                        usable_energy_ratio(btech, n) * bat_tech_energy_built(vars, input, bt, n, y_idx),
                        base_name = "bat_tech_firm_energy_y$(y_idx)_bt$(bt)_b$(n)")
                    add_to_expression!(total_capacity, firm, btech_risk)
                end
            end

            node_capacity[ni] = total_capacity
            node_peak[ni] = peak_demand
        end

        # Peak-hour transfers: bounded by the inter-node transfer capacity
        imports = Dict{Tuple{Int,Int}, VariableRef}()
        for (m, n) in node_pairs
            cap = AffExpr(0.0)
            for i in 1:n_buses, j in 1:n_buses
                (b2n[i] == m && b2n[j] == n) || continue
                add_to_expression!(cap, input.network.connections[i, j])
                for y in 1:vars.years_per_inv_period:y_idx
                    if haskey(vars.transfer_investment, y) &&
                       haskey(vars.transfer_investment[y], (i, j))
                        add_to_expression!(cap, vars.transfer_investment[y][(i, j)])
                    end
                end
            end
            imports[(m, n)] = @variable(model, lower_bound = 0,
                base_name = "cap_import_y$(y_idx)_$(m)_$(n)")
            @constraint(model, imports[(m, n)] <= cap,
                base_name = "cap_import_max_y$(y_idx)_$(m)_$(n)")
        end

        for ni in 1:n_nodes
            haskey(node_capacity, ni) || continue
            net_import = AffExpr(0.0)
            for ((m, n), v) in imports
                n == ni && add_to_expression!(net_import, v)
                m == ni && add_to_expression!(net_import, -1.0, v)
            end
            # Capacity adequacy constraint: capacity + net imports + slack >= peak
            @constraint(model,
                node_capacity[ni] + net_import + vars.slack_capacity[(y_idx, ni)] >= node_peak[ni],
                base_name = "cap_adequacy_y$(y_idx)_node$(ni)"
            )
        end
    end
end

# =============================================================================
# Cumulative Capacity Calculation
# =============================================================================

"""
    build_cumulative_capacity_expressions(vars, input, year_idx)

Build expressions for cumulative capacity up to and including year_idx.

Returns Dict with:
- "gen" => Dict{gen_idx, Vector{AffExpr}} per node
- "bat_power" => Dict{bat_idx, Vector{AffExpr}} per node
- "bat_capacity" => Dict{bat_idx, Vector{AffExpr}} per node
- "tech", "bat_tech_power", "bat_tech_capacity" => the candidates' capacity
  built and in service (`tech_capacity_built`, `bat_tech_power_built`,
  `bat_tech_energy_built`) per node
"""
function build_cumulative_capacity_expressions(
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    year_idx::Int
)::Dict{String, Any}
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node
    n_gen = length(input.generators)
    n_bat = length(input.batteries)

    result = Dict{String, Any}()

    # Generator capacity: unified retirement with degradation
    gen_cap = Dict{Int, Vector{AffExpr}}()
    for g in 1:n_gen
        gen = input.generators[g]
        gen_cap[g] = Vector{AffExpr}(undef, n_buses)

        for n in 1:n_buses
            rated_power = gen.rated_power[n]
            lifetime = gen.life_time[n]
            initial_age = gen.initial_age[n]
            age_at_year = initial_age + (year_idx - 1)
            degradation_rate = gen.degradation_rate[n]

            cap = AffExpr(0.0)

            # Existing: active if age < lifetime, with degradation
            if age_at_year < lifetime && rated_power > 0
                deg_factor = (1.0 - degradation_rate) ^ age_at_year
                add_to_expression!(cap, rated_power * deg_factor)
            elseif rated_power > 0
                # Past lifetime: capacity from life extension variable (if any)
                life_ext_var = vars.gen_life_extension[year_idx][g][n]
                if life_ext_var !== nothing
                    add_to_expression!(cap, life_ext_var)
                end
            end

            gen_cap[g][n] = cap
        end
    end
    result["gen"] = gen_cap

    # Battery power and energy capacity in service (unified retirement)
    result["bat_power"] = Dict{Int, Vector{AffExpr}}(
        b => [battery_power_in_service(vars, input.batteries[b], b, n, year_idx)
              for n in 1:n_buses]
        for b in 1:n_bat)
    result["bat_capacity"] = Dict{Int, Vector{AffExpr}}(
        b => [battery_energy_in_service(vars, input.batteries[b], b, n, year_idx)
              for n in 1:n_buses]
        for b in 1:n_bat)

    # Technology and battery technology capacity built and in service
    # (sums over the investment variables: the objective's fixed O&M)
    result["tech"] = Dict{Int, Vector{AffExpr}}(
        t => [tech_capacity_built(vars, input, t, n, year_idx; state = false)
              for n in 1:n_buses]
        for t in eachindex(input.technologies))
    result["bat_tech_power"] = Dict{Int, Vector{AffExpr}}(
        bt => [bat_tech_power_built(vars, input, bt, n, year_idx; state = false)
               for n in 1:n_buses]
        for bt in eachindex(input.battery_technologies))
    result["bat_tech_capacity"] = Dict{Int, Vector{AffExpr}}(
        bt => [bat_tech_energy_built(vars, input, bt, n, year_idx; state = false)
               for n in 1:n_buses]
        for bt in eachindex(input.battery_technologies))

    return result
end

# =============================================================================
# Objective Function
# =============================================================================

"""
Economic lifetime (years) of a build whose input gives none: transmission
corridors, and reservoirs, electrolyzers and fuel infrastructure without a
positive lifetime. It sets the build's annuity (`capital_recovery_factor`)
and the years it pays it.
"""
const DEFAULT_ASSET_LIFETIME = 40.0

"""
Price of the annual investment budget slack (`add_budget_constraints!`) per
unit of overnight cost over the budget: 10 \$ per \$ exceeded, so a build
over the budget must save ten times its cost.
"""
const BUDGET_SLACK_PENALTY = 10.0

"""
Investment kinds the annual budget covers (`add_budget_constraints!`):
converters and the fuel builds are outside it.
"""
const BUDGET_KINDS = (:tech, :bat_power, :bat_energy, :transfer, :reservoir)

"""
    capital_recovery_factor(rate, life) -> Float64

Share of an overnight cost paid each year so that `life` equal payments are
worth it at discount rate `rate`: `r(1+r)^L / ((1+r)^L − 1)`, or `1/L`
(straight line) when `rate` is 0. A non-positive `life` takes
`DEFAULT_ASSET_LIFETIME`.
"""
function capital_recovery_factor(rate::Real, life::Real)
    L = life > 0 ? Float64(life) : DEFAULT_ASSET_LIFETIME
    abs(rate) < 1e-12 && return 1.0 / L
    g = (1.0 + rate)^L
    return rate * g / (g - 1.0)
end

"""
    annuity_years(build_year, life, num_years) -> UnitRange{Int}

Years of the horizon (indices `1:num_years`) in which a build of year
`build_year` is in service and pays its annuity: from the build year while
its age is below `life` (`DEFAULT_ASSET_LIFETIME` when not positive), as
`tech_capacity_built` keeps it in service.
"""
function annuity_years(build_year::Int, life::Real, num_years::Int)
    L = life > 0 ? life : DEFAULT_ASSET_LIFETIME
    return build_year:min(num_years, build_year + ceil(Int, L) - 1)
end

"""
    annuity_npv_factor(rate, life, build_year, num_years) -> Float64

Discounted annuities per unit of overnight cost of a build of year
`build_year`: `capital_recovery_factor(rate, life)` × Σ 1/(1+rate)^(y−1) over
its `annuity_years`, each year discounted like that year's other costs. A
build whose life fits in the horizon pays `(1+rate)/(1+rate)^(build_year−1)`
per unit (its first payment falls in the build year); one built near the
end pays only for the years left.
"""
annuity_npv_factor(rate::Real, life::Real, build_year::Int, num_years::Int) =
    capital_recovery_factor(rate, life) *
    sum(1.0 / (1.0 + rate)^(y - 1) for y in annuity_years(build_year, life, num_years);
        init = 0.0)

"""
    master_investment_terms(vars, input; mult=nothing) -> Vector{NamedTuple}

Every investment variable of the master as `(kind, year, var, cost, life)`:
its build year (an investment-period start), its overnight cost per unit
built and its economic lifetime (years):
- `:tech`: technology MW, `invest_cost`, `life_time`;
- `:bat_power` / `:bat_energy`: battery-technology MW / MWh,
  `invest_cost_power` / `invest_cost_capacity`, `life_time`;
- `:transfer`: corridor MW, the higher `transference_invest_cost` of its two
  ends (`corridor_cost_terms`, once per corridor), `DEFAULT_ASSET_LIFETIME`;
- `:converter`: converter MW, `invest_cost`, `life_time`;
- `:reservoir`: reservoir MWh, `reservoir_invest_cost`, the generator's
  `life_time`;
- `:fuel`: fuel storage, route transport and electrolyzer builds
  (`master_fuel_investment_terms`).
A stochastic scenario's `mult` scales the costs: technologies by
`invest_cost_renewables` / `invest_cost_conventional`, battery technologies
by `invest_cost_storage` × `bat_invest_cost_power` / `bat_invest_cost_capacity`,
transmission by `invest_cost_transmission`, converters by
`converter_invest_cost`, the fuel builds by theirs.
"""
function master_investment_terms(vars::MasterProblemVariables, input::MasterProblemInput;
                                 mult::Union{Nothing, ScenarioMultipliers} = nothing)
    n_buses = input.network.num_buses
    m_re = mult === nothing ? 1.0 : mult.invest_cost_renewables
    m_conv = mult === nothing ? 1.0 : mult.invest_cost_conventional
    m_sto = mult === nothing ? 1.0 : mult.invest_cost_storage
    m_bat_p = mult === nothing ? m_sto : m_sto * mult.bat_invest_cost_power
    m_bat_e = mult === nothing ? m_sto : m_sto * mult.bat_invest_cost_capacity
    m_tx = mult === nothing ? 1.0 : mult.invest_cost_transmission
    m_cvt = mult === nothing ? 1.0 : mult.converter_invest_cost

    terms = NamedTuple{(:kind, :year, :var, :cost, :life),
                       Tuple{Symbol, Int, VariableRef, Float64, Float64}}[]
    term!(kind, y, v, cost, life) = push!(terms,
        (kind = kind, year = y, var = v, cost = Float64(cost), life = Float64(life)))
    for y in 1:vars.years_per_inv_period:length(input.years)
        for (t, tech) in enumerate(input.technologies)
            m_t = tech.type == "Renewable" ? m_re : m_conv
            for n in 1:n_buses
                term!(:tech, y, vars.tech_investment[y][t][n], tech.invest_cost[n] * m_t,
                      tech.life_time[n])
            end
        end
        for (bt, btech) in enumerate(input.battery_technologies), n in 1:n_buses
            term!(:bat_power, y, vars.bat_tech_power_investment[y][bt][n],
                  btech.invest_cost_power[n] * m_bat_p, btech.life_time[n])
            term!(:bat_energy, y, vars.bat_tech_capacity_investment[y][bt][n],
                  btech.invest_cost_capacity[n] * m_bat_e, btech.life_time[n])
        end
        for (v, cost) in corridor_cost_terms(vars.transfer_investment[y],
                                             input.network.transference_invest_cost)
            term!(:transfer, y, v, cost * m_tx, DEFAULT_ASSET_LIFETIME)
        end
        for (key, conv) in master_converters(input.network)
            v = get(vars.converter_investment[y], key, nothing)
            v === nothing || term!(:converter, y, v, conv.invest_cost * m_cvt, conv.life_time)
        end
        for (g, gen) in enumerate(input.generators)
            haskey(vars.reservoir_investment[y], g) || continue
            for n in 1:n_buses
                gen.reservoir_invest_max[n] > 0 || continue
                term!(:reservoir, y, vars.reservoir_investment[y][g][n],
                      gen.reservoir_invest_cost[n], gen.life_time[n])
            end
        end
    end
    # M2 (#89): fuel storage, route transport and electrolyzer builds
    for (y, v, cost, life) in master_fuel_investment_terms(vars, input; mult = mult)
        term!(:fuel, y, v, cost, life)
    end
    return terms
end

"""
    build_investment_cost_expression(vars, input; mult=nothing, discount_rate=input.discount_rate)

Discounted investment cost of the plan, as annuities: each unit (MW, MWh)
built pays `capital_recovery_factor(discount_rate, life)` × its overnight
cost in every year of the horizon it is in service (`annuity_years`),
discounted to the base year like the other annual costs
(`annuity_npv_factor`). Nothing is charged after the horizon, so a build
near its end pays only for the years it serves, as in GenX and PyPSA,
instead of its whole overnight cost with no residual value. The terms and
the stochastic multipliers are `master_investment_terms`'; `discount_rate`
is the scenario's in a stochastic master.
"""
function build_investment_cost_expression(
    vars::MasterProblemVariables,
    input::MasterProblemInput;
    mult::Union{Nothing, ScenarioMultipliers} = nothing,
    discount_rate::Float64 = input.discount_rate
)::AffExpr
    num_years = length(input.years)
    inv_cost = AffExpr(0.0)
    for t in master_investment_terms(vars, input; mult = mult)
        t.cost == 0 && continue
        add_to_expression!(inv_cost, t.var,
            t.cost * annuity_npv_factor(discount_rate, t.life, t.year, num_years))
    end
    return inv_cost
end

"""
    adequacy_slack_penalty(input, y_idx, ni) -> Float64

Price per MW of capacity adequacy shortfall at node `ni` in year `y_idx`
(`slack_capacity`): the loss-of-load penalty (VOLL, per MWh) × the hours of
the node's peak period, the steps `adequacy_peak_steps` takes as the peak
(the top 10 % of the year's demand) × the step length. A MW missing at the
peak would leave load unserved in each of those hours, so the slack costs
more than any firm capacity and binds unless no build can close the gap.
"""
adequacy_slack_penalty(input::MasterProblemInput, y_idx::Int, ni::Int) =
    input.loss_of_load_penalty * length(adequacy_peak_steps(input, y_idx, ni)) *
    max(1, input.temporal_resolution_hours)

"""
    master_slack_cost(vars, input; discount_rate=input.discount_rate) -> AffExpr

Cost of the first-stage slacks, each year discounted like its other costs:
the investment over the annual budget at `BUDGET_SLACK_PENALTY` per unit and
the capacity adequacy shortfall at `adequacy_slack_penalty` per MW. (The RE
target's shortfall is an operating cost of the year, `add_re_target_constraints!`.)
"""
function master_slack_cost(vars::MasterProblemVariables, input::MasterProblemInput;
                           discount_rate::Float64 = input.discount_rate)
    cost = AffExpr(0.0)
    for y_idx in eachindex(input.years)
        df = 1.0 / (1.0 + discount_rate)^(y_idx - 1)
        v = get(vars.slack_budget, y_idx, nothing)
        v === nothing || add_to_expression!(cost, v, BUDGET_SLACK_PENALTY * df)
        for ni in 1:input.network.num_nodes
            v = get(vars.slack_capacity, (y_idx, ni), nothing)
            v === nothing && continue
            add_to_expression!(cost, v, adequacy_slack_penalty(input, y_idx, ni) * df)
        end
    end
    return cost
end

"""
    build_annual_asset_cost(vars, input, y_idx; mult=nothing, by_unit=nothing)

Undiscounted per-year cost of existing assets: life extension of units past
their lifetime, (incremental, LP-linear) decommissioning, the fixed cost
(\$/MW/year) of the converter capacity built so far and the fixed O&M
(\$/MW/year) of the capacity in service:
- generators and technologies: the power `build_cumulative_capacity_expressions`
  gives (existing units within their lifetime, degraded; life-extended MW;
  technology MW built, degraded and not yet retired), the capacity the
  dispatch receives;
- batteries: rated (not degraded) discharge power of existing ones within
  their lifetime, life-extended MW, and the rated battery-technology power
  built and not yet retired.
A stochastic scenario's `mult` scales the generator and battery
decommissioning costs, and `gen_fixed_cost` scales every fixed O&M.
Units the NPV iteration retired (`input.npv_retired_units`) pay their
decommissioning in the first year.

With a dict `by_unit`, each existing unit's share (fixed O&M, life
extension, decommissioning) is also added to `by_unit[(:gen | :bat, i, bus)]`,
for its NPV (`extract_unit_operation`).
"""
function build_annual_asset_cost(
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    y_idx::Int;
    mult::Union{Nothing, ScenarioMultipliers} = nothing,
    by_unit::Union{Nothing, AbstractDict} = nothing
)::AffExpr
    n_buses = input.network.num_buses
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    m_gen_decomm = mult === nothing ? 1.0 : mult.gen_decommissioning_cost
    m_bat_decomm = mult === nothing ? 1.0 : mult.bat_decommissioning_cost
    m_fixed = mult === nothing ? 1.0 : mult.gen_fixed_cost
    annual_cost = AffExpr(0.0)
    # A term of existing unit `key`: also its own share when asked for
    function charge!(key, args...)
        add_to_expression!(annual_cost, args...)
        by_unit === nothing ||
            add_to_expression!(get!(() -> AffExpr(0.0), by_unit, key), args...)
    end

    # Life extension costs (annual): cost to keep units past lifetime
    for g in 1:n_gen
        gen = input.generators[g]
        for n in 1:n_buses
            life_ext_var = vars.gen_life_extension[y_idx][g][n]
            if life_ext_var !== nothing
                ext_cost = gen.invest_cost[n] * input.life_extension_cost_factor
                charge!((:gen, g, n), life_ext_var, ext_cost)
            end
        end
    end
    for b in 1:n_bat
        bat = input.batteries[b]
        for n in 1:n_buses
            life_ext_var = vars.bat_life_extension[y_idx][b][n]
            if life_ext_var !== nothing
                ext_cost = bat.invest_cost_power[n] * input.life_extension_cost_factor
                charge!((:bat, b, n), life_ext_var, ext_cost)
            end
        end
    end

    # Generator and technology fixed O&M on the power in service this year
    if any(any(>(0), u.fixed_cost) for u in input.generators) ||
       any(any(>(0), u.fixed_cost) for u in input.technologies)
        caps = build_cumulative_capacity_expressions(vars, input, y_idx)
        for (key, units) in (("gen", input.generators), ("tech", input.technologies))
            for (i, u) in enumerate(units), n in 1:n_buses
                fc = u.fixed_cost[n] * m_fixed
                fc > 0 || continue
                if key == "gen"
                    charge!((:gen, i, n), caps[key][i][n], fc)
                else
                    add_to_expression!(annual_cost, caps[key][i][n], fc)
                end
            end
        end
    end

    # Battery fixed cost on the discharge power in service this year
    for b in 1:n_bat
        bat = input.batteries[b]
        for n in 1:n_buses
            fc = bat.fixed_cost[n] * m_fixed
            (fc > 0 && bat.max_discharge_power[n] > 0) || continue
            if bat.initial_age[n] + (y_idx - 1) < bat.life_time[n]
                charge!((:bat, b, n), fc * bat.max_discharge_power[n])
            else
                lev = vars.bat_life_extension[y_idx][b][n]
                lev === nothing || charge!((:bat, b, n), lev, fc)
            end
        end
    end
    for (bt, btech) in enumerate(input.battery_technologies), n in 1:n_buses
        fc = btech.fixed_cost[n] * m_fixed
        fc > 0 || continue
        add_to_expression!(annual_cost, bat_tech_power_rated(vars, input, bt, n, y_idx), fc)
    end

    # Fixed cost of the converter capacity built up to this year
    for (key, conv) in master_converters(input.network)
        conv.fixed_cost > 0 || continue
        add_to_expression!(annual_cost, conv.fixed_cost,
                           converter_built(vars, key, y_idx; state = false))
    end

    # Decommissioning costs (incremental, LP-linear)
    for g in 1:n_gen
        gen = input.generators[g]
        for n in 1:n_buses
            rated_power = gen.rated_power[n]
            decomm_cost = gen.decommissioning_cost[n] * m_gen_decomm
            if rated_power <= 0 || decomm_cost <= 0
                continue
            end
            lifetime = gen.life_time[n]
            initial_age = gen.initial_age[n]
            # Year index when retirement first occurs
            retirement_y = max(1, ceil(Int, lifetime - initial_age + 1))

            if y_idx == retirement_y
                # First retirement year: decommission full capacity minus life ext
                age = initial_age + y_idx - 1
                deg = (1.0 - gen.degradation_rate[n]) ^ age
                full_cap = rated_power * deg
                charge!((:gen, g, n), full_cap * decomm_cost)
                lev = vars.gen_life_extension[y_idx][g][n]
                if lev !== nothing
                    charge!((:gen, g, n), lev, -decomm_cost)
                end
            elseif y_idx > retirement_y
                # Subsequent years: incremental decommissioning of life-extended capacity
                prev = vars.gen_life_extension[y_idx - 1][g][n]
                curr = vars.gen_life_extension[y_idx][g][n]
                if prev !== nothing
                    charge!((:gen, g, n), prev, decomm_cost)
                    if curr !== nothing
                        charge!((:gen, g, n), curr, -decomm_cost)
                    end
                end
            end
        end
    end
    # Battery decommissioning
    for b in 1:n_bat
        bat = input.batteries[b]
        for n in 1:n_buses
            base_power = bat.max_discharge_power[n]
            decomm_cost = bat.decommissioning_cost[n] * m_bat_decomm
            if base_power <= 0 || decomm_cost <= 0
                continue
            end
            lifetime = bat.life_time[n]
            initial_age = bat.initial_age[n]
            retirement_y = max(1, ceil(Int, lifetime - initial_age + 1))

            if y_idx == retirement_y
                # Its degraded power then (the most life extension keeps)
                charge!((:bat, b, n),
                        base_power * battery_degradation_factor(bat, n, y_idx) * decomm_cost)
                lev = vars.bat_life_extension[y_idx][b][n]
                if lev !== nothing
                    charge!((:bat, b, n), lev, -decomm_cost)
                end
            elseif y_idx > retirement_y
                prev = vars.bat_life_extension[y_idx - 1][b][n]
                curr = vars.bat_life_extension[y_idx][b][n]
                if prev !== nothing
                    charge!((:bat, b, n), prev, decomm_cost)
                    if curr !== nothing
                        charge!((:bat, b, n), curr, -decomm_cost)
                    end
                end
            end
        end
    end
    # Units the NPV iteration retired: decommissioned in the first year
    if y_idx == 1
        for ru in input.npv_retired_units
            ru.decommissioned_mw > 0 || continue
            if ru.unit_type == "generator"
                dc = input.generators[ru.unit_idx].decommissioning_cost[ru.bus] * m_gen_decomm
                dc > 0 && charge!((:gen, ru.unit_idx, ru.bus), ru.decommissioned_mw * dc)
            else
                dc = input.batteries[ru.unit_idx].decommissioning_cost[ru.bus] * m_bat_decomm
                dc > 0 && charge!((:bat, ru.unit_idx, ru.bus), ru.decommissioned_mw * dc)
            end
        end
    end

    # M2 (#89): electrolyzer fixed cost (existing + built)
    add_to_expression!(annual_cost, master_fuel_annual_cost(vars, input, y_idx; mult = mult))

    return annual_cost
end

"""
    build_master_objective!(model, vars, input)

Build the objective function: the discounted investment annuities
(`build_investment_cost_expression`), the existing assets' annual costs,
the representative days' operating cost (RE-target shortfall and CO2-budget
violations included) and the budget and adequacy slacks (`master_slack_cost`).
"""
function build_master_objective!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)
    num_years = length(input.years)
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node
    n_gen = length(input.generators)
    n_bat = length(input.batteries)

    total_cost = build_investment_cost_expression(vars, input)

    # Per-year costs: life extension, decommissioning, operational (every year, not just period starts)
    for y_idx in 1:num_years
        discount_factor = 1.0 / ((1.0 + input.discount_rate)^(y_idx - 1))
        annual_cost = build_annual_asset_cost(vars, input, y_idx)

        # Add discounted per-year costs
        add_to_expression!(total_cost, annual_cost, discount_factor)

        # Operational costs (second stage) - added from subproblems
        if haskey(vars.operational_costs, y_idx)
            for day_cost in vars.operational_costs[y_idx]
                add_to_expression!(total_cost, day_cost, discount_factor)
            end
        end
    end

    # Budget and adequacy slacks (the RE shortfall is in the operating cost)
    add_to_expression!(total_cost, master_slack_cost(vars, input))

    @objective(model, Min, total_cost)

    return total_cost  # Return for MGA near-optimal constraint
end

# =============================================================================
# Representative Days Operational Validation (CRITICAL for correct RE targets)
# =============================================================================

"""
    create_day_ps_vars!(model, input, year_idx, day_idx, hours)

Create PowerSystemVariables for a representative day.
Uses power_system.jl's PowerSystemVariables type directly instead of a separate struct.
"""
function create_day_ps_vars!(
    model::Model,
    input::MasterProblemInput,
    year_idx::Int,
    day_idx::Int,
    hours::Int
)::PowerSystemVariables
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_buses = input.network.num_buses
    n_nodes = input.network.num_nodes
    prefix = "op_y$(year_idx)_d$(day_idx)"

    # Pre-compute lookup maps: which buses each generator/battery is active at
    # (existing generators only have their rated power; what the master
    # builds is candidate technologies, see add_day_operational_constraints!)
    buses_of_gen = [Int[] for _ in 1:n_gen]
    gens_at_bus = [Int[] for _ in 1:n_buses]
    for g in 1:n_gen
        gen = input.generators[g]
        for b in 1:n_buses
            if gen.rated_power[b] > 0
                push!(buses_of_gen[g], b)
                push!(gens_at_bus[b], g)
            end
        end
    end

    buses_of_bat = [Int[] for _ in 1:n_bat]
    bats_at_bus = [Int[] for _ in 1:n_buses]
    for bi in 1:n_bat
        bat = input.batteries[bi]
        for b in 1:n_buses
            has_cap = bat.max_discharge_power[b] > 0
            has_invest = length(bat.invest_max_power) >= b && bat.invest_max_power[b] > 0
            if has_cap || has_invest
                push!(buses_of_bat[bi], b)
                push!(bats_at_bus[b], bi)
            end
        end
    end

    # Generator output — SparseAxisArray (only active gen-bus pairs)
    gen_output = @variable(model,
        [g=1:n_gen, b=buses_of_gen[g], t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_gen")

    # gen_status fixed at 1 (economic dispatch — no UC in master)
    gen_status = @variable(model,
        [g=1:n_gen, b=buses_of_gen[g], t=1:hours],
        lower_bound = 1, upper_bound = 1, base_name = "$(prefix)_gs")

    # Curtailment [node, hour] (per-node, matching PowerSystemVariables convention)
    curtailment = @variable(model,
        [n=1:n_nodes, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_curt")

    # FRE penetration loss [node, hour]
    fre_penetration_loss = @variable(model,
        [n=1:n_nodes, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_fre_loss")

    # Battery variables — SparseAxisArray (only active bat-bus pairs)
    bat_charge = @variable(model,
        [bi=1:n_bat, b=buses_of_bat[bi], t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_bat_ch")

    bat_discharge = @variable(model,
        [bi=1:n_bat, b=buses_of_bat[bi], t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_bat_dch")

    # bat_soc uses hours+1 convention (soc[1]=initial, soc[t+1]=after hour t)
    bat_soc = @variable(model,
        [bi=1:n_bat, b=buses_of_bat[bi], t=1:(hours+1)],
        lower_bound = 0, base_name = "$(prefix)_bat_soc")

    # Voltage angle + power flow by line (DC power flow for multi-bus)
    voltage_angle = @variable(model,
        [b=1:n_buses, t=1:hours],
        lower_bound = -π, upper_bound = π,
        base_name = "$(prefix)_vangle")

    # Bus pair → line indices (populated by add_dc_constraints!)
    pair_lines = Dict{Tuple{Int,Int}, Vector{Int}}()

    # Reserve variables [node, hour]
    reserve_static = @variable(model,
        [n=1:n_nodes, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_rs")
    reserve_dynamic = @variable(model,
        [n=1:n_nodes, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_rd")
    reserve_static_loss = @variable(model,
        [n=1:n_nodes, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_rs_loss")
    reserve_dynamic_loss = @variable(model,
        [n=1:n_nodes, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_rd_loss")

    # Load shedding [bus, hour] (B2: indexed per-bus to match operational layout).
    # In the master, ``buses == nodes`` (one synthetic bus per node) so n_buses == n_nodes.
    load_shed = @variable(model,
        [b=1:n_buses, t=1:hours],
        lower_bound = 0, base_name = "$(prefix)_ll")

    # Inertia loss variable (only if inertia limit is configured)
    loss_of_inertia_var = if input.inertia_limit > 0
        @variable(model, [t=1:hours], lower_bound=0,
            base_name = "$(prefix)_inertia_loss")
    else
        nothing
    end

    # Sectoral loss of load variables (M2)
    loss_of_load_sectoral = Dict{String, Matrix{VariableRef}}()
    if !isempty(input.sectoral_criticality)
        for (sector, _) in input.sectoral_criticality
            loss_of_load_sectoral[sector] = @variable(model,
                [b=1:n_buses, t=1:hours],
                lower_bound = 0, base_name = "$(prefix)_lol_$(sector)")
        end
    end

    # Segment variables for PWL cost decomposition (bidding curves)
    gen_seg_output = Dict{Tuple{Int,Int}, Any}()
    for (g, bus_curves) in input.gen_cost_curves
        for (b, segs) in bus_curves
            if !isempty(segs) && b in buses_of_gen[g]
                n_seg = length(segs)
                sv = @variable(model, [k=1:n_seg, t=1:hours],
                               lower_bound=0, base_name="$(prefix)_gseg_$(g)_$(b)")
                gen_seg_output[(g, b)] = sv
                for t in 1:hours
                    @constraint(model, gen_output[g, b, t] ==
                        sum(sv[k, t] for k in 1:n_seg))
                end
            end
        end
    end

    bat_seg_discharge = Dict{Tuple{Int,Int}, Any}()
    for (bi, bus_curves) in input.bat_cost_curves
        for (b, segs) in bus_curves
            if !isempty(segs) && b in buses_of_bat[bi]
                n_seg = length(segs)
                sv = @variable(model, [k=1:n_seg, t=1:hours],
                               lower_bound=0, base_name="$(prefix)_bseg_$(bi)_$(b)")
                bat_seg_discharge[(bi, b)] = sv
                for t in 1:hours
                    @constraint(model, bat_discharge[bi, b, t] ==
                        sum(sv[k, t] for k in 1:n_seg))
                end
            end
        end
    end

    # Reservoir hydro variables for the representative day. Only created when at
    # least one generator has a reservoir, so all-thermal/RE systems are not
    # bloated. add_reservoir_constraints! enforces the water balance + cyclic
    # level so hydro is energy-limited in the master (not treated as firm MW).
    has_any_reservoir = any(any(g.reservoir_capacity .> 0) for g in input.generators)
    reservoir_level = nothing
    reservoir_spillage = nothing
    reservoir_pump = nothing
    if has_any_reservoir
        reservoir_level = @variable(model,
            [g=1:n_gen, b=buses_of_gen[g], t=1:(hours+1)],
            lower_bound = 0, base_name = "$(prefix)_res_level")
        reservoir_spillage = @variable(model,
            [g=1:n_gen, b=buses_of_gen[g], t=1:hours],
            lower_bound = 0, base_name = "$(prefix)_res_spill")
        reservoir_pump = @variable(model,
            [g=1:n_gen, b=buses_of_gen[g], t=1:hours],
            lower_bound = 0, base_name = "$(prefix)_res_pump")
    end

    return PowerSystemVariables(
        gen_output, gen_status, nothing, nothing,  # no startup/shutdown
        curtailment, fre_penetration_loss,
        bat_charge, bat_discharge, bat_soc,
        pair_lines, voltage_angle,
        reserve_static, reserve_dynamic, reserve_static_loss, reserve_dynamic_loss,
        load_shed,
        buses_of_gen, gens_at_bus, buses_of_bat, bats_at_bus;
        loss_of_inertia = loss_of_inertia_var,
        loss_of_load_sectoral = loss_of_load_sectoral,
        gen_seg_output = gen_seg_output,
        bat_seg_discharge = bat_seg_discharge,
        reservoir_level = reservoir_level,
        reservoir_spillage = reservoir_spillage,
        reservoir_pump = reservoir_pump,
        # M2 (#89): electrolyzers of the fuel block (load in the bus balance)
        electrolyzer_power = master_electrolyzer_power_vars(model, input, prefix, hours)
    )
end

"""
    generators_for_day(generators, start_hour, hours)

The generators as a representative day (or TSAM period) starting at master
step `start_hour` sees them. The day model reads `availability[t, b]` and
`reservoir_inflow[t, b]` by its own step `t = 1..hours`, so both profiles are
cut to rows `start_hour : start_hour + hours - 1` (wrapping around a shorter
profile). A generator with neither profile is returned as is.
"""
function generators_for_day(generators::AbstractVector, start_hour::Int, hours::Int)
    rows(m) = size(m, 1) == 0 ? m :
        m[[mod1(start_hour + t - 1, size(m, 1)) for t in 1:hours], :]
    return map(generators) do gen
        size(gen.availability, 1) == 0 && size(gen.reservoir_inflow, 1) == 0 && return gen
        GeneratorConfig(
            gen.name, gen.type, gen.fuel,
            gen.rated_power, gen.min_power,
            gen.efficiency_rated, gen.efficiency_min,
            gen.ramp_up, gen.ramp_down,
            gen.min_up_time, gen.min_down_time,
            gen.start_up_cost, gen.fuel_cost,
            gen.fixed_cost, gen.maintenance_cost,
            gen.inertia, gen.invest_cost,
            rows(gen.availability), gen.reservable,
            gen.life_time, gen.initial_age,
            gen.degradation_rate, gen.decommissioning_cost,
            gen.frequency_hz, gen.current_type,
            gen.reservoir_capacity, gen.reservoir_initial_level,
            gen.reservoir_min_level, gen.reservoir_max_level,
            rows(gen.reservoir_inflow), gen.reservoir_turbine_efficiency,
            gen.reservoir_evaporation_rate, gen.reservoir_pump_capacity,
            gen.reservoir_pump_efficiency, gen.reservoir_spillage_allowed,
            gen.reservoir_invest_cost, gen.reservoir_invest_max,
            gen.risk_coefficient,
            gen.reservoir_min_release,
            gen.cascade_downstream, gen.cascade_delay_hours,
            gen.reservoir_head_min_factor, gen.num_units
        )
    end
end

"""
    master_outage_factor(factors, unit, node, h) -> Float64

Scheduled-outage availability factor of unit `unit` in master step `h`
(`gen_outage_factors` / `bat_outage_factors` of `MasterProblemInput`): the
product of the unit's whole-unit factor `(unit, 0)` and, for a node `> 0`,
the factor of its windows at that node `(unit, node)`. 1.0 without a window
or past the end of a factor series.
"""
function master_outage_factor(factors::AbstractDict, unit::Int, node::Int, h::Int)
    isempty(factors) && return 1.0
    f = 1.0
    for key in (node > 0 ? ((unit, 0), (unit, node)) : ((unit, 0),))
        v = get(factors, key, nothing)
        v === nothing || h > length(v) || h < 1 || (f *= v[h])
    end
    return f
end

has_outage(factors::AbstractDict, unit::Int) =
    !isempty(factors) && any(k -> k[1] == unit, keys(factors))

"""
    with_day_outages(units, factors, nodes_of_bus, start_hour, hours)

`units` (a day's generators or batteries, their availability already cut to
the day) with their scheduled outages folded in: each step `t` of a unit
with a window is × `master_outage_factor` at master step `start_hour + t - 1`,
as the dispatch multiplies its availability by `compile_outage_factor`. A
unit without a profile gets one (all ones before the outage).
"""
function with_day_outages(units::AbstractVector, factors::AbstractDict,
                          nodes_of_bus::AbstractVector{Int}, start_hour::Int, hours::Int)
    isempty(factors) && return units
    return map(enumerate(units)) do (i, u)
        has_outage(factors, i) || return u
        m = u.availability
        nb = length(nodes_of_bus)
        a = size(m, 1) == 0 ? ones(hours, nb) : Matrix{Float64}(m)
        for t in 1:min(hours, size(a, 1)), b in 1:min(nb, size(a, 2))
            a[t, b] *= master_outage_factor(factors, i, nodes_of_bus[b], start_hour + t - 1)
        end
        with_fields(u; availability = a)
    end
end

"""
    generators_for_day(input::MasterProblemInput, start_hour, hours)
    batteries_for_day(input::MasterProblemInput, start_hour, hours)

The master's existing generators (batteries) as a representative day or
TSAM period starting at master step `start_hour` sees them: their profiles
cut to the day and their scheduled outages folded in (`with_day_outages`).
"""
generators_for_day(input::MasterProblemInput, start_hour::Int, hours::Int) =
    with_day_outages(generators_for_day(input.generators, start_hour, hours),
                     input.gen_outage_factors, input.network.bus_to_node, start_hour, hours)

batteries_for_day(input::MasterProblemInput, start_hour::Int, hours::Int) =
    with_day_outages(batteries_for_day(input.batteries, start_hour, hours),
                     input.bat_outage_factors, zeros(Int, input.network.num_buses),
                     start_hour, hours)

"""
    batteries_for_day(batteries, start_hour, hours)

The batteries (or battery technologies) as a representative day starting at
master step `start_hour` sees them: an availability profile is cut to rows
`start_hour : start_hour + hours - 1` (wrapping around a shorter profile),
as `generators_for_day` does. A unit without a profile is returned as is.
"""
function batteries_for_day(batteries::AbstractVector, start_hour::Int, hours::Int)
    return map(batteries) do bat
        m = bat.availability
        size(m, 1) == 0 && return bat
        with_fields(bat; availability =
            m[[mod1(start_hour + t - 1, size(m, 1)) for t in 1:hours], :])
    end
end

"""
    tech_availability_at(tech, hour, b) -> Float64

Availability of technology `tech` at bus `b` in master step `hour`
(wrapping around a shorter profile; 1.0 without a profile): the capacity
factor of a renewable, the derates and outages of a dispatchable unit, as
the generator it becomes in the dispatch gets them.
"""
function tech_availability_at(tech, hour::Int, b::Int)
    m = tech.availability
    return size(m, 1) == 0 ? 1.0 : m[mod1(hour, size(m, 1)), b]
end

"""
    master_unit_reserve_terms(input, start_hour, b, ts)
        -> Vector{Tuple{Symbol, Int, Float64, Float64}}

The candidate units that hold reserve at bus `b`, step `ts` of a master
day or TSAM period starting at master step `start_hour`, by the dispatch's
rules for the units they become (`add_reserve_constraints!`), as
`(kind, index, a, d)`:

- `(:tech, t, a, d)`: a reservable, dispatchable technology (not a variable
  renewable, see `is_variable_renewable`). Headroom
  `a × cap − output` with `a` its availability; dynamic capability
  `d × cap`, `d = dynamic_reserve_contribution`.
- `(:bat_tech, bt, a, d)`: a reservable battery technology. Headroom
  `a × P − discharge + charge` with `P` the discharge power built and `a` its
  availability; dynamic capability `d × P`, `d = a × dynamic_reserve_contribution`.

`a` and `d` are the coefficients of the capacity built (master investment
expressions, Benders subproblem constants) in the system's reserve headroom
and dynamic-capability constraints.
"""
function master_unit_reserve_terms(input::MasterProblemInput, start_hour::Int,
                                   b::Int, ts::Int)
    out = Tuple{Symbol, Int, Float64, Float64}[]
    contribution = input.dynamic_reserve_contribution
    hour = start_hour + ts - 1
    at_hour(m) = size(m, 1) == 0 ? 1.0 : m[mod1(hour, size(m, 1)), b]
    for (t, tech) in enumerate(input.technologies)
        (tech.reservable && !is_variable_renewable(tech, b)) || continue
        push!(out, (:tech, t, tech_availability_at(tech, hour, b), contribution))
    end
    for (bt, btech) in enumerate(input.battery_technologies)
        btech.reservable || continue
        a = at_hour(btech.availability)
        push!(out, (:bat_tech, bt, a, contribution * a))
    end
    return out
end

"""
    master_unit_headroom(input, start_hour, tech_output, tech_cap,
                         bt_dis, bt_ch, bt_pow) -> Function

`extra_headroom` for `add_reserve_constraints!` in a master day: `(b, ts) ->
(headroom, dynamic)` of the candidate units at bus `b`
(`master_unit_reserve_terms`), or `nothing` when none holds reserve.
`tech_cap(t, b)` and `bt_pow(bt, b)` give the capacity built; `tech_output`,
`bt_dis` and `bt_ch` are the day's dispatch variables (`nothing` without
technologies / battery technologies).
"""
function master_unit_headroom(input::MasterProblemInput, start_hour::Int, tech_output,
                              tech_cap::Function, bt_dis, bt_ch, bt_pow::Function)
    return (b, ts) -> begin
        headroom = AffExpr(0.0)
        dynamic = AffExpr(0.0)
        any_unit = false
        for (kind, i, a, d) in master_unit_reserve_terms(input, start_hour, b, ts)
            if kind === :tech
                tech_output === nothing && continue
                cap = tech_cap(i, b)
                add_to_expression!(headroom, cap, a)
                add_to_expression!(headroom, -1.0, tech_output[i, b, ts])
                add_to_expression!(dynamic, cap, d)
            else
                bt_dis === nothing && continue
                P = bt_pow(i, b)
                add_to_expression!(headroom, P, a)
                add_to_expression!(headroom, -1.0, bt_dis[i, b, ts])
                add_to_expression!(headroom, 1.0, bt_ch[i, b, ts])
                add_to_expression!(dynamic, P, d)
            end
            any_unit = true
        end
        return any_unit ? (headroom, dynamic) : nothing
    end
end

"""
    add_battery_tech_day!(model, input, pow_cap, en_cap, start_hour, hours;
                          suffix="", store=nothing) -> (discharge, charge, soc)

The candidate battery technologies' operation on one representative day or
TSAM period, as the dispatch runs the batteries they build: charge and
discharge within the power built × availability, ramp limits (BatteryConfig),
SOC within the energy built, cyclic at `soc_initial`. `pow_cap(bt, b)` and
`en_cap(bt, b)` give the capacity built: an expression of the investment
variables in the master, a number in a Benders subproblem. With `store`,
each constraint whose right-hand side holds the power (energy: the SOC limit
and the initial and cyclic SOC) is recorded in `store["bt_pow"]`
(`store["bt_cap"]`) as `(constraint, coefficient, bt, b)`, for the cut. Constraint names end in `suffix`, variable names in `var_suffix`.
"""
function add_battery_tech_day!(model::Model, input::MasterProblemInput,
                               pow_cap::Function, en_cap::Function,
                               start_hour::Int, hours::Int;
                               suffix::String = "", var_suffix::String = suffix,
                               store = nothing)
    n_bat_tech = length(input.battery_technologies)
    n_buses = input.network.num_buses
    tres = input.temporal_resolution_hours
    vsfx = var_suffix
    dis = @variable(model, [bt=1:n_bat_tech, b=1:n_buses, ts=1:hours],
        lower_bound = 0, base_name = "bat_tech_dis$(vsfx)")
    ch = @variable(model, [bt=1:n_bat_tech, b=1:n_buses, ts=1:hours],
        lower_bound = 0, base_name = "bat_tech_ch$(vsfx)")
    soc = @variable(model, [bt=1:n_bat_tech, b=1:n_buses, ts=0:hours],
        lower_bound = 0, base_name = "bat_tech_soc$(vsfx)")
    rec(kind, c, coef, bt, b) = store === nothing ||
        push!(get!(store, kind, Tuple{Any, Float64, Int, Int}[]), (c, coef, bt, b))

    day_techs = batteries_for_day(input.battery_technologies, start_hour, hours)
    for (bt, btech) in enumerate(day_techs), b in 1:n_buses
        P = pow_cap(bt, b)
        E = en_cap(bt, b)
        for ts in 1:hours
            avail = storage_availability(btech.availability, ts, b)
            c = @constraint(model, dis[bt, b, ts] <= P * avail,
                base_name = "bt_dis_cap_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
            rec("bt_pow", c, avail, bt, b)
            c = @constraint(model, ch[bt, b, ts] <= P * avail,
                base_name = "bt_ch_cap_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
            rec("bt_pow", c, avail, bt, b)
        end
        for ts in 0:hours
            c = @constraint(model, soc[bt, b, ts] <= E,
                base_name = "bt_soc_cap_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
            rec("bt_cap", c, 1.0, bt, b)
        end
        for flow in (ch, dis)
            for (c, coef) in add_storage_ramp_constraints!(model, ts -> flow[bt, b, ts],
                    P, NaN, btech.ramp_up[b], btech.ramp_down[b], tres, hours)
                rec("bt_pow", c, coef, bt, b)
            end
        end
        ch_eff = btech.charge_efficiency[b]
        dis_eff = btech.discharge_efficiency[b]
        # Clamp to soc_min to avoid initial/end SOC vs soc_min conflict
        initial_soc_frac = max(btech.soc_initial[b], btech.soc_min[b])
        # Skip SOC dynamics if efficiency is zero (tech not at this node)
        if dis_eff <= 0.0 || ch_eff <= 0.0
            for ts in 0:hours
                @constraint(model, soc[bt, b, ts] <= 0,
                    base_name = "bt_soc_zero_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
            end
            for ts in 1:hours
                @constraint(model, dis[bt, b, ts] <= 0,
                    base_name = "bt_dis_zero_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
                @constraint(model, ch[bt, b, ts] <= 0,
                    base_name = "bt_ch_zero_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
            end
        else
            c = @constraint(model, soc[bt, b, 0] == E * initial_soc_frac,
                base_name = "bt_soc_init_bt$(bt)_b$(b)$(suffix)")
            rec("bt_cap", c, initial_soc_frac, bt, b)
            # SOC in MWh; charge/discharge in MW over a step of tres hours
            for ts in 1:hours
                @constraint(model,
                    soc[bt, b, ts] == soc[bt, b, ts-1]
                        + tres * (ch_eff * ch[bt, b, ts] - (1.0/dis_eff) * dis[bt, b, ts]),
                    base_name = "bt_soc_dyn_bt$(bt)_b$(b)_ts$(ts)$(suffix)")
            end
            c = @constraint(model, soc[bt, b, hours] == E * initial_soc_frac,
                base_name = "bt_soc_cyc_bt$(bt)_b$(b)$(suffix)")
            rec("bt_cap", c, initial_soc_frac, bt, b)
        end
    end
    return dis, ch, soc
end

"""
    add_day_operational_constraints!(model, ps_vars, vars, input, year_idx, day_idx, demand, start_hour)

Add operational constraints for a representative day by delegating to power_system.jl
and transmission_dc.jl functions. Only master-specific logic (capacity precomputation,
technology dispatch, RE enforcement, sectoral LoL) remains here.

Returns `(tech_output, tech_day_cost, tech_spend)`: the candidate
technologies' dispatch and their operating cost that `day_operational_cost`
does not see (the offer-curve fuel cost of technologies with a
`fuel_cost_curve`, the curtailment penalty of candidate renewables, the
battery technologies' maintenance and throughput degradation, converter,
line-flow and start-up costs), unscaled like `day_operational_cost`; the
callers scale both to the year the same way. `tech_spend` is the money part
of `tech_day_cost` (no penalty, no degradation), for `day_operating_spend`. With `curtailment`, the
day's per-system curtailment, renewable output and RE-3c exemption are
added to the year's with `period_scale` for its annual curtailment limit,
which the caller adds (`add_curtailment_limit!`).

CRITICAL: This links investment decisions to operational feasibility.
"""
function add_day_operational_constraints!(
    model::Model,
    ps_vars::PowerSystemVariables,
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    year_idx::Int,
    day_idx::Int,
    demand::Matrix{Float64},
    start_hour::Int;
    # TSAM inter-period SOC linking overrides (optional)
    initial_soc_overrides::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing,
    final_soc_targets::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing,
    # TSAM inter-period reservoir-level linking overrides (optional, seasonal hydro)
    initial_reservoir_overrides::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing,
    final_reservoir_targets::Union{Nothing, Dict{Tuple{Int,Int}, Any}} = nothing,
    # Inter-system DC-OPF: external flow contributions at border buses
    # (bus, hour) => AffExpr to add to flow_sum in KCL
    external_injections::Union{Nothing, Dict{Tuple{Int,Int}, AffExpr}} = nothing,
    # Share of the existing renewables' energy each system may curtail
    # beyond the curtailment limit to stay under its RE growth cap
    # (re_cap_exemption)
    re_cap_exempt::Vector{Float64} = Float64[],
    # The year's curtailment limit: the day's terms are added to it with
    # the day's weight in the year (add_curtailment_limit!)
    curtailment::Union{Nothing, CurtailmentYear} = nothing,
    period_scale::Float64 = 1.0
)
    hours = size(demand, 1)
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node

    # Apply demand growth
    growth_factor = (1.0 + input.demand_growth)^(year_idx - 1)

    # =========================================================================
    # Precompute total capacity per generator/battery per bus for this year
    # These are AffExpr (may contain investment variables) passed to power_system.jl
    # =========================================================================
    total_cap_gen = Dict{Tuple{Int,Int}, Any}()
    for g in 1:n_gen
        gen = input.generators[g]
        for b in ps_vars.buses_of_gen[g]
            cap = AffExpr(0.0)
            rated_power = gen.rated_power[b]
            lifetime = gen.life_time[b]
            initial_age = gen.initial_age[b]
            age_at_year = initial_age + (year_idx - 1)
            degradation_rate = gen.degradation_rate[b]

            if age_at_year < lifetime && rated_power > 0
                deg_factor = (1.0 - degradation_rate) ^ age_at_year
                add_to_expression!(cap, rated_power * deg_factor)
            elseif rated_power > 0
                life_ext_var = vars.gen_life_extension[year_idx][g][b]
                if life_ext_var !== nothing
                    add_to_expression!(cap, life_ext_var)
                end
            end

            total_cap_gen[(g, b)] = cap
        end
    end

    total_cap_bat_power = Dict{Tuple{Int,Int}, Any}()
    total_cap_bat_energy = Dict{Tuple{Int,Int}, Any}()
    for bi in 1:n_bat
        bat = input.batteries[bi]
        for b in ps_vars.buses_of_bat[bi]
            total_cap_bat_power[(bi, b)] = battery_power_in_service(vars, bat, bi, b, year_idx)
            total_cap_bat_energy[(bi, b)] = battery_energy_in_service(vars, bat, bi, b, year_idx)
        end
    end

    # =========================================================================
    # Technology Generation Variables (per-technology investment dispatch)
    # Technologies are master-only concepts — not in power_system.jl
    # =========================================================================
    n_tech = length(input.technologies)
    n_bat_tech = length(input.battery_technologies)

    tech_output = nothing
    # Capacity of each technology in service this year (for the reserves)
    tech_caps = Dict{Tuple{Int,Int}, AffExpr}()
    # Fuel cost of technologies with an offer curve (day_operational_cost
    # charges the others their flat fuel_cost)
    tech_curve_cost = AffExpr(0.0)
    if n_tech > 0
        tech_output = @variable(model,
            [t=1:n_tech, b=1:n_buses, ts=1:hours],
            lower_bound = 0,
            base_name = "tech_out_y$(year_idx)_d$(day_idx)")

        for t in 1:n_tech
            tech = input.technologies[t]
            for b in 1:n_buses
                cumul_cap = tech_capacity_built(vars, input, t, b, year_idx)
                tech_caps[(t, b)] = cumul_cap
                avails = Vector{Float64}(undef, hours)
                for ts in 1:hours
                    # Every technology's profile (also a dispatchable one's
                    # derates and outages), as the dispatch applies it
                    avail = tech_availability_at(tech, start_hour + ts - 1, b)
                    avails[ts] = avail
                    @constraint(model,
                        tech_output[t, b, ts] <= cumul_cap * avail,
                        base_name = "tech_cap_t$(t)_b$(b)_ts$(ts)_y$(year_idx)_d$(day_idx)")
                end
                add_tech_curve_segments!(model, input, tech_output, t, b,
                    cumul_cap, avails, tech_curve_cost;
                    suffix = "_y$(year_idx)_d$(day_idx)")
            end
        end
    end

    bat_tech_discharge = nothing
    bat_tech_charge = nothing
    bat_tech_soc = nothing
    # Power and energy built and still in service this year
    bt_pow(bt, b) = bat_tech_power_built(vars, input, bt, b, year_idx)
    bt_en(bt, b) = bat_tech_energy_built(vars, input, bt, b, year_idx)
    if n_bat_tech > 0
        bat_tech_discharge, bat_tech_charge, bat_tech_soc = add_battery_tech_day!(
            model, input, bt_pow, bt_en,
            start_hour, hours; suffix = "_y$(year_idx)_d$(day_idx)")
    end

    # =========================================================================
    # Build day input NamedTuple for power_system.jl / transmission_dc.jl
    # =========================================================================
    n_nodes = input.network.num_nodes
    demand_slice = Matrix{Float64}(undef, hours, n_nodes)
    for t in 1:hours, n in 1:n_nodes
        demand_slice[t, n] = demand[t, n] * growth_factor
    end

    # The day model reads availability and inflow by its own step t = 1..hours
    day_generators = generators_for_day(input, start_hour, hours)

    day_input = (
        generators = day_generators,
        batteries = batteries_for_day(input, start_hour, hours),
        network = input.network,
        demand = demand_slice,
        temporal = (hours = hours, resolution_hours = input.temporal_resolution_hours),
        year = input.base_year + year_idx - 1,
        base_year = input.base_year,
        mode = "economic_dispatch",
        reserve_static_requirement = input.reserve_static_requirement,
        reserve_static_default_ratio = input.reserve_static_default_ratio,
        reserve_dynamic_requirement = input.reserve_dynamic_requirement,
        dynamic_reserve_contribution = input.dynamic_reserve_contribution,
        # Reserves are pooled per system (one per original system of a merged run)
        reserve_node_groups = system_node_groups(input.co2_budget_groups, n_nodes),
        inertia_limit = input.inertia_limit,
        inertia_limit_hourly = Float64[],
        rooftop_generation = nothing,
        re_penetration_target = 0.0,
        soc_end_tolerance = 0.0,
        min_cycling_ratio = 0.0,
        min_cycling_period_days = 1.0,
        pwl_loss_segments = input.transmission_loss_segments,
        gen_cost_curves = input.gen_cost_curves,
        bat_cost_curves = input.bat_cost_curves,
        # Rolling-horizon seam slots are not applicable inside the master's
        # representative-day subproblems (each day is solved fresh, with no
        # seam to a prior day). Provide empty Dicts so the shared
        # power_system.jl constraint builders fall back to their no-boundary
        # paths instead of erroring on a missing field.
        generator_output_prev = Dict{Int, Dict{Int, Float64}}(),
        reservoir_level_prev = Dict{Int, Dict{Int, Float64}}(),
    )

    # =========================================================================
    # DELEGATE to power_system.jl constraint functions
    # =========================================================================
    add_generator_constraints!(model, ps_vars, day_input;
        capacity_override=total_cap_gen)
    add_battery_constraints!(model, ps_vars, day_input;
        capacity_override_power=total_cap_bat_power,
        capacity_override_energy=total_cap_bat_energy,
        initial_soc_overrides=initial_soc_overrides,
        final_soc_targets=final_soc_targets)
    # Reservoir water balance / cyclic level — gives hydro an energy budget in
    # the master (previously omitted, so hydro was over-credited as firm MW).
    # When TSAM seasonal linking is active the cyclic closure is replaced by the
    # period's free start and end (overrides below). No-op with no reservoir.
    # The reservoirs' level bounds include the capacity built so far
    res_cap = Dict{Tuple{Int,Int}, Any}()
    for (g, gen) in enumerate(input.generators), b in 1:n_buses
        gen.reservoir_capacity[b] > 0 && gen.reservoir_invest_max[b] > 0 || continue
        res_cap[(g, b)] = reservoir_in_service(vars, gen, g, b, year_idx)
    end
    add_reservoir_constraints!(model, ps_vars, day_input;
        initial_reservoir_overrides=initial_reservoir_overrides,
        final_reservoir_targets=final_reservoir_targets,
        capacity_override=res_cap)
    # The units the master builds hold reserve as they will in the dispatch
    add_reserve_constraints!(model, ps_vars, day_input;
        capacity_override=total_cap_gen,
        battery_capacity_override=total_cap_bat_power,
        demand_scale=1.0,  # demand already scaled in demand_slice
        extra_headroom=master_unit_headroom(input, start_hour, tech_output,
            (t, b) -> get(tech_caps, (t, b), 0.0), bat_tech_discharge, bat_tech_charge,
            bt_pow))
    add_inertia_constraints!(model, ps_vars, day_input)
    # AC/DC and frequency converters as the dispatch models them, at their
    # rated power plus the converter capacity the master builds
    conv_cap = Dict{Tuple{String,Int}, AffExpr}(
        key => converter_capacity(vars, conv, key, year_idx)
        for (key, conv) in master_converters(input.network))
    add_converter_constraints!(model, ps_vars, day_input; capacity_override=conv_cap)

    # =========================================================================
    # Build extra injection function for balance (master-specific terms)
    # =========================================================================
    extra_fn = (bus, t) -> begin
        expr = AffExpr(0.0)
        # Technology generation
        if tech_output !== nothing
            for tc in 1:n_tech
                add_to_expression!(expr, tech_output[tc, bus, t])
            end
        end
        # Battery technology net discharge
        if bat_tech_discharge !== nothing
            for btc in 1:n_bat_tech
                add_to_expression!(expr, bat_tech_discharge[btc, bus, t])
                add_to_expression!(expr, -1.0, bat_tech_charge[btc, bus, t])
            end
        end
        # Inter-system external injections
        if external_injections !== nothing && haskey(external_injections, (bus, t))
            add_to_expression!(expr, external_injections[(bus, t)])
        end
        return expr
    end

    # =========================================================================
    # DELEGATE to transmission_dc.jl / power_system.jl for power balance
    # =========================================================================
    # Use network.transmission_lines (populated by convert_network_config from YAML)
    # NOT input.transmission_lines (MasterProblemInput field, may be empty)
    network_lines = input.network.transmission_lines
    n_lines = length(network_lines)
    line_flow_cost = AffExpr(0.0)
    if n_buses > 1 && n_lines > 0
        transmission = with_loss_model(TransmissionDC(input.network),
            input.transmission_loss_segments, input.network.s_base)
        # Line capacity: base plus the MW built on its corridor so far
        # (transmission.lines includes transformers)
        line_cap_override = Dict{Int, Any}()
        expandable = Set{Int}()
        expansion = Dict{Int, Float64}()
        for (l, (from, to)) in enumerate(transmission.lines)
            cap = AffExpr(transmission.line_capacities[l])
            built = transfer_capacity_built(vars, from, to, year_idx)
            # A corridor whose investment limit is 0 is not expandable
            extra = transfer_expansion_max(vars, from, to, year_idx)
            if built !== nothing && extra > 0
                push!(expandable, l)
                expansion[l] = extra
                add_to_expression!(cap, built)
            end
            line_cap_override[l] = cap
        end
        add_dc_constraints!(model, transmission, ps_vars, day_input;
            extra_injections_fn=extra_fn, expandable_lines=expandable,
            expansion_max=expansion)
        add_line_capacity_constraints!(model, transmission, ps_vars, day_input;
            capacity_override=line_cap_override)
        line_flow_cost = add_line_flow_cost!(model, transmission, ps_vars, input, hours)
    else
        add_demand_constraints!(model, ps_vars, day_input;
            extra_injections_fn=extra_fn)
    end

    # =========================================================================
    # DELEGATE curtailment to power_system.jl
    # =========================================================================
    add_curtailment_constraints!(model, ps_vars, day_input;
        capacity_override=total_cap_gen)

    # =========================================================================
    # Sectoral split of the load shed (M2) — master-specific
    # =========================================================================
    remainder_premium = add_day_sectoral_shed_constraints!(model, ps_vars, input,
        demand_slice, start_hour, growth_factor; label = "y$(year_idx)_d$(day_idx)")

    # The RE target is annual (add_re_target_constraints!): the caller adds
    # the day's system_day_renewable, system_day_generation and
    # system_day_demand to the year's (add_day_re!).

    # =========================================================================
    # Curtailment limit — master-specific, annual: the day's curtailment and
    # renewable output per system go to the year's (add_curtailment_limit!)
    # =========================================================================
    # Accumulator for tech-investment curtailment cost — added to the master
    # objective so the LP trades tech overbuild against storage capex on an
    # explicit economic basis (the max_curtailment_ratio constraint alone
    # empirically does not bind for the full-year operational LP).
    tech_day_cost = AffExpr(0.0)
    # Unclassified sectoral remainder above plain VOLL (as the dispatch prices it)
    add_to_expression!(tech_day_cost, remainder_premium)

    if input.max_curtailment_ratio < 1.0
        # The candidates' curtailment (variable renewables only, as the
        # dispatch's), cap × avail − output, pays curtailment_cost
        if tech_output !== nothing && input.curtailment_cost > 0
            for (tc, tech) in enumerate(input.technologies)
                is_variable_renewable(tech, 1) || continue
                for b in 1:n_buses, ts in 1:hours
                    avail = tech_availability_at(tech, start_hour + ts - 1, b)
                    add_to_expression!(tech_day_cost,
                        tech_caps[(tc, b)] * (avail * input.curtailment_cost))
                    add_to_expression!(tech_day_cost,
                        -input.curtailment_cost, tech_output[tc, b, ts])
                end
            end
        end
        curtailment === nothing ||
            add_period_curtailment!(curtailment,
                system_day_curtailment(ps_vars, tech_output, (tc, b) -> tech_caps[(tc, b)],
                                       input, start_hour, hours),
                system_day_renewable(ps_vars, tech_output, input, hours),
                re_cap_exempt_energy(input, re_cap_exempt, year_idx, start_hour, hours),
                period_scale)
    end

    add_to_expression!(tech_day_cost, tech_curve_cost)
    add_to_expression!(tech_day_cost,
        battery_tech_operating_cost(input, bat_tech_charge, bat_tech_discharge, hours))
    # Converter variable cost, flow cost of lines with a per-MW·km cost and
    # (LP-relaxed) start-ups, as the dispatch charges them
    converter_cost = add_converter_objective_terms(ps_vars, day_input)
    startup_cost = add_day_commitment!(model, ps_vars, input, total_cap_gen, year_idx, hours)
    add_to_expression!(tech_day_cost, converter_cost)
    add_to_expression!(tech_day_cost, line_flow_cost)
    add_to_expression!(tech_day_cost, startup_cost)

    # The money of those (day_operating_spend): no curtailment penalty, no
    # sectoral shed premium, no battery degradation
    tech_spend = AffExpr(0.0)
    for c in (tech_curve_cost, converter_cost, line_flow_cost, startup_cost,
              battery_tech_operating_cost(input, bat_tech_charge, bat_tech_discharge, hours;
                                          degradation = false))
        add_to_expression!(tech_spend, c)
    end

    # The caller scales tech_day_cost with the day cost (calculate_day_operational_cost)
    return tech_output, tech_day_cost, tech_spend
end

"""
    add_line_flow_cost!(model, transmission, ps_vars, input, hours) -> AffExpr

Unscaled cost of the flow on the lines that charge one per MWh (their
`flow_cost`, e.g. distance × cost per MW·km of an inter-system link):
`flow_cost × |flow|` per line and step, with `|flow|` an auxiliary variable
above `±flow` (the objective keeps it tight). Each line pays its own cost,
as in the dispatch (`add_line_flow_costs!`); transformers pay none.
"""
function add_line_flow_cost!(model::Model, transmission::TransmissionDC,
                             ps_vars::PowerSystemVariables,
                             input::MasterProblemInput, hours::Int)
    cost = AffExpr(0.0)
    for (l, c) in enumerate(line_flow_costs(transmission, input.network))
        c > 0 || continue
        for t in 1:hours
            pf = ps_vars.power_flow_by_line[l][t]
            a = @variable(model, lower_bound = 0)
            @constraint(model, a >= pf)
            @constraint(model, a >= -pf)
            add_to_expression!(cost, c, a)
        end
    end
    return cost
end

"""
    line_flow_costs(transmission, network) -> Vector{Float64}

`flow_cost` of each branch of `transmission`, in its order: the network's
transmission lines (self-loops dropped, as `TransmissionDC` does), then the
transformers at 0.
"""
function line_flow_costs(transmission::TransmissionDC, network::NetworkConfig)
    costs = zeros(length(transmission.lines))
    isempty(network.transmission_lines) && return costs
    l = 0
    for tl in network.transmission_lines
        tl.from_node == tl.to_node && continue
        l += 1
        l <= length(costs) || break
        costs[l] = tl.flow_cost
    end
    return costs
end

"""
    add_day_commitment!(model, ps_vars, input, total_cap_gen, year_idx, hours) -> AffExpr

LP-relaxed unit commitment of a representative day, when the dispatch
commits units (`input.unit_commitment`). Each thermal (or reservoir) unit
with a start-up cost or a minimum power gets an online capacity `u[t]`
(MW, at most the unit's capacity this year):

    gen_output[t] ≤ u[t],   gen_output[t] ≥ min_power × u[t],
    startup[t] ≥ u[t] − u[t−1]   (cyclic over the day)

A start of the whole unit (startup = its rated power) costs `start_up_cost`,
so a MW started costs `start_up_cost / rated`, with `rated` the unit's
degraded rated power this year as the dispatch sees it. Minimum up and down
times are not modelled.

Returns the start-up cost divided by the step length: the caller scales
the day cost by the step length and the day's weight, and a start-up is an
event, not energy. Each unit's share is kept in
`model.ext[:master_startup][ps_vars][(g, b)]`, for its NPV.
"""
function add_day_commitment!(model::Model, ps_vars::PowerSystemVariables,
                             input::MasterProblemInput, total_cap_gen::AbstractDict,
                             year_idx::Int, hours::Int)
    cost = AffExpr(0.0)
    input.unit_commitment || return cost
    tres = max(1.0, input.temporal_resolution_hours)
    for (g, gen) in enumerate(input.generators)
        # Committed units only, as in the dispatch (no variable renewable)
        for b in committed_buses(gen, ps_vars.buses_of_gen[g])
            suc = gen.start_up_cost[b]
            pmin = gen.min_power[b]
            rated = gen.rated_power[b] *
                (1.0 - gen.degradation_rate[b])^(gen.initial_age[b] + year_idx - 1)
            (rated > 0 && (suc > 0 || pmin > 0)) || continue
            cap = total_cap_gen[(g, b)]
            u = @variable(model, [1:hours], lower_bound = 0)
            su = @variable(model, [1:hours], lower_bound = 0)
            unit_cost = AffExpr(0.0)
            for t in 1:hours
                out = ps_vars.gen_output[g, b, t]
                @constraint(model, u[t] <= cap)
                @constraint(model, out <= u[t])
                pmin > 0 && @constraint(model, out >= pmin * u[t])
                @constraint(model, su[t] >= u[t] - u[t == 1 ? hours : t - 1])
                if suc > 0
                    add_to_expression!(cost, suc / rated / tres, su[t])
                    add_to_expression!(unit_cost, suc / rated / tres, su[t])
                end
            end
            if suc > 0
                by_day = get!(() -> IdDict{Any, Dict{Tuple{Int,Int}, AffExpr}}(),
                              model.ext, :master_startup)
                get!(() -> Dict{Tuple{Int,Int}, AffExpr}(), by_day, ps_vars)[(g, b)] = unit_cost
            end
        end
    end
    return cost
end

"""
    battery_tech_operating_cost(input, charge, discharge, hours; degradation=true) -> AffExpr

Unscaled operating cost of the candidate battery technologies over `hours`
steps, as the dispatch charges the batteries they become: `maintenance_cost`
on charge and discharge, `throughput_degradation_cost` on discharge (left
out with `degradation = false`).
`charge`/`discharge` are indexed [technology, bus, step]; `nothing` when the
system has no battery technology.
"""
function battery_tech_operating_cost(input::MasterProblemInput, charge, discharge, hours::Int;
                                     degradation::Bool = true)
    cost = AffExpr(0.0)
    (charge === nothing || discharge === nothing) && return cost
    for (bt, btech) in enumerate(input.battery_technologies), b in 1:input.network.num_buses
        mc = btech.maintenance_cost[b]
        dc = degradation ? btech.throughput_degradation_cost[b] : 0.0
        (mc != 0 || dc != 0) || continue
        for ts in 1:hours
            add_to_expression!(cost, mc, charge[bt, b, ts])
            add_to_expression!(cost, mc + dc, discharge[bt, b, ts])
        end
    end
    return cost
end

"""
    add_day_sectoral_shed_constraints!(model, ps_vars, input, demand_slice,
                                       start_hour, growth_factor; label=nothing)

Split each bus's load shed of a representative day into per-sector shed plus
an unclassified remainder, as the dispatch does (SEC-1, SEC-2):

- each sector sheds at most its own demand at the bus
  (`sectoral_demand[t, parent node] × demand_fraction × growth`);
- `load_shed − Σ sector shed` lies in [0, bus demand not covered by any sector].

The sectoral shed is part of `load_shed`, not an extra injection: the bus can
shed at most its demand (PB-1b). `day_operational_cost` charges `load_shed` at
VOLL and adds the criticality premium on the sectoral part. `label` names the
constraints; `nothing` leaves them unnamed (Benders subproblems).

Returns the unscaled premium of the unclassified remainder above VOLL
(`loss_of_load_penalty`): `VOLL × (m − 1) × (load_shed − Σ sector shed)`, with `m` the remainder's
multiplier (`sector_remainder_multiplier`, the dispatch's rule); the caller
adds it to the day's cost.
"""
function add_day_sectoral_shed_constraints!(model, ps_vars, input, demand_slice,
                                            start_hour::Int, growth_factor::Float64;
                                            label::Union{Nothing, String} = nothing)
    voll = input.loss_of_load_penalty
    premium = AffExpr(0.0)
    lol = ps_vars.loss_of_load_sectoral
    (lol === nothing || isempty(lol)) && return premium
    hours = size(demand_slice, 1)
    for t in 1:hours, b in 1:input.network.num_buses
        parent = input.network.bus_to_node[b]
        frac = input.network.buses[b].demand_fraction
        sector_shed = AffExpr(0.0)
        sector_demand = 0.0
        sec_vals = Dict{String, Float64}()
        for (sector, lol_vars) in lol
            sec_val = 0.0
            sec_dem = get(input.sectoral_demand, sector, nothing)
            if sec_dem !== nothing && size(sec_dem, 1) > 0 && parent <= size(sec_dem, 2)
                hour_in_year = mod1(start_hour + t - 1, size(sec_dem, 1))
                sec_val = max(0.0, sec_dem[hour_in_year, parent] * frac * growth_factor)
            end
            sec_vals[sector] = sec_val
            sector_demand += sec_val
            add_to_expression!(sector_shed, lol_vars[b, t])
            @constraint(model, lol_vars[b, t] <= sec_val,
                base_name = label === nothing ? "" : "lol_sec_ub_$(sector)_$(label)_n$(b)_t$(t)")
        end
        residual_cap = max(0.0, demand_slice[t, parent] * frac - sector_demand)
        @constraint(model, ps_vars.load_shed[b, t] - sector_shed >= 0.0,
            base_name = label === nothing ? "" : "lol_sec_sum_$(label)_n$(b)_t$(t)")
        @constraint(model, ps_vars.load_shed[b, t] - sector_shed <= residual_cap,
            base_name = label === nothing ? "" : "lol_sec_residual_$(label)_n$(b)_t$(t)")
        m = sector_remainder_multiplier(keys(lol), input.sectoral_criticality,
                                        s -> sec_vals[s])
        if m != 1.0
            add_to_expression!(premium, voll * (m - 1.0), ps_vars.load_shed[b, t])
            add_to_expression!(premium, -voll * (m - 1.0), sector_shed)
        end
    end
    return premium
end

"""
    co2_cost_per_mwh(input, unit, b)

Carbon cost of one MWh produced by `unit` (generator or technology) at bus `b`
(\$/t × t/MWh_e, see `co2_per_mwh`), as the dispatch charges it.
"""
co2_cost_per_mwh(input::MasterProblemInput, unit, b::Int) =
    input.co2_cost * co2_per_mwh(input.fuel_co2, unit, b)

"""
    tech_curve_segments(input, tc, b)

Offer-curve segments of candidate technology `tc` at bus `b`, or `nothing`
when it has no curve there and is priced at its flat `fuel_cost`, the same
rule the generators follow. A single segment (a flat curve) prices the
output at its own marginal cost.
"""
function tech_curve_segments(input::MasterProblemInput, tc::Int, b::Int)
    bus_curves = get(input.tech_cost_curves, tc, nothing)
    bus_curves === nothing && return nothing
    segs = get(bus_curves, b, nothing)
    return (segs === nothing || isempty(segs)) ? nothing : segs
end

"""
    add_tech_curve_segments!(model, input, tech_output, tc, b, cap, avails, cost;
                             suffix="", store=nothing)

Price candidate technology `tc`'s output at bus `b` with its offer curve, as
the dispatch prices the generator it becomes: the output is split into one
variable per segment, segment k is capped at `fraction_k × cap × avails[ts]`
and its marginal cost is added to `cost`. `cap` is the cumulative capacity
(an expression in the master, a number in a Benders subproblem). With
`store`, each segment cap is recorded as `(cref, fraction_k × avail, tc, b, ts)`
under `tech_seg_cap_t<tc>_b<b>_k<k>_ts<ts>` for the Benders cut gradient.
No-op without a curve.
"""
function add_tech_curve_segments!(model::Model, input::MasterProblemInput,
                                  tech_output, tc::Int, b::Int, cap, avails::Vector{Float64},
                                  cost::AffExpr; suffix::String = "", store = nothing)
    segs = tech_curve_segments(input, tc, b)
    segs === nothing && return nothing
    n_seg = length(segs)
    sv = @variable(model, [k=1:n_seg, ts=1:length(avails)], lower_bound = 0,
                   base_name = "tech_seg_t$(tc)_b$(b)$(suffix)")
    for ts in eachindex(avails)
        @constraint(model, tech_output[tc, b, ts] == sum(sv[k, ts] for k in 1:n_seg))
        for k in 1:n_seg
            cname = "tech_seg_cap_t$(tc)_b$(b)_k$(k)_ts$(ts)"
            coef = segs[k].fraction * avails[ts]
            cref = @constraint(model, sv[k, ts] <= coef * cap,
                               base_name = cname * suffix)
            store === nothing || (store[cname] = (cref, coef, tc, b, ts))
            add_to_expression!(cost, segs[k].marginal_cost, sv[k, ts])
        end
    end
    return sv
end

"""
    day_operational_cost(ps_vars, input, hours; tech_output=nothing)

Unscaled operating cost of one representative day or TSAM period: fuel,
maintenance and CO2, load shedding, curtailment, battery maintenance (on
charge and discharge) and throughput, and the reserve and inertia
shortfalls. The callers scale it to a year. Fixed O&M is not an operating
cost: it is charged per year on the capacity in service
(`build_annual_asset_cost`).
"""
function day_operational_cost(
    ps_vars::PowerSystemVariables,
    input::MasterProblemInput,
    hours::Int;
    tech_output::Union{Nothing, Any} = nothing
)::AffExpr
    n_gen = length(input.generators)
    n_buses = input.network.num_buses
    n_nodes = input.network.num_nodes

    cost = AffExpr(0.0)

    for t in 1:hours
        for g in 1:n_gen
            for n in ps_vars.buses_of_gen[g]
                add_gen_step_cost!(cost, ps_vars, input, g, n, t)
            end
        end

        if tech_output !== nothing
            n_tech = length(input.technologies)
            for tc in 1:n_tech
                tech = input.technologies[tc]
                for b in 1:n_buses
                    co2 = co2_cost_per_mwh(input, tech, b)
                    # With an offer curve the fuel is priced per segment
                    # (add_tech_curve_segments!, in tech_day_cost)
                    fuel = tech_curve_segments(input, tc, b) === nothing ? tech.fuel_cost[b] : 0.0
                    total_cost = fuel + tech.maintenance_cost[b] + co2
                    if total_cost > 0
                        add_to_expression!(cost, tech_output[tc, b, t], total_cost)
                    end
                end
            end
        end

        # load_shed is per-bus (B2 refactor) — iterate over buses.
        for b in 1:n_buses
            add_to_expression!(cost, ps_vars.load_shed[b, t], input.loss_of_load_penalty)
        end

        # RE-target shortfall: the Benders subproblems' slack (the monolithic
        # master's is annual, add_re_target_constraints!; there it stays 0)
        for n in 1:n_nodes
            add_to_expression!(cost, ps_vars.fre_penetration_loss[n, t],
                re_shortfall_penalty(input))
        end

        # Curtailment penalty (existing renewable generators).
        # Tech-investment curtailment is penalised separately in
        # add_day_operational_constraints! where the tech_output expression
        # and per-year cumul_cap are accessible. Without this term the master
        # has no incentive to size tech to actual absorption capacity —
        # excess renewable is spilled "for free", so master overbuilds and
        # operational reveals the gap (curtailment 40-65% from year 2042 on
        # despite the configured max_curtailment_ratio = 5%).
        for n in 1:n_nodes
            add_to_expression!(cost, ps_vars.curtailment[n, t],
                input.curtailment_cost)
        end

        # Sectoral shed is part of load_shed (add_day_sectoral_shed_constraints!),
        # which is already charged VOLL above: add the criticality premium
        # VOLL × (κ − 1), so a sector's MWh costs VOLL × κ. The unclassified
        # remainder's premium (weighted mean criticality) comes from
        # add_day_sectoral_shed_constraints!.
        if ps_vars.loss_of_load_sectoral !== nothing && !isempty(ps_vars.loss_of_load_sectoral)
            for (sector, lol_vars) in ps_vars.loss_of_load_sectoral
                criticality = get(input.sectoral_criticality, sector, 1.0)
                sector_premium = input.loss_of_load_penalty * (criticality - 1.0)
                for n in 1:n_buses
                    add_to_expression!(cost, lol_vars[n, t], sector_premium)
                end
            end
        end
    end

    n_bat = length(input.batteries)
    for t in 1:hours
        for b in 1:n_bat
            for n in ps_vars.buses_of_bat[b]
                add_bat_step_cost!(cost, ps_vars, input, b, n, t)
            end
        end
    end

    for t in 1:hours
        for n in 1:n_nodes
            add_to_expression!(cost, ps_vars.reserve_static_loss[n, t],
                input.loss_of_reserve_static)
            add_to_expression!(cost, ps_vars.reserve_dynamic_loss[n, t],
                input.loss_of_reserve_dynamic)
        end
    end

    if ps_vars.loss_of_inertia !== nothing
        for t in 1:hours
            add_to_expression!(cost, ps_vars.loss_of_inertia[t],
                input.loss_of_inertia_penalty)
        end
    end

    return cost
end

"""
    add_gen_step_cost!(cost, ps_vars, input, g, n, t)

Add existing generator `g`'s operating cost at bus `n` in step `t` (per
step, not × Δt) to `cost`: its fuel (`fuel_cost`, or its offer curve's
segments), maintenance and CO2 price per MWh (no CO2 price with
`co2_price = false`). `day_operational_cost` and the
unit's NPV (`add_unit_day_operation!`) both use it.
"""
function add_gen_step_cost!(cost::AffExpr, ps_vars::PowerSystemVariables,
                            input::MasterProblemInput, g::Int, n::Int, t::Int;
                            co2_price::Bool = true)
    gen = input.generators[g]
    co2 = co2_price ? co2_cost_per_mwh(input, gen, n) : 0.0
    maint_cost = gen.maintenance_cost[n]
    if haskey(ps_vars.gen_seg_output, (g, n))
        # PWL fuel cost via segments
        segs = input.gen_cost_curves[g][n]
        seg_vars = ps_vars.gen_seg_output[(g, n)]
        for k in 1:length(segs)
            add_to_expression!(cost, segs[k].marginal_cost, seg_vars[k, t])
        end
        add_to_expression!(cost, ps_vars.gen_output[g, n, t],
                          maint_cost + co2)
    else
        fuel_cost = gen.fuel_cost[n]
        add_to_expression!(cost, ps_vars.gen_output[g, n, t],
                          fuel_cost + maint_cost + co2)
    end
    return cost
end

"""
    add_bat_step_cost!(cost, ps_vars, input, b, n, t)

Add existing battery `b`'s operating cost at bus `n` in step `t` (per step)
to `cost`: maintenance per MWh charged and discharged, as in the dispatch,
and its throughput cost (or offer curve) on the discharge.
"""
function add_bat_step_cost!(cost::AffExpr, ps_vars::PowerSystemVariables,
                            input::MasterProblemInput, b::Int, n::Int, t::Int)
    bat = input.batteries[b]
    mc = bat.maintenance_cost[n]
    if mc != 0
        add_to_expression!(cost, mc, ps_vars.bat_charge[b, n, t])
        add_to_expression!(cost, mc, ps_vars.bat_discharge[b, n, t])
    end
    if haskey(ps_vars.bat_seg_discharge, (b, n))
        segs = input.bat_cost_curves[b][n]
        seg_vars = ps_vars.bat_seg_discharge[(b, n)]
        for k in 1:length(segs)
            add_to_expression!(cost, segs[k].marginal_cost, seg_vars[k, t])
        end
    else
        add_to_expression!(cost, ps_vars.bat_discharge[b, n, t],
                          bat.throughput_degradation_cost[n])
    end
    return cost
end

"""
    day_operating_spend(ps_vars, input, hours; tech_output=nothing, tech_spend=nothing) -> AffExpr

The money a representative day (or TSAM period) spends, unscaled like
`day_operational_cost`, for the `budget_scope: total` budget: the existing
generators' fuel (or offer-curve segments) and maintenance, the candidate
technologies' fuel and maintenance, the existing batteries' maintenance, and
`tech_spend` (`add_day_operational_constraints!`: offer curves of
technologies, battery-technology maintenance, converter, line-flow and
start-up costs), the categories of the dispatch's operating budget
(`OPERATING_BUDGET_COSTS`). No modelling penalty (unserved energy, reserve,
inertia, curtailment, RE shortfall), no battery degradation and no CO2 price
(a transfer to the state, not a cost of operation).
"""
function day_operating_spend(ps_vars::PowerSystemVariables, input::MasterProblemInput,
                             hours::Int; tech_output = nothing, tech_spend = nothing)
    spend = AffExpr(0.0)
    n_buses = input.network.num_buses
    for t in 1:hours
        for g in eachindex(input.generators), n in ps_vars.buses_of_gen[g]
            add_gen_step_cost!(spend, ps_vars, input, g, n, t; co2_price = false)
        end
        if tech_output !== nothing
            for (tc, tech) in enumerate(input.technologies), b in 1:n_buses
                fuel = tech_curve_segments(input, tc, b) === nothing ? tech.fuel_cost[b] : 0.0
                c = fuel + tech.maintenance_cost[b]
                c != 0 && add_to_expression!(spend, c, tech_output[tc, b, t])
            end
        end
        for b in eachindex(input.batteries), n in ps_vars.buses_of_bat[b]
            mc = input.batteries[b].maintenance_cost[n]
            mc != 0 || continue
            add_to_expression!(spend, mc, ps_vars.bat_charge[b, n, t])
            add_to_expression!(spend, mc, ps_vars.bat_discharge[b, n, t])
        end
    end
    tech_spend === nothing || add_to_expression!(spend, tech_spend)
    return spend
end

"""
    master_fuel_day_spend(model, input, fuel_day) -> AffExpr

The money of a day's fuel block (`add_master_fuel_day!`, whose cost is
`fuel_day`): the fuel bought and transported and the electrolyzers' variable
and water cost, less the coupled units' flat `fuel_cost` (which
`day_operating_spend` counts and the fuel balance replaces). The fuel
shortfall, excess and unmet non-electric demand penalties are left out.
"""
function master_fuel_day_spend(model::Model, input::MasterProblemInput, fuel_day::AffExpr)
    spend = AffExpr(0.0)
    has_master_fuel(input) || return spend
    add_to_expression!(spend, fuel_day)
    terms = model.ext[:master_fuel_last_day][2]
    for k in (:pe_loss_cost, :pe_excess_cost)
        add_to_expression!(spend, -1.0, terms[k])
    end
    return spend
end

"""
    calculate_day_operational_cost(ps_vars, input, year_idx, hours;
                                   tech_output=nothing, tech_day_cost=nothing, ndays)

Calculate operational cost for a representative day, scaled to the year
(`rep_day_scaling` with `ndays` days selected in the year).
`tech_day_cost` (from `add_day_operational_constraints!`) is scaled with it.
"""
function calculate_day_operational_cost(
    ps_vars::PowerSystemVariables,
    input::MasterProblemInput,
    year_idx::Int,
    hours::Int;
    tech_output::Union{Nothing, Any} = nothing,
    tech_day_cost::Union{Nothing, AffExpr} = nothing,
    ndays::Integer = input.representative_days_per_year
)::AffExpr
    scaling_factor = rep_day_scaling(input, ndays)

    cost = day_operational_cost(ps_vars, input, hours; tech_output)
    tech_day_cost === nothing || add_to_expression!(cost, tech_day_cost)
    return cost * scaling_factor
end

"""
    rep_day_scaling(input, ndays=input.representative_days_per_year)

Factor from one representative day's per-step quantities to the year: the
step length times the days each representative day stands for, the year's
days over `ndays`, the days actually selected for it. Fewer than
`representative_days_per_year` are selected when the separation or the data
leave no more; dividing by the setting then underweighted every cost and
energy of the year.
"""
function rep_day_scaling(input::MasterProblemInput,
                         ndays::Integer = input.representative_days_per_year)
    tres = max(1.0, input.temporal_resolution_hours)
    days_in_year = length(input.hours_per_year) > 0 ? input.hours_per_year[1] / 24.0 : 365.0
    return tres * days_in_year / max(ndays, 1)
end

"""
    warn_fewer_rep_days(input, ndays)

Warn when only `ndays` representative days could be selected, fewer than
`representative_days_per_year` (the minimum separation or the data leave
no more); each then stands for more days of the year (`rep_day_scaling`).
"""
function warn_fewer_rep_days(input::MasterProblemInput, ndays::Integer)
    ndays < input.representative_days_per_year || return nothing
    @warn "Only $(ndays) of the $(input.representative_days_per_year) representative " *
          "days requested could be selected (min_day_separation = " *
          "$(input.min_day_separation)); each stands for 1/$(ndays) of the year"
    return nothing
end

"""
    day_co2_by_group(ps_vars, tech_output, input, hours) -> Vector{AffExpr}

CO2 of a representative day or TSAM period per budget group of
`input.co2_budget_groups`: Σ over steps of output (MW) × `co2_per_mwh`
(t/MWh) of the generators and candidate technologies at the group's nodes.
Per step: the caller scales it by the step length and the day's weight.
"""
function day_co2_by_group(ps_vars::PowerSystemVariables, tech_output,
                          input::MasterProblemInput, hours::Int)
    b2n = input.network.bus_to_node
    out = [AffExpr(0.0) for _ in input.co2_budget_groups]
    for (k, grp) in enumerate(input.co2_budget_groups)
        has_co2_budget(grp) || continue
        in_group = Set(grp.nodes)
        for (g, gen) in enumerate(input.generators), b in ps_vars.buses_of_gen[g]
            b2n[b] in in_group || continue
            f = co2_per_mwh(input.fuel_co2, gen, b)
            f == 0.0 && continue
            for t in 1:hours
                add_to_expression!(out[k], f, ps_vars.gen_output[g, b, t])
            end
        end
        tech_output === nothing && continue
        for (tc, tech) in enumerate(input.technologies), b in 1:input.network.num_buses
            b2n[b] in in_group || continue
            f = co2_per_mwh(input.fuel_co2, tech, b)
            f == 0.0 && continue
            for t in 1:hours
                add_to_expression!(out[k], f, tech_output[tc, b, t])
            end
        end
    end
    return out
end

"""
    add_co2_budget_constraints!(model, vars, input, y_idx, annual_co2; scenario_idx=0) -> AffExpr

One annual CO2 budget constraint per budget group (one per system), as in
the dispatch: the year's emissions `annual_co2[k]` (t) stay within the
group's budget plus a violation slack. Returns the cost of the slacks at
each group's own violation penalty, to add to the year's operating cost.
"""
function add_co2_budget_constraints!(model::Model, vars::MasterProblemVariables,
                                     input::MasterProblemInput, y_idx::Int,
                                     annual_co2::Vector{AffExpr}; scenario_idx::Int = 0)
    cost = AffExpr(0.0)
    for (k, grp) in enumerate(input.co2_budget_groups)
        has_co2_budget(grp) || continue
        tag = scenario_idx > 0 ? "_s$(scenario_idx)" : ""
        viol = @variable(model, lower_bound = 0,
            base_name = "co2_budget_violation$(tag)_y$(y_idx)_$(grp.name)")
        vars.co2_budget_violation[(scenario_idx, y_idx, k)] = viol
        @constraint(model, annual_co2[k] <= grp.budget + viol,
            base_name = "co2_budget$(tag)_y$(y_idx)_$(grp.name)")
        add_to_expression!(cost, grp.violation_penalty, viol)
    end
    return cost
end

"""
    add_representative_days_validation!(model, vars, input, targets)

Add operational validation through representative days.
This is CRITICAL for linking investment decisions to actual generation feasibility.

Every year models the same days of the year (`master_day_plan`): the
representative days, then the critical days (lowest renewable supply
against demand), each scaled to the year by the days it stands for.
"""
function add_representative_days_validation!(
    model::Model,
    vars::MasterProblemVariables,
    base_input::MasterProblemInput,
    targets::Dict{Tuple{Int,Int}, Float64};
    scenario::Union{Nothing, Scenario} = nothing,
    scenario_idx::Int = 0
)
    num_years = length(base_input.years)
    timesteps_per_day = 24 ÷ base_input.temporal_resolution_hours
    day_offset = 1000 * scenario_idx   # keeps day names distinct per scenario
    # The same days of the year for every year (and every scenario): the
    # representative days, then the critical days, with the days of the
    # year each stands for
    offsets, day_weights, n_rep = master_day_plan(base_input)
    tres = max(1.0, base_input.temporal_resolution_hours)
    # Least RE share the existing renewables leave each system the year
    # before (re_cap_exemption)
    prev_floor = nothing

    for y_idx in 1:num_years
        # The year's fuel prices; a stochastic scenario operates on its own
        # demand and costs
        input = year_master_input(base_input, y_idx)
        scenario === nothing ||
            (input = scenario_master_input(input, scenario.multipliers, y_idx))

        rep_days = master_rep_days(input, y_idx; offsets)

        if isempty(rep_days)
            @warn "No representative days selected for year $(input.years[y_idx])"
            continue
        end
        # Each day stands for `day_weights` days of the year
        y_idx == 1 && scenario_idx <= 1 && warn_fewer_rep_days(input, n_rep)
        periods = [(h, min(h + timesteps_per_day - 1, size(input.base_demand, 1)) - h + 1,
                    tres * day_weights[k]) for (k, h) in enumerate(rep_days)]
        cap_exempt, prev_floor = re_cap_exemption(input, targets, y_idx, periods, prev_floor)

        vars.operational_costs[y_idx] = AffExpr[]
        day_spends = AffExpr[]   # the days' money, for budget_scope: total
        annual_co2 = [AffExpr(0.0) for _ in input.co2_budget_groups]
        # Each system's renewable and total generation and demand, scaled
        # to the year
        re_energy = [AffExpr(0.0) for _ in master_systems(input)]
        re_generation = [AffExpr(0.0) for _ in re_energy]
        re_demand = zeros(length(re_energy))
        curt_year = CurtailmentYear()

        for (day_idx, start_hour) in enumerate(rep_days)
            end_hour = min(start_hour + timesteps_per_day - 1, size(input.base_demand, 1))
            hours = end_hour - start_hour + 1

            if hours < 1
                continue
            end

            day_demand = input.base_demand[start_hour:end_hour, :]
            # Step length × the days of the year this day stands for
            weight = day_weights[day_idx]
            scale = tres * weight

            # Create PowerSystemVariables for this day
            ps_vars = create_day_ps_vars!(model, input, y_idx, day_idx + day_offset, hours)

            # Add operational constraints via delegation to power_system.jl
            tech_output, tech_day_cost, tech_spend = add_day_operational_constraints!(
                model, ps_vars, vars, input, y_idx, day_idx + day_offset, day_demand, start_hour;
                re_cap_exempt = cap_exempt, curtailment = curt_year,
                period_scale = scale
            )

            # Operational cost scaled to the year (candidate technologies' too)
            day_cost = calculate_day_operational_cost_tsam(ps_vars, input, y_idx, hours, weight;
                tech_output=tech_output, tech_day_cost=tech_day_cost)
            # M2 (#89): fuel balance, fuel infrastructure and electrolyzers
            fuel_day = add_master_fuel_day!(model, ps_vars, input, y_idx,
                start_hour, hours, tech_output;
                extra = k -> master_fuel_capacity(vars, input, y_idx, k))
            add_to_expression!(day_cost, fuel_day, weight)
            push!(vars.operational_costs[y_idx], day_cost)
            input.budget_total && push!(day_spends,
                year_day_spend(model, ps_vars, input, hours, scale, weight,
                               tech_output, tech_spend, fuel_day))
            record_master_day!(model, scenario_idx, y_idx, ps_vars, input, hours, scale)

            # The day's CO2, renewable output and demand, scaled to the
            # year like its cost
            for (k, e) in enumerate(day_co2_by_group(ps_vars, tech_output, input, hours))
                add_to_expression!(annual_co2[k], e, scale)
            end
            add_day_re!(re_energy, re_generation, re_demand, ps_vars, tech_output, input, y_idx,
                        start_hour, hours, scale)
        end

        input.budget_total && record_year_operating_cost!(model, scenario_idx, y_idx, day_spends)
        push!(vars.operational_costs[y_idx],
            add_curtailment_limit!(model, input, curt_year, y_idx; scenario_idx))
        push!(vars.operational_costs[y_idx],
            add_co2_budget_constraints!(model, vars, input, y_idx, annual_co2;
                scenario_idx=scenario_idx))
        push!(vars.operational_costs[y_idx],
            add_re_target_constraints!(model, vars, input, y_idx, targets,
                re_energy, re_generation, re_demand; scenario_idx=scenario_idx))
    end
end

"""
    add_day_re!(re_energy, re_generation, re_demand, ps_vars, tech_output, input,
                y_idx, start_hour, hours, scale)

Add a representative day's (or TSAM period's) renewable generation
(`system_day_renewable`), total generation (`system_day_generation`) and
demand (`system_day_demand`) per system, × `scale` (step length × the days
it stands for), to the year's.
"""
function add_day_re!(re_energy::Vector{AffExpr}, re_generation::Vector{AffExpr},
                     re_demand::Vector{Float64},
                     ps_vars::PowerSystemVariables, tech_output,
                     input::MasterProblemInput, y_idx::Int, start_hour::Int,
                     hours::Int, scale::Float64)
    for (s, e) in enumerate(system_day_renewable(ps_vars, tech_output, input, hours))
        add_to_expression!(re_energy[s], e, scale)
    end
    for (s, e) in enumerate(system_day_generation(ps_vars, tech_output, input, hours))
        add_to_expression!(re_generation[s], e, scale)
    end
    re_demand .+= scale .* system_day_demand(input, y_idx, start_hour, hours)
    return nothing
end

# =============================================================================
# TSAM (Time-Series Aggregation Method)
# =============================================================================

"""
    calculate_day_operational_cost_tsam(day_vars, input, year_idx, hours, weight;
                                        tech_output=nothing, tech_day_cost=nothing)

Calculate operational cost for a TSAM representative period with period weight.
Uses weight-based scaling instead of 365/N uniform scaling; `tech_day_cost`
is scaled with it.
"""
function calculate_day_operational_cost_tsam(
    ps_vars::PowerSystemVariables,
    input::MasterProblemInput,
    year_idx::Int,
    hours::Int,
    weight::Float64;
    tech_output::Union{Nothing, Any} = nothing,
    tech_day_cost::Union{Nothing, AffExpr} = nothing
)::AffExpr
    tres = max(1.0, input.temporal_resolution_hours)
    cost = day_operational_cost(ps_vars, input, hours; tech_output)
    tech_day_cost === nothing || add_to_expression!(cost, tech_day_cost)
    return cost * tres * weight
end

"""
    tsam_day_map(input, y_idx) -> Vector{Int}

The TSAM period (1..K) of each day of year `y_idx` (`input.tsam_day_period`),
checked: one entry per day of the year (`hours_per_year` steps at the
temporal resolution, in periods of a day), every entry a period, and each
period's count of days equal to its weight (the days its cost, energy
balance, RE target and curtailment stand for). Without a map, a single
period stands for every day of the year. Anything else raises: the seasonal
storage would run through a different year than the one the costs and
balances count, which gives the master free energy.
"""
function tsam_day_map(input::MasterProblemInput, y_idx::Int)::Vector{Int}
    weights = input.tsam_period_weights[y_idx]
    K = length(weights)
    year = input.years[y_idx]
    tres = max(1, input.temporal_resolution_hours)
    tpd = max(1, 24 ÷ tres)
    hpy = y_idx <= length(input.hours_per_year) ? input.hours_per_year[y_idx] : HOURS_STD_YEAR
    D = (hpy ÷ tres) ÷ tpd
    map = y_idx <= length(input.tsam_day_period) ? input.tsam_day_period[y_idx] : Int[]
    if isempty(map)
        K == 1 || error("TSAM inter-period linking in $year needs tsam_day_period, the " *
                        "period of each of the year's $D days ($K periods)")
        map = fill(1, D)
    end
    length(map) == D || error("tsam_day_period for $year has $(length(map)) days; " *
                              "the year has $D")
    bad = findfirst(k -> !(1 <= k <= K), map)
    bad === nothing || error("tsam_day_period for $year: day $bad is in period " *
                             "$(map[bad]), not one of the $K periods")
    counts = zeros(Int, K)
    for k in map
        counts[k] += 1
    end
    for k in 1:K
        abs(counts[k] - weights[k]) <= 1e-6 * max(1.0, abs(weights[k])) ||
            error("TSAM period $k of $year stands for $(weights[k]) days (its weight) " *
                  "but tsam_day_period gives it $(counts[k]): the seasonal storage would " *
                  "run through a different year than the costs and energy balance count")
    end
    return map
end

"""
    add_inter_day_state!(model, day_period, Δ, u, l, ρ, lo, hi, name)
        -> Vector{VariableRef}

The inter-day state of one storage over the year (Kotzur et al. 2018,
superposition of inter- and intra-period states): `S[d]`, the level at the
start of day `d` (1..D, year-cyclic), moves by the intra-period change of
the day's period `k = day_period[d]`,

    S[d+1] = ρ[k]·S[d] + Δ[k]

and the level within the day, `S[d]` plus the period's level relative to its
start, stays in `[lo, hi]`: `S[d] + u[k] ≤ hi` and `ρ[k]·S[d] + l[k] ≥ lo`,
with `u[k]` (`l[k]`) the period's highest (lowest) level relative to its
start and `ρ[k]` the decay of a level over the period (1 without losses;
with losses both rows are sufficient, as the decay of `S[d]` within the day
lies between 1 and `ρ[k]`). `lo` and `hi` may hold capacity built by the
master. Three rows per day, whatever the number of periods.
"""
function add_inter_day_state!(model::Model, day_period::Vector{Int}, Δ, u, l, ρ, lo, hi,
                              name::String)::Vector{VariableRef}
    D = length(day_period)
    S = [@variable(model, lower_bound = 0, base_name = "$(name)_d$(d - 1)") for d in 1:D]
    for d in 1:D
        k = day_period[d]
        @constraint(model, S[mod1(d + 1, D)] == ρ[k] * S[d] + Δ[k],
                    base_name = "$(name)_chain_d$(d - 1)")
        @constraint(model, S[d] + u[k] <= hi, base_name = "$(name)_max_d$(d - 1)")
        @constraint(model, ρ[k] * S[d] + l[k] >= lo, base_name = "$(name)_min_d$(d - 1)")
    end
    return S
end

"""
    add_tsam_periods_validation!(model, vars, input, targets)

Add operational validation using TSAM representative periods with seasonal
storage. Replaces add_representative_days_validation! when use_tsam=true.

Each period's costs, energy balance, CO2, RE target and curtailment count
× its weight (the days it stands for). With `tsam_inter_period_linking`,
batteries and reservoirs carry energy across the year by the superposition
of Kotzur et al. (2018, Applied Energy 213:123-135): each period keeps its
own storage trajectory from a free start level (the intra-period state: its
change Δ and its highest and lowest level relative to the start), and an
inter-day state runs through every day of the year on the period of that
day (`tsam_day_map`, `add_inter_day_state!`), stored in
`vars.inter_period_soc` / `vars.inter_period_reservoir`. Each period thus
counts in the storage exactly as often as in the costs and balances. The
master's candidate battery technologies stay cyclic within each period.
"""
function add_tsam_periods_validation!(
    model::Model,
    vars::MasterProblemVariables,
    base_input::MasterProblemInput,
    targets::Dict{Tuple{Int,Int}, Float64};
    scenario::Union{Nothing, Scenario} = nothing,
    scenario_idx::Int = 0
)
    num_years = length(base_input.years)
    n_bat = length(base_input.batteries)
    n_buses = base_input.network.num_buses
    timesteps_per_day = 24 ÷ base_input.temporal_resolution_hours
    # Seasonal hydro: any reservoir unit makes the inter-period reservoir state
    # relevant (gated on the same tsam_inter_period_linking flag as batteries).
    has_reservoir = any(any(g.reservoir_capacity .> 0) for g in base_input.generators)
    # Distinct names per stochastic scenario (each has its own periods and states)
    day_offset = 1000 * scenario_idx
    stag = scenario_idx > 0 ? "_s$(scenario_idx)" : ""
    prev_floor = nothing   # re_cap_exemption

    for y_idx in 1:num_years
        input = year_master_input(base_input, y_idx)
        scenario === nothing ||
            (input = scenario_master_input(input, scenario.multipliers, y_idx))
        period_starts = input.tsam_period_start_hours[y_idx]
        period_weights = input.tsam_period_weights[y_idx]
        K = length(period_starts)

        if K < 1
            @warn "No TSAM periods for year $(input.years[y_idx])"
            continue
        end
        tres_p = max(1.0, input.temporal_resolution_hours)
        periods = [(h, min(h + timesteps_per_day - 1, size(input.base_demand, 1)) - h + 1,
                    tres_p * period_weights[k]) for (k, h) in enumerate(period_starts)]
        cap_exempt, prev_floor = re_cap_exemption(input, targets, y_idx, periods, prev_floor)

        vars.operational_costs[y_idx] = AffExpr[]
        day_spends = AffExpr[]   # the days' money, for budget_scope: total
        annual_co2 = [AffExpr(0.0) for _ in input.co2_budget_groups]
        re_energy = [AffExpr(0.0) for _ in master_systems(input)]
        re_generation = [AffExpr(0.0) for _ in re_energy]
        re_demand = zeros(length(re_energy))
        curt_year = CurtailmentYear()

        # --- Seasonal storage: what is linked this year ---
        # Batteries with energy in service this year (the capacity the
        # period's SOC bounds use) at the buses the day model has them;
        # reservoirs at the buses of their generator
        day_period = Int[]
        bat_caps = Dict{Tuple{Int,Int}, AffExpr}()
        res_keys = Tuple{Int,Int}[]
        if input.tsam_inter_period_linking && (n_bat > 0 || has_reservoir)
            for b in 1:n_bat, n in 1:n_buses
                bat = input.batteries[b]
                (bat.max_discharge_power[n] > 0 ||
                 (length(bat.invest_max_power) >= n && bat.invest_max_power[n] > 0)) || continue
                cap = battery_energy_in_service(vars, bat, b, n, y_idx)
                iszero(cap) || (bat_caps[(b, n)] = cap)
            end
            for (g, gen) in enumerate(input.generators), n in 1:n_buses
                gen.reservoir_capacity[n] > 0 && gen.rated_power[n] > 0 && push!(res_keys, (g, n))
            end
            isempty(bat_caps) && isempty(res_keys) || (day_period = tsam_day_map(input, y_idx))
        end
        bat_keys = sort!(collect(keys(bat_caps)))
        # Per linked storage and period: start level, intra-period change Δ,
        # highest (u) and lowest (l) level relative to the start, decay ρ
        intra() = (s0 = Any[nothing for _ in 1:K], Δ = [AffExpr(0.0) for _ in 1:K],
                   u = [AffExpr(0.0) for _ in 1:K], l = [AffExpr(0.0) for _ in 1:K],
                   ρ = ones(K))
        bat_intra = Dict(key => intra() for key in bat_keys)
        res_intra = Dict(key => intra() for key in res_keys)

        # --- Each period ---
        for (k, start_hour) in enumerate(period_starts)
            weight = period_weights[k]
            end_hour = min(start_hour + timesteps_per_day - 1, size(input.base_demand, 1))
            hours = end_hour - start_hour + 1
            if hours < 1
                continue
            end

            day_demand = input.base_demand[start_hour:end_hour, :]

            # Create PowerSystemVariables for this period
            ps_vars = create_day_ps_vars!(model, input, y_idx, k + day_offset, hours)

            # A linked storage starts the period at a free level s0 and ends
            # it at s0 + Δ (a reservoir: its start level after the period's
            # evaporation, ρ·s0, + Δ) instead of being cyclic
            tech_output = nothing
            soc_initial = isempty(bat_keys) ? nothing : Dict{Tuple{Int,Int}, Any}()
            soc_final = isempty(bat_keys) ? nothing : Dict{Tuple{Int,Int}, Any}()
            for (b, n) in bat_keys
                st = bat_intra[(b, n)]
                tag = "$(stag)_y$(y_idx)_b$(b)_n$(n)_p$(k)"
                st.s0[k] = @variable(model, lower_bound = 0, base_name = "soc_start$(tag)")
                st.Δ[k] = 1.0 * @variable(model, base_name = "soc_delta$(tag)")
                soc_initial[(b, n)] = st.s0[k]
                soc_final[(b, n)] = st.s0[k] + st.Δ[k]
            end
            res_initial = isempty(res_keys) ? nothing : Dict{Tuple{Int,Int}, Any}()
            res_final = isempty(res_keys) ? nothing : Dict{Tuple{Int,Int}, Any}()
            for (g, n) in res_keys
                st = res_intra[(g, n)]
                tag = "$(stag)_y$(y_idx)_g$(g)_n$(n)_p$(k)"
                st.ρ[k] = (1.0 - input.generators[g].reservoir_evaporation_rate[n])^hours
                st.s0[k] = @variable(model, lower_bound = 0, base_name = "reslvl_start$(tag)")
                st.Δ[k] = 1.0 * @variable(model, base_name = "reslvl_delta$(tag)")
                res_initial[(g, n)] = st.s0[k]
                res_final[(g, n)] = st.ρ[k] * st.s0[k] + st.Δ[k]
            end

            tech_output, tech_day_cost, tech_spend = add_day_operational_constraints!(
                model, ps_vars, vars, input, y_idx, k + day_offset,
                day_demand, start_hour;
                initial_soc_overrides=soc_initial,
                final_soc_targets=soc_final,
                initial_reservoir_overrides=res_initial,
                final_reservoir_targets=res_final,
                re_cap_exempt=cap_exempt,
                curtailment=curt_year,
                period_scale=max(1.0, input.temporal_resolution_hours) * weight
            )

            # The period's highest and lowest level relative to its start
            # (a reservoir: relative to its start level decayed by the
            # evaporation so far, i.e. the trajectory of a reservoir started
            # empty)
            for (b, n) in bat_keys
                st = bat_intra[(b, n)]
                tag = "$(stag)_y$(y_idx)_b$(b)_n$(n)_p$(k)"
                u = @variable(model, lower_bound = 0, base_name = "soc_intra_max$(tag)")
                l = @variable(model, upper_bound = 0, base_name = "soc_intra_min$(tag)")
                for t in 2:(hours + 1)
                    @constraint(model, ps_vars.bat_soc[b, n, t] - st.s0[k] <= u)
                    @constraint(model, ps_vars.bat_soc[b, n, t] - st.s0[k] >= l)
                end
                st.u[k] = 1.0 * u
                st.l[k] = 1.0 * l
            end
            for (g, n) in res_keys
                st = res_intra[(g, n)]
                tag = "$(stag)_y$(y_idx)_g$(g)_n$(n)_p$(k)"
                keep = 1.0 - input.generators[g].reservoir_evaporation_rate[n]
                u = @variable(model, lower_bound = 0, base_name = "reslvl_intra_max$(tag)")
                l = @variable(model, upper_bound = 0, base_name = "reslvl_intra_min$(tag)")
                for t in 2:(hours + 1)
                    rel = ps_vars.reservoir_level[g, n, t] - keep^(t - 1) * st.s0[k]
                    @constraint(model, rel <= u)
                    @constraint(model, rel >= l)
                end
                st.u[k] = 1.0 * u
                st.l[k] = 1.0 * l
            end

            # Weighted operational cost (candidate technologies' too)
            day_cost = calculate_day_operational_cost_tsam(
                ps_vars, input, y_idx, hours, weight;
                tech_output=tech_output, tech_day_cost=tech_day_cost
            )
            # M2 (#89): fuel balance, fuel infrastructure and electrolyzers
            # (cyclic tanks per period; the day's cost is in $, × weight)
            fuel_day = add_master_fuel_day!(model, ps_vars, input, y_idx,
                start_hour, hours, tech_output;
                extra = key -> master_fuel_capacity(vars, input, y_idx, key))
            add_to_expression!(day_cost, fuel_day, weight)
            push!(vars.operational_costs[y_idx], day_cost)
            input.budget_total && push!(day_spends,
                year_day_spend(model, ps_vars, input, hours,
                               max(1.0, input.temporal_resolution_hours) * weight, weight,
                               tech_output, tech_spend, fuel_day))
            record_master_day!(model, scenario_idx, y_idx, ps_vars, input, hours,
                max(1.0, input.temporal_resolution_hours) * weight)

            # The period's CO2, renewable output and demand, weighted to
            # the year like its cost
            tres = max(1.0, input.temporal_resolution_hours)
            for (i, e) in enumerate(day_co2_by_group(ps_vars, tech_output, input, hours))
                add_to_expression!(annual_co2[i], e, tres * weight)
            end
            add_day_re!(re_energy, re_generation, re_demand, ps_vars, tech_output, input, y_idx,
                        start_hour, hours, tres * weight)
        end

        # --- The inter-day state of each linked storage over the year ---
        if !isempty(bat_keys)
            vars.inter_period_soc[y_idx] = Dict{Tuple{Int,Int,Int}, VariableRef}()
            for (b, n) in bat_keys
                st = bat_intra[(b, n)]
                S = add_inter_day_state!(model, day_period, st.Δ, st.u, st.l, st.ρ,
                    0.0, bat_caps[(b, n)], "soc_inter$(stag)_y$(y_idx)_b$(b)_n$(n)")
                for (d, v) in enumerate(S)
                    vars.inter_period_soc[y_idx][(b, n, d - 1)] = v
                end
            end
        end
        if !isempty(res_keys)
            vars.inter_period_reservoir[y_idx] = Dict{Tuple{Int,Int,Int}, VariableRef}()
            for (g, n) in res_keys
                gen = input.generators[g]
                st = res_intra[(g, n)]
                # The in-service capacity (existing + built so far), as the
                # period's level bounds
                res_cap = reservoir_in_service(vars, gen, g, n, y_idx)
                name = "reslvl_inter$(stag)_y$(y_idx)_g$(g)_n$(n)"
                S = add_inter_day_state!(model, day_period, st.Δ, st.u, st.l, st.ρ,
                    gen.reservoir_min_level[n] * res_cap, gen.reservoir_max_level[n] * res_cap,
                    name)
                for (d, v) in enumerate(S)
                    vars.inter_period_reservoir[y_idx][(g, n, d - 1)] = v
                end
                # Head-dependent power reads the period's own level: the
                # period starts no higher than the reservoir on any of its
                # days, so it never sees more head than the year gives it
                hmf = n <= length(gen.reservoir_head_min_factor) ?
                    gen.reservoir_head_min_factor[n] : 1.0
                span = (gen.reservoir_max_level[n] - gen.reservoir_min_level[n]) *
                       gen.reservoir_capacity[n]
                if hmf < 1.0 && span > 0
                    for (d, k) in enumerate(day_period)
                        st.s0[k] === nothing && continue
                        @constraint(model, st.s0[k] <= S[d], base_name = "$(name)_head_d$(d - 1)")
                    end
                end
            end
        end

        input.budget_total && record_year_operating_cost!(model, scenario_idx, y_idx, day_spends)
        push!(vars.operational_costs[y_idx],
            add_curtailment_limit!(model, input, curt_year, y_idx; scenario_idx))
        push!(vars.operational_costs[y_idx],
            add_co2_budget_constraints!(model, vars, input, y_idx, annual_co2;
                scenario_idx=scenario_idx))
        push!(vars.operational_costs[y_idx],
            add_re_target_constraints!(model, vars, input, y_idx, targets,
                re_energy, re_generation, re_demand; scenario_idx=scenario_idx))
    end
end

# =============================================================================
# Main API
# =============================================================================

"""
    create_master_problem(input; use_representative_days=true)

Create the Master Problem optimization model.

# Arguments
- `input::MasterProblemInput`: Master problem configuration
- `use_representative_days::Bool`: Whether to add representative day subproblems
                                   CRITICAL: Must be true for correct RE target enforcement!
- `capacity_states::Bool`: hourly rows reference per-year capacity state
                           variables (`add_capacity_states!`, default) instead
                           of the investment sums; the same LP either way
- `defer_budget::Bool`: with `input.budget_total`, leave the budget out (the
                        stochastic master adds it once its scenarios'
                        operating costs exist)

# Returns
- `model::Model`: JuMP model
- `vars::MasterProblemVariables`: Variable container
- `targets::Dict{Tuple{Int,Int}, Float64}`: RE targets by (system, year)
"""
function create_master_problem(
    input::MasterProblemInput;
    use_representative_days::Bool = true,
    custom_constraints = nothing,
    capacity_states::Bool = true,
    defer_budget::Bool = false
)
    # Create model with optimizer
    model = solver_model(create_optimizer(
        solver_name=input.solver_name,
        threads=input.threads,
        time_limit=input.time_limit,
        gap=input.gap,
        verbose=input.verbose,
        solver_options=input.solver_options
    ))

    # Calculate RE targets
    targets = calculate_target_ratios(input)

    # Create variables
    vars = build_master_variables!(model, input)
    # Capacity in service per year, referenced by the hourly rows
    capacity_states && add_capacity_states!(model, vars, input)

    # Add constraints
    add_investment_constraints!(model, vars, input)
    # budget_scope: total needs the years' operating cost (below)
    input.budget_total || add_budget_constraints!(model, vars, input)
    # Retirement cascade constraints - ensures once retired, stays retired
    add_retirement_cascade_constraints!(model, vars, input)
    add_capacity_adequacy_constraints!(model, vars, input)
    add_transmission_symmetry_constraints!(model, vars, input)

    # CRITICAL: Add representative days / TSAM periods validation
    # This links investment decisions to operational feasibility and RE targets
    if use_representative_days
        if input.use_tsam
            add_tsam_periods_validation!(model, vars, input, targets)
        else
            add_representative_days_validation!(model, vars, input, targets)
        end
    end

    # The annual RE target, minimum-increment floor and growth cap come with
    # the representative days / TSAM periods (add_re_target_constraints!),
    # which hold the renewable and total generation (define_re_share!)
    define_re_share!(model, vars, input)

    # Investment and operating cost of each year against one budget
    # (budget_scope: total); a stochastic master adds it over its scenarios
    if input.budget_total && !defer_budget
        add_budget_constraints!(model, vars, input;
            operating = budget_operating_cost(model, vars, input))
    end

    # User-defined investment constraints (declarative config + plugin overlays).
    apply_user_constraints!(model, vars, input, custom_constraints)

    # Build objective (now includes operational costs from representative days)
    build_master_objective!(model, vars, input)

    return model, vars, targets
end

"""
    extract_master_solution(model, vars, input; unit_operation=false)

Extract solution values from solved Master Problem. With
`unit_operation = true` the result also holds each existing unit's
generation and costs (`extract_unit_operation`), for `calculate_unit_npv`.
"""
function extract_master_solution(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput;
    unit_operation::Bool = false
)::MasterProblemResult
    num_years = length(input.years)
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node
    n_gen = length(input.generators)
    n_bat = length(input.batteries)

    # Extract investment decisions
    n_tech = length(input.technologies)
    n_bat_tech = length(input.battery_technologies)
    tech_inv = Dict{Int, Dict{Int, Vector{Float64}}}()
    bat_tech_pow_inv = Dict{Int, Dict{Int, Vector{Float64}}}()
    bat_tech_cap_inv = Dict{Int, Dict{Int, Vector{Float64}}}()
    trans_inv = Dict{Int, Dict{Tuple{Int,Int}, Float64}}()

    gen_life = Dict{Int, Dict{Int, Vector{Float64}}}()
    bat_life = Dict{Int, Dict{Int, Vector{Float64}}}()

    total_inv_by_year = zeros(num_years)
    total_op_by_year = zeros(num_years)
    re_by_year = zeros(num_years)

    cumul_gen = Dict{Int, Dict{Int, Vector{Float64}}}()
    cumul_bat = Dict{Int, Dict{Int, Vector{Float64}}}()
    cumul_bat_power = Dict{Int, Dict{Int, Vector{Float64}}}()
    cumul_tech = Dict{Int, Dict{Int, Vector{Float64}}}()
    cumul_bat_tech_pow = Dict{Int, Dict{Int, Vector{Float64}}}()
    cumul_bat_tech_cap = Dict{Int, Dict{Int, Vector{Float64}}}()

    for y_idx in 1:num_years
        tech_inv[y_idx] = Dict{Int, Vector{Float64}}()
        bat_tech_pow_inv[y_idx] = Dict{Int, Vector{Float64}}()
        bat_tech_cap_inv[y_idx] = Dict{Int, Vector{Float64}}()
        trans_inv[y_idx] = Dict{Tuple{Int,Int}, Float64}()
        gen_life[y_idx] = Dict{Int, Vector{Float64}}()
        bat_life[y_idx] = Dict{Int, Vector{Float64}}()
        cumul_gen[y_idx] = Dict{Int, Vector{Float64}}()
        cumul_bat[y_idx] = Dict{Int, Vector{Float64}}()
        cumul_bat_power[y_idx] = Dict{Int, Vector{Float64}}()
        cumul_tech[y_idx] = Dict{Int, Vector{Float64}}()
        cumul_bat_tech_pow[y_idx] = Dict{Int, Vector{Float64}}()
        cumul_bat_tech_cap[y_idx] = Dict{Int, Vector{Float64}}()

        # Existing generator cumulative capacity (no investments, existing + life extension only)
        for g in 1:n_gen
            gen = input.generators[g]
            cumul_gen[y_idx][g] = zeros(n_buses)
            for n in 1:n_buses
                age_at_year = gen.initial_age[n] + (y_idx - 1)
                deg_rate = gen.degradation_rate[n]
                if age_at_year < gen.life_time[n] && gen.rated_power[n] > 0
                    cumul_gen[y_idx][g][n] = gen.rated_power[n] * (1.0 - deg_rate) ^ age_at_year
                elseif gen.rated_power[n] > 0
                    life_ext_var = vars.gen_life_extension[y_idx][g][n]
                    if life_ext_var !== nothing
                        cumul_gen[y_idx][g][n] += capacity_value(life_ext_var)
                    end
                end
            end
            # Share in service: 1 within the lifetime; past it, the share of
            # the (degraded) capacity the master keeps by life extension
            gen_life[y_idx][g] = zeros(n_buses)
            for n in 1:n_buses
                age_at_year = gen.initial_age[n] + (y_idx - 1)
                ext = vars.gen_life_extension[y_idx][g][n]
                gen_life[y_idx][g][n] = if age_at_year < gen.life_time[n]
                    1.0
                elseif ext === nothing
                    0.0
                else
                    ub = gen.rated_power[n] * (1.0 - gen.degradation_rate[n]) ^ age_at_year
                    ub > 0 ? clamp(capacity_value(ext) / ub, 0.0, 1.0) : 0.0
                end
            end
        end

        # Existing battery cumulative capacity (no investments, existing + life extension only)
        for b in 1:n_bat
            bat = input.batteries[b]
            cumul_bat[y_idx][b] = zeros(n_buses)
            cumul_bat_power[y_idx][b] = zeros(n_buses)
            for n in 1:n_buses
                age_at_year = bat.initial_age[n] + (y_idx - 1)
                if age_at_year < bat.life_time[n]
                    deg = battery_degradation_factor(bat, n, y_idx)
                    if bat.capacity[n] > 0
                        cumul_bat[y_idx][b][n] = bat.capacity[n] * deg
                    end
                    if bat.max_discharge_power[n] > 0
                        cumul_bat_power[y_idx][b][n] = bat.max_discharge_power[n] * deg
                    end
                elseif bat.max_discharge_power[n] > 0
                    life_ext_var = vars.bat_life_extension[y_idx][b][n]
                    if life_ext_var !== nothing
                        life_ext_val = capacity_value(life_ext_var)
                        cumul_bat_power[y_idx][b][n] += life_ext_val
                        if bat.max_discharge_power[n] > 0
                            ratio = bat.capacity[n] / bat.max_discharge_power[n]
                            cumul_bat[y_idx][b][n] += life_ext_val * ratio
                        end
                    end
                end
            end
            # Share in service, as for generators: past the lifetime, the
            # share of the degraded power the master keeps
            bat_life[y_idx][b] = zeros(n_buses)
            for n in 1:n_buses
                age_at_year = bat.initial_age[n] + (y_idx - 1)
                ext = vars.bat_life_extension[y_idx][b][n]
                ub = bat.max_discharge_power[n] * battery_degradation_factor(bat, n, y_idx)
                bat_life[y_idx][b][n] = if age_at_year < bat.life_time[n]
                    1.0
                elseif ext === nothing || ub <= 0
                    0.0
                else
                    clamp(capacity_value(ext) / ub, 0.0, 1.0)
                end
            end
        end

        # Technology investment extraction
        yp = inv_period_year(y_idx, vars.years_per_inv_period)
        for t in 1:n_tech
            tech = input.technologies[t]
            # Investment value: only non-zero at period start, zero otherwise
            if y_idx == yp
                tech_inv[y_idx][t] = [capacity_value(vars.tech_investment[yp][t][n]) for n in 1:n_buses]
            else
                tech_inv[y_idx][t] = zeros(n_buses)
            end
            # Cumulative with degradation and retirement (sum over period starts only)
            cumul_tech[y_idx][t] = zeros(n_buses)
            for n in 1:n_buses
                for y in 1:vars.years_per_inv_period:y_idx
                    inv_age = y_idx - y
                    if inv_age < tech.life_time[n]
                        deg = (1.0 - tech.degradation_rate[n]) ^ inv_age
                        cumul_tech[y_idx][t][n] += capacity_value(vars.tech_investment[y][t][n]) * deg
                    end
                end
            end
        end

        # Battery technology investment extraction
        for bt in 1:n_bat_tech
            btech = input.battery_technologies[bt]
            if y_idx == yp
                bat_tech_pow_inv[y_idx][bt] = [capacity_value(vars.bat_tech_power_investment[yp][bt][n]) for n in 1:n_buses]
                bat_tech_cap_inv[y_idx][bt] = [capacity_value(vars.bat_tech_capacity_investment[yp][bt][n]) for n in 1:n_buses]
            else
                bat_tech_pow_inv[y_idx][bt] = zeros(n_buses)
                bat_tech_cap_inv[y_idx][bt] = zeros(n_buses)
            end
            cumul_bat_tech_pow[y_idx][bt] = zeros(n_buses)
            cumul_bat_tech_cap[y_idx][bt] = zeros(n_buses)
            for n in 1:n_buses
                for y in 1:vars.years_per_inv_period:y_idx
                    inv_age = y_idx - y
                    if inv_age < btech.life_time[n]
                        deg = bat_tech_degradation_factor(btech, n, inv_age)
                        cumul_bat_tech_pow[y_idx][bt][n] += capacity_value(vars.bat_tech_power_investment[y][bt][n]) * deg
                        cumul_bat_tech_cap[y_idx][bt][n] += capacity_value(vars.bat_tech_capacity_investment[y][bt][n]) * deg
                    end
                end
            end
        end

        # Transmission investment
        if haskey(vars.transfer_investment, yp)
            for ((i, j), var) in vars.transfer_investment[yp]
                if y_idx == yp
                    trans_inv[y_idx][(i, j)] = capacity_value(var)
                else
                    trans_inv[y_idx][(i, j)] = 0.0
                end
            end
        end

    end

    # Achieved RE share per year and system
    re_by_year, re_by_system = master_re_shares(input,
        master_re_energy(model, vars, input))

    # Extract reservoir investment
    res_inv = Dict{Int, Dict{Int, Vector{Float64}}}()
    for y_idx in 1:num_years
        res_inv[y_idx] = Dict{Int, Vector{Float64}}()
        for g in 1:n_gen
            if haskey(vars.reservoir_investment[y_idx], g)
                res_inv[y_idx][g] = [capacity_value(vars.reservoir_investment[y_idx][g][n]) for n in 1:n_buses]
            end
        end
    end

    # Converter investment: MW built in each period-start year
    conv_inv = Dict{Int, Dict{Tuple{String,Int}, Float64}}()
    for y_idx in 1:num_years
        conv_inv[y_idx] = Dict{Tuple{String,Int}, Float64}()
        for (key, var) in get(vars.converter_investment, y_idx, Dict{Tuple{String,Int}, VariableRef}())
            conv_inv[y_idx][key] = capacity_value(var)
        end
    end

    # Overnight cost of each year's builds, and the annuities each year pays
    # (base costs and discount rate)
    annuity_by_year = zeros(num_years)
    streams = Dict{Tuple{Int, Float64}, Float64}()
    for t in master_investment_terms(vars, input)
        t.cost == 0 && continue
        built = value(t.var) * t.cost
        total_inv_by_year[t.year] += built
        crf = capital_recovery_factor(input.discount_rate, t.life)
        for y in annuity_years(t.year, t.life, num_years)
            annuity_by_year[y] += built * crf
        end
        abs(built) > 1e-12 &&
            (streams[(t.year, t.life)] = get(streams, (t.year, t.life), 0.0) + built * crf)
    end

    # Operating cost of each year's representative days (or TSAM periods),
    # scaled to the year and undiscounted; the expected cost over the
    # scenarios in a stochastic master. Benders replaces it with its
    # subproblems' costs.
    for (y_idx, costs) in vars.operational_costs
        1 <= y_idx <= num_years || continue
        total_op_by_year[y_idx] = sum(value(c) for c in costs; init = 0.0)
    end

    return MasterProblemResult(
        termination_status(model),
        objective_value(model),
        solve_time(model),
        tech_inv,
        bat_tech_pow_inv,
        bat_tech_cap_inv,
        trans_inv,
        gen_life,
        bat_life,
        total_inv_by_year,
        total_op_by_year,
        re_by_year,
        re_by_system,
        cumul_gen,
        cumul_bat,
        cumul_bat_power,
        cumul_tech,
        cumul_bat_tech_pow,
        cumul_bat_tech_cap,
        res_inv,
        conv_inv,
        extract_master_fuel_solution(vars, input),  # M2 (#89)
        unit_operation ? extract_unit_operation(model, vars, input) : UnitOperation[],
        annuity_by_year,
        sort!([(y, life, a) for ((y, life), a) in streams])
    )
end

"""
    annuity_costs(result, num_years) -> Vector{Float64}

`result.annuity_cost_by_year`, or zeros for a result built without it.
"""
annuity_costs(result::MasterProblemResult, num_years::Int) =
    length(result.annuity_cost_by_year) == num_years ? result.annuity_cost_by_year :
                                                       zeros(num_years)

"""
    master_re_energy(model, vars, input) -> Dict{Tuple{Int,Int}, Tuple{Float64, Float64}}

Renewable and total generation (MWh/year) per (system, year) in the solved
master, from its representative days (`model.ext[:master_re]`, kept by
`add_re_target_constraints!`); the probability-weighted mean over the
scenarios in a stochastic master. Without representative days (a master
built without them, the Benders master) a system's year counts its ratio
`re_energy_ratio` over a unit generation.
"""
function master_re_energy(model::Model, vars::MasterProblemVariables,
                          input::MasterProblemInput)
    out = Dict{Tuple{Int,Int}, Tuple{Float64, Float64}}()
    record = get(model.ext, :master_re, nothing)
    if record === nothing || isempty(record)
        for ((s, y), v) in vars.re_energy_ratio
            out[(s, y)] = (value(v), 1.0)
        end
        return out
    end
    scenarios = get(model.ext, :master_scenarios, nothing)
    for ((k, s, y), (energy, generation, _)) in record
        p = k == 0 || scenarios === nothing ? 1.0 : scenarios[k].probability
        e0, g0 = get(out, (s, y), (0.0, 0.0))
        out[(s, y)] = (e0 + p * value(energy), g0 + p * value(generation))
    end
    return out
end

"""
    master_re_shares(input, re_energy) -> (by_year, by_system)

Achieved RE share from the renewable and total generation per (system,
year) `re_energy` (`master_re_energy`): per year over all systems
(Σ renewable / Σ generation, energy-weighted) and per system name. A year
without generation reports 0.
"""
function master_re_shares(input::MasterProblemInput,
                          re_energy::AbstractDict{Tuple{Int,Int}, Tuple{Float64, Float64}})
    num_years = length(input.years)
    systems = master_systems(input)
    share(e, d) = d > 0 ? e / d : 0.0
    by_year = zeros(num_years)
    by_system = Dict{String, Vector{Float64}}(sr.name => zeros(num_years) for sr in systems)
    for y in 1:num_years
        e_tot, d_tot = 0.0, 0.0
        for (s, sr) in enumerate(systems)
            e, d = get(re_energy, (s, y), (0.0, 0.0))
            by_system[sr.name][y] = share(e, d)
            e_tot += e
            d_tot += d
        end
        by_year[y] = share(e_tot, d_tot)
    end
    return by_year, by_system
end

"""
    master_conflict(model; limit=20) -> Vector{String}

A set of constraints and bounds that cannot hold together (an irreducible
infeasible subsystem) of a master the solver found infeasible, at most
`limit` of them, printed as constraints. Empty when the solver cannot
compute one (HiGHS and Gurobi can) or finds none, so the caller can always
report it.
"""
function master_conflict(model::Model; limit::Int = 20)::Vector{String}
    out = String[]
    try
        JuMP.compute_conflict!(model)
        MOI.get(model, MOI.ConflictStatus()) == MOI.CONFLICT_FOUND || return out
        for (F, S) in JuMP.list_of_constraint_types(model),
                c in JuMP.all_constraints(model, F, S)
            MOI.get(model, MOI.ConstraintConflictStatus(), c) == MOI.NOT_IN_CONFLICT && continue
            push!(out, string(c))
            length(out) >= limit && return out
        end
    catch e
        @debug "No conflict from the solver" exception = e
    end
    return out
end

# =============================================================================
# Stochastic Programming Support
# =============================================================================

"""
    create_stochastic_master_problem(input::StochasticMasterInput; custom_constraints=nothing)

Two-stage stochastic Master Problem. Investment is first-stage, common to
all scenarios. Each scenario has its own representative-day (or TSAM)
operation, built on its own demand and costs (`scenario_master_input`), so
the plan must serve every scenario and pays each one's operating cost
weighted by its probability.
"""
function create_stochastic_master_problem(input::StochasticMasterInput;
                                          custom_constraints = nothing)
    base_input = input.base_input

    if !input.use_stochastic || isempty(input.scenarios)
        model, vars, targets = create_master_problem(base_input;
            use_representative_days=true, custom_constraints=custom_constraints)
        return model, vars, targets, input.scenarios
    end

    # Investment structure only; the operation is added per scenario below.
    model, vars, targets = create_master_problem(base_input;
        use_representative_days=false, custom_constraints=custom_constraints,
        defer_budget=true)

    scenario_costs = Dict{Int, Vector{AffExpr}}[]
    first_soc = nothing
    first_res = nothing
    for (k, scenario) in enumerate(input.scenarios)
        empty!(vars.operational_costs)
        if base_input.use_tsam
            add_tsam_periods_validation!(model, vars, base_input, targets;
                scenario=scenario, scenario_idx=k)
        else
            add_representative_days_validation!(model, vars, base_input, targets;
                scenario=scenario, scenario_idx=k)
        end
        push!(scenario_costs, copy(vars.operational_costs))
        if k == 1
            # Report the first scenario's seasonal chains (each scenario owns its own)
            first_soc = copy(vars.inter_period_soc)
            first_res = copy(vars.inter_period_reservoir)
        end
    end
    merge!(empty!(vars.inter_period_soc), first_soc)
    merge!(empty!(vars.inter_period_reservoir), first_res)

    # Expected operating cost per year, for anything that reads operational_costs
    empty!(vars.operational_costs)
    for (k, scenario) in enumerate(input.scenarios), (y_idx, costs) in scenario_costs[k]
        expected = get!(vars.operational_costs, y_idx, [AffExpr(0.0)])[1]
        for c in costs
            add_to_expression!(expected, c, scenario.probability)
        end
    end

    if base_input.budget_total
        add_budget_constraints!(model, vars, base_input;
            operating = budget_operating_cost(model, vars, base_input;
                                              scenarios = input.scenarios))
    end

    build_stochastic_objective!(model, vars, base_input, input.scenarios, scenario_costs)
    # Each scenario's weight in the units' NPV (extract_unit_operation) and
    # in the reported RE share
    model.ext[:master_scenarios] = input.scenarios
    define_re_share!(model, vars, base_input)

    return model, vars, targets, input.scenarios
end

"""
    with_fields(x; kw...)

Copy of the immutable struct `x` with the given fields replaced.
"""
function with_fields(x::T; kw...) where T
    return T((haskey(kw, f) ? kw[f] : getfield(x, f) for f in fieldnames(T))...)
end

"""
    year_master_input(input, year_idx) -> MasterProblemInput

The master input of year `year_idx`: each generator and candidate technology
burning a fuel of the fuels block pays its `fuel_cost` grown at that fuel's
`price_growth_rate` from `fuel_price_base_year` (`fuel_price_factor`), as
the dispatch charges it that year. A unit with no fuel there keeps its
`fuel_cost`; offer curves (`fuel_cost_curve`) stay as given. `input` itself
when no fuel grows. A stochastic scenario builds on it
(`scenario_master_input`), so its `fuel_price_growth` multiplies the grown
price.
"""
function year_master_input(input::MasterProblemInput, year_idx::Int)::MasterProblemInput
    rates = input.fuel_price_growth
    any(!iszero, values(rates)) || return input
    year = input.years[year_idx]
    grown(u) = (r = get(rates, u.fuel, 0.0); iszero(r) ? u :
        with_fields(u; fuel_cost = u.fuel_cost .* fuel_price_factor(r, year,
                                                                   input.fuel_price_base_year)))
    return with_fields(input;
        generators = GeneratorConfig[grown(g) for g in input.generators],
        technologies = TechnologyConfig[grown(t) for t in input.technologies])
end

"""
    scenario_master_input(input, mult, year_idx)

The master input a stochastic scenario operates on in year `year_idx`:
- demand × `demand_growth`^(year_idx − 1) (an annual factor: 1.02 = +2 %/year)
- fuel cost × `fuel_cost` × `fuel_price_growth`^(year_idx − 1), for generators
  (piecewise fuel-cost curves included) and candidate technologies
- maintenance cost × `maintenance_cost` (generators, batteries, technologies);
  batteries and battery technologies also × `bat_maintenance_cost`
- start-up cost × `gen_start_up_cost` (generators)
- CO2 cost × `carbon_price`
- flow cost of lines with a per-MW·km cost × `transmission_cost_per_mw_km`
- converter variable cost × `converter_variable_cost`
- penalties: loss of load (and the sectoral penalties derived from it) ×
  `penalty_loss_of_load`, static and dynamic reserve shortfall ×
  `penalty_loss_of_reserve`, curtailment × `penalty_curtailment`, RE
  penetration shortfall × `penalty_fre_penetration`, CO2 budget violation ×
  `penalty_co2_budget_violation`
Investment-cost, decommissioning, fixed O&M (`gen_fixed_cost`, a per-year
cost of the capacity in service, `build_annual_asset_cost`) and discount
multipliers act on the objective instead.
"""
function scenario_master_input(
    input::MasterProblemInput,
    mult::ScenarioMultipliers,
    year_idx::Int
)::MasterProblemInput
    fuel = mult.fuel_cost * mult.fuel_price_growth^(year_idx - 1)
    maint = mult.maintenance_cost
    bat_maint = maint * mult.bat_maintenance_cost
    gens = [with_fields(g; fuel_cost=g.fuel_cost .* fuel,
                        maintenance_cost=g.maintenance_cost .* maint,
                        start_up_cost=g.start_up_cost .* mult.gen_start_up_cost)
            for g in input.generators]
    bats = [with_fields(b; maintenance_cost=b.maintenance_cost .* bat_maint)
            for b in input.batteries]
    techs = [with_fields(t; fuel_cost=t.fuel_cost .* fuel,
                         maintenance_cost=t.maintenance_cost .* maint)
             for t in input.technologies]
    btechs = [with_fields(bt; maintenance_cost=bt.maintenance_cost .* bat_maint)
              for bt in input.battery_technologies]
    m_conv = mult.converter_variable_cost
    network = with_fields(input.network;
        transmission_lines=TransmissionLineData[
            with_fields(l; flow_cost=l.flow_cost * mult.transmission_cost_per_mw_km)
            for l in input.network.transmission_lines],
        acdc_converters=ACDCConverterData[with_fields(c; variable_cost=c.variable_cost * m_conv)
                         for c in input.network.acdc_converters],
        freq_converters=FrequencyConverterData[with_fields(c; variable_cost=c.variable_cost * m_conv)
                         for c in input.network.freq_converters])
    co2_groups = [CO2BudgetGroup(grp.name, grp.nodes, grp.budget,
                                 grp.violation_penalty * mult.penalty_co2_budget_violation)
                  for grp in input.co2_budget_groups]
    # A generator or technology with a fuel-cost curve is charged its
    # segments, not fuel_cost
    scale_curves(curves) = Dict{Int, Dict{Int, Vector{CostSegment}}}(
        u => Dict{Int, Vector{CostSegment}}(
            n => [CostSegment(s.fraction, s.marginal_cost * fuel) for s in segs]
            for (n, segs) in bus_curves)
        for (u, bus_curves) in curves)
    return with_fields(input;
        network=network,
        # M2 (#89): fuel prices × the same factor as the units' fuel_cost
        primary_energy=scale_master_fuel(input.primary_energy, mult, fuel),
        generators=gens, batteries=bats,
        technologies=techs, battery_technologies=btechs,
        gen_cost_curves=scale_curves(input.gen_cost_curves),
        tech_cost_curves=scale_curves(input.tech_cost_curves),
        co2_budget_groups=co2_groups,
        base_demand=input.base_demand .* mult.demand_growth^(year_idx - 1),
        co2_cost=input.co2_cost * mult.carbon_price,
        loss_of_load_penalty=input.loss_of_load_penalty * mult.penalty_loss_of_load,
        loss_of_reserve_static=input.loss_of_reserve_static * mult.penalty_loss_of_reserve,
        loss_of_reserve_dynamic=input.loss_of_reserve_dynamic * mult.penalty_loss_of_reserve,
        curtailment_cost=input.curtailment_cost * mult.penalty_curtailment,
        fre_penetration_loss_penalty=input.fre_penetration_loss_penalty *
                                     mult.penalty_fre_penetration)
end

"""
    build_stochastic_objective!(model, vars, input, scenarios, scenario_costs)

Expected total cost over the scenarios: for each one, its investment cost
(with its investment-cost multipliers), the existing-asset costs and its own
operating cost, all discounted at its discount rate and weighted by its
probability. `scenario_costs[k]` holds scenario k's operating cost per year.
"""
function build_stochastic_objective!(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput,
    scenarios::Vector{Scenario},
    scenario_costs::Vector{Dict{Int, Vector{AffExpr}}}
)
    num_years = length(input.years)
    total_cost = AffExpr(0.0)

    for (k, scenario) in enumerate(scenarios)
        prob = scenario.probability
        mult = scenario.multipliers
        rate = input.discount_rate * mult.discount_rate

        add_to_expression!(total_cost,
            build_investment_cost_expression(vars, input; mult=mult, discount_rate=rate),
            prob)

        for y_idx in 1:num_years
            df = 1.0 / ((1.0 + rate)^(y_idx - 1))
            add_to_expression!(total_cost,
                build_annual_asset_cost(vars, input, y_idx; mult=mult), prob * df)
            for day_cost in get(scenario_costs[k], y_idx, AffExpr[])
                add_to_expression!(total_cost, day_cost, prob * df)
            end
        end
    end

    # Budget and adequacy slacks: first stage, at the base discount rate.
    # Each scenario's RE shortfall is in its operating cost.
    add_to_expression!(total_cost, master_slack_cost(vars, input))

    @objective(model, Min, total_cost)

    return total_cost  # Return for MGA near-optimal constraint
end

# =============================================================================
# NPV-based retirement of existing units
# =============================================================================

"""
    record_master_day!(model, scenario_idx, year_idx, ps_vars, input, hours, scale)

Keep a representative day's (or TSAM period's) operation variables on the
model (`model.ext[:master_days]`), with the input it runs on (a stochastic
scenario's) and `scale`, the factor that takes its per-step MW to the year
(step length × the days it stands for), for the units' NPV.
"""
function record_master_day!(model::Model, scenario_idx::Int, year_idx::Int,
                            ps_vars::PowerSystemVariables, input::MasterProblemInput,
                            hours::Int, scale::Float64)
    push!(get!(() -> Any[], model.ext, :master_days),
          (scenario_idx = scenario_idx, year_idx = year_idx, ps_vars = ps_vars,
           input = input, hours = hours, scale = scale))
    return nothing
end

"""
    add_unit_day_operation!(op, ps_vars, input, year_idx, hours, scale, startup)

Add one solved representative day's operation of each existing generator
and battery to `op` (`UnitOperation`), scaled to the year by `scale`: the
generator's MWh and what the master charges it for them
(`add_gen_step_cost!`; with primary energy on, the fuel it burns at
`master_fuel_unit_price` instead of its `fuel_cost`; its LP-relaxed
start-ups, `startup`), the battery's `add_bat_step_cost!`.
"""
function add_unit_day_operation!(op::UnitOperation, ps_vars::PowerSystemVariables,
                                 input::MasterProblemInput, year_idx::Int, hours::Int,
                                 scale::Float64, startup)
    for (g, gen) in enumerate(input.generators), n in ps_vars.buses_of_gen[g]
        key = (:gen, g, n, year_idx)
        out = 0.0
        cost = AffExpr(0.0)
        for t in 1:hours
            out += value(ps_vars.gen_output[g, n, t])
            add_gen_step_cost!(cost, ps_vars, input, g, n, t)
        end
        c = value(cost)
        # The fuel part (flat fuel_cost or offer curve; tank fuel below) and
        # its yearly growth: a scenario's fuel_price_growth, times the
        # price_growth_rate of the flat fuel_cost's fuel (year_master_input)
        # or of the tank fuel
        fpg = op.multipliers === nothing ? 1.0 : op.multipliers.fuel_price_growth
        has_curve = haskey(ps_vars.gen_seg_output, (g, n))
        fuel = has_curve ? c - (gen.maintenance_cost[n] + co2_cost_per_mwh(input, gen, n)) * out :
                           gen.fuel_cost[n] * out
        fuel_price = master_fuel_unit_price(input, g, n, year_idx)
        if fuel_price !== nothing
            has_curve || (c -= gen.fuel_cost[n] * out; fuel = 0.0)
            c += fuel_price * out
        end
        tank = fuel_price === nothing ? 0.0 : fuel_price * out
        if startup !== nothing && haskey(startup, (g, n))
            c += value(startup[(g, n)])
        end
        op.energy[key] = get(op.energy, key, 0.0) + scale * out
        op.operating_cost[key] = get(op.operating_cost, key, 0.0) + scale * c
        op.fuel_cost[key] = get(op.fuel_cost, key, 0.0) + scale * (fuel + tank)
        if fuel + tank > 0
            growth = tank > 0 ? master_fuel_unit_growth(input, g, n) * fpg : fpg
            flat = has_curve ? fpg : fpg * (1.0 + get(input.fuel_price_growth, gen.fuel, 0.0))
            op.fuel_growth[key] = (fuel * flat + tank * growth) / (fuel + tank)
        end
    end
    for b in eachindex(input.batteries), n in ps_vars.buses_of_bat[b]
        key = (:bat, b, n, year_idx)
        cost = AffExpr(0.0)
        for t in 1:hours
            add_bat_step_cost!(cost, ps_vars, input, b, n, t)
        end
        op.operating_cost[key] = get(op.operating_cost, key, 0.0) + scale * value(cost)
    end
    return op
end

"""
    add_unit_asset_costs!(op, vars, input, mult)

Add each existing unit's share of the solved `build_annual_asset_cost` of
every year (fixed O&M, life extension, decommissioning; `mult` for a
stochastic scenario) to `op.asset_cost`.
"""
function add_unit_asset_costs!(op::UnitOperation, vars::MasterProblemVariables,
                               input::MasterProblemInput,
                               mult::Union{Nothing, ScenarioMultipliers})
    for y_idx in eachindex(input.years)
        by_unit = Dict{Tuple{Symbol,Int,Int}, AffExpr}()
        build_annual_asset_cost(vars, input, y_idx; mult = mult, by_unit = by_unit)
        for ((kind, i, n), e) in by_unit
            op.asset_cost[(kind, i, n, y_idx)] = value(e)
        end
    end
    return op
end

"""
    extract_unit_operation(model, vars, input) -> Vector{UnitOperation}

Each existing unit's operation and costs in the solved master
(`create_master_problem` or `create_stochastic_master_problem`): one
`UnitOperation` per scenario (one for a deterministic master), from the
representative days the model recorded (`record_master_day!`) and its
annual asset costs.
"""
function extract_unit_operation(model::Model, vars::MasterProblemVariables,
                                input::MasterProblemInput)::Vector{UnitOperation}
    days = get(model.ext, :master_days, Any[])
    startup = get(model.ext, :master_startup, nothing)
    scenarios = get(model.ext, :master_scenarios, nothing)
    cases = scenarios === nothing ? [(0, 1.0, nothing)] :
        [(k, sc.probability, sc.multipliers) for (k, sc) in enumerate(scenarios)]
    ops = UnitOperation[]
    for (k, prob, mult) in cases
        rate = mult === nothing ? input.discount_rate : input.discount_rate * mult.discount_rate
        op = UnitOperation(prob, rate; multipliers = mult)
        for d in days
            d.scenario_idx == k || continue
            add_unit_day_operation!(op, d.ps_vars, d.input, d.year_idx, d.hours, d.scale,
                startup === nothing ? nothing : get(startup, d.ps_vars, nothing))
        end
        add_unit_asset_costs!(op, vars, input, mult)
        push!(ops, op)
    end
    return ops
end

# Capacity in service per modelled year of existing unit (kind, index, bus)
# in the plan: generators MW (degraded, life-extended), batteries discharge MW
function unit_capacity_by_year(result::MasterProblemResult, kind::Symbol, i::Int, n::Int,
                               num_years::Int)
    caps = kind === :gen ? result.cumulative_gen_capacity : result.cumulative_bat_power
    return [haskey(caps, y) && haskey(caps[y], i) && n <= length(caps[y][i]) ?
            caps[y][i][n] : 0.0 for y in 1:num_years]
end

"""
    calculate_unit_npv(result, input, unit_type, unit_idx, node; npv_threshold=0.0)

NPV of keeping existing unit `unit_idx` ("generator" or "battery") at bus
`node` (the master's location) in the solved plan `result`, extracted with
`unit_operation = true`. Over the modelled years, discounted as the master
discounts them (year 1 undiscounted):

    generator: Σ_y df_y × (E_y × base_lcoe − C_y − A_y)
    battery:   Σ_y df_y × (r × invest_cost_power × P_y − C_y − A_y)

with `E_y` the MWh the master dispatched it (its representative days
scaled to the year), `C_y` the operating cost the master charges for them,
`A_y` its fixed O&M, life extension and decommissioning in the plan
(`UnitOperation`), `P_y` its discharge power in service and
`r = npv_annual_return_rate`. Degradation and the end of life are in the
plan's capacity and dispatch. A stochastic master's NPV is the
probability-weighted mean over its scenarios (each with its discount rate).

With `extra_years > 0` (a myopic year's master, whose horizon is that year)
the NPV also covers the next `extra_years` years while the unit is still in
service: the last modelled year's operation held at its level, scaled by
the unit's capacity degradation, its fuel part also by the fuel's yearly
growth (`UnitOperation.fuel_growth`); fixed O&M on the degraded capacity
(battery: on its rated power, as the master charges it, and the revenue on
its degraded power); no life extension, so
it retires at the end of its `life_time` and pays its decommissioning then;
discounted on from the last modelled year (`extra_unit_npv`). An
approximation: the later years' dispatch is not modelled.

`recommend_retirement` when the NPV is below `npv_threshold`;
`remaining_lifetime` is the number of years it is in service (modelled and
extra).
"""
function calculate_unit_npv(
    result::MasterProblemResult,
    input::MasterProblemInput,
    unit_type::String,
    unit_idx::Int,
    node::Int;
    npv_threshold::Float64 = 0.0,
    extra_years::Int = 0
)::UnitNPV
    unit_type in ("generator", "battery") || error("Unknown unit type: $unit_type")
    isempty(result.unit_operation) && error(
        "calculate_unit_npv needs the units' operation: extract the result with " *
        "extract_master_solution(model, vars, input; unit_operation = true)")
    num_years = length(input.years)
    kind = unit_type == "generator" ? :gen : :bat
    caps = unit_capacity_by_year(result, kind, unit_idx, node, num_years)
    npv = 0.0
    for op in result.unit_operation
        v = 0.0
        for y in 1:num_years
            key = (kind, unit_idx, node, y)
            revenue = kind === :gen ? get(op.energy, key, 0.0) * input.base_lcoe :
                input.npv_annual_return_rate *
                input.batteries[unit_idx].invest_cost_power[node] * caps[y]
            v += (revenue - get(op.operating_cost, key, 0.0) - get(op.asset_cost, key, 0.0)) /
                 (1.0 + op.discount_rate)^(y - 1)
        end
        extra_years > 0 &&
            (v += extra_unit_npv(op, input, kind, unit_idx, node, caps[end], extra_years)[1])
        npv += op.probability * v
    end
    in_service = Float64(count(>(1e-6), caps))
    extra_years > 0 && (in_service += extra_unit_npv(result.unit_operation[1], input, kind,
        unit_idx, node, caps[end], extra_years)[2])
    return UnitNPV(unit_type, unit_idx, node, input.system_name, npv, in_service,
                   npv < npv_threshold)
end

"""
    extra_unit_npv(op, input, kind, i, n, cap_last, extra_years) -> (npv, years)

The NPV (discounted like the master, from its first year) of existing unit
`(kind, i, n)` over the `extra_years` years after the last modelled one,
and how many of them it is in service, for `calculate_unit_npv`. The last
modelled year's operation (`op`) is held at its level and scaled by the
capacity degradation (and its fuel part by `op.fuel_growth` per year); fixed
O&M on the capacity in service (a battery: on its rated power, as the
master charges it); no life extension: in its first year past
`life_time` (or the first extra year, for a unit the master life-extended)
it retires and pays its decommissioning on the capacity it had.
`cap_last` is its capacity in service in the last modelled year.
"""
function extra_unit_npv(op::UnitOperation, input::MasterProblemInput, kind::Symbol,
                        i::Int, n::Int, cap_last::Float64, extra_years::Int)
    Y = length(input.years)
    cap_last > 1e-6 || return 0.0, 0
    mult = op.multipliers
    m_fixed = mult === nothing ? 1.0 : mult.gen_fixed_cost
    df(y) = 1.0 / (1.0 + op.discount_rate)^(y - 1)
    key = (kind, i, n, Y)
    unit = kind === :gen ? input.generators[i] : input.batteries[i]
    deg = unit.degradation_rate[n]
    age_last = unit.initial_age[n] + (Y - 1)
    within = age_last < unit.life_time[n]
    E = get(op.energy, key, 0.0)
    C = get(op.operating_cost, key, 0.0)
    F = get(op.fuel_cost, key, 0.0)
    growth = get(op.fuel_growth, key, 1.0)
    v, years = 0.0, 0
    for k in 1:extra_years
        age = age_last + k
        if within && age < unit.life_time[n]
            ratio = (1.0 - deg)^k
            if kind === :gen
                cap = om_cap = unit.rated_power[n] * (1.0 - deg)^age
                revenue = E * ratio * input.base_lcoe
                cost = (C - F) * ratio + F * ratio * growth^k
            else
                # Fixed O&M on its rated power, as the master charges it
                cap, om_cap = cap_last * ratio, unit.max_discharge_power[n]
                revenue = input.npv_annual_return_rate * unit.invest_cost_power[n] * cap
                cost = C * ratio
            end
            v += (revenue - cost - unit.fixed_cost[n] * m_fixed * om_cap) * df(Y + k)
            years += 1
        else
            # End of life: decommissioned on the capacity it had
            m_dec = mult === nothing ? 1.0 :
                (kind === :gen ? mult.gen_decommissioning_cost : mult.bat_decommissioning_cost)
            cap = !within ? cap_last :
                kind === :gen ? unit.rated_power[n] * (1.0 - deg)^age : cap_last * (1.0 - deg)^k
            v -= unit.decommissioning_cost[n] * m_dec * cap * df(Y + k)
            break
        end
    end
    return v, years
end

"""
    unit_npvs(result, input; npv_threshold=0.0, extra_years=0) -> Vector{UnitNPV}

`calculate_unit_npv` of every existing generator and battery location in
service in some year of the plan.
"""
function unit_npvs(result::MasterProblemResult, input::MasterProblemInput;
                   npv_threshold::Float64 = 0.0, extra_years::Int = 0)::Vector{UnitNPV}
    out = UnitNPV[]
    num_years = length(input.years)
    for (g, gen) in enumerate(input.generators), n in eachindex(gen.rated_power)
        gen.rated_power[n] > 0 || continue
        any(>(1e-6), unit_capacity_by_year(result, :gen, g, n, num_years)) || continue
        push!(out, calculate_unit_npv(result, input, "generator", g, n;
                                      npv_threshold = npv_threshold, extra_years = extra_years))
    end
    for (b, bat) in enumerate(input.batteries), n in eachindex(bat.max_discharge_power)
        bat.max_discharge_power[n] > 0 || continue
        any(>(1e-6), unit_capacity_by_year(result, :bat, b, n, num_years)) || continue
        push!(out, calculate_unit_npv(result, input, "battery", b, n;
                                      npv_threshold = npv_threshold, extra_years = extra_years))
    end
    return out
end

"""
    get_units_with_negative_npv(result, input; npv_threshold=0.0, extra_years=0) -> Vector{UnitNPV}

The units of `unit_npvs` whose NPV is below `npv_threshold`.
"""
function get_units_with_negative_npv(
    result::MasterProblemResult,
    input::MasterProblemInput;
    npv_threshold::Float64 = 0.0,
    extra_years::Int = 0
)::Vector{UnitNPV}
    return filter(u -> u.recommend_retirement,
                  unit_npvs(result, input; npv_threshold = npv_threshold,
                            extra_years = extra_years))
end

"""
    npv_round_reference_cost(result, input, units; extra_years=0) -> Float64

The total cost a master re-solve that retires `units` (`UnitNPV`) is
compared with, in the objective's units: the objective of the solved plan
`result` (for `input`, extracted with `unit_operation`), less the units' NPV
over the `extra_years` past the master's horizon (`extra_unit_npv`,
probability-weighted over the scenarios; `extra_years` as for
`calculate_unit_npv`).

The objective holds every cost of an existing unit over the modelled years
(its operation, and in `build_annual_asset_cost` its fixed O&M on the
capacity in service, life extension and decommissioning, at the end of its
life or, retired, in the first year), so the costs a retired unit avoids
are in the re-solve's objective. It stops at the master's horizon: a
myopic year's master models that year only, so the units' later years (what
keeping them earns and costs there, as their NPV values them) are counted
here. With `extra_years = 0` it is `result.objective`.
"""
function npv_round_reference_cost(result::MasterProblemResult, input::MasterProblemInput,
                                  units::AbstractVector{UnitNPV};
                                  extra_years::Int = 0)::Float64
    cost = result.objective
    extra_years > 0 || return cost
    Y = length(input.years)
    for u in units
        kind = u.unit_type == "generator" ? :gen : :bat
        cap_last = unit_capacity_by_year(result, kind, u.unit_idx, u.node, Y)[end]
        for op in result.unit_operation
            cost -= op.probability *
                    extra_unit_npv(op, input, kind, u.unit_idx, u.node, cap_last, extra_years)[1]
        end
    end
    return cost
end

"""
    cost_grew(before, after) -> Bool

Whether a re-solve's total cost (`after`) is above `before` beyond the
solver's noise (relative 1e-6).
"""
cost_grew(before::Real, after::Real) = after > before + 1e-6 * abs(before)

# Copy of `v` with entry `n` set to 0
_zero_at(v::AbstractVector, n::Int) = (w = copy(v); n <= length(w) && (w[n] = 0.0); w)

"""
    retire_units(input, units; decommission=true) -> MasterProblemInput

`input` with each unit of `units` (`UnitNPV`) retired for the whole
horizon at its bus: its capacity there is 0 (so no dispatch, no life
extension, no fixed O&M and no end-of-life decommissioning), and with
`decommission` it pays its decommissioning cost once, in the first year, on
its (degraded) capacity then, as the master charges a unit retiring at the
end of its life (`npv_retired_units`, `build_annual_asset_cost`). A myopic
year passes `decommission = false` for the units an earlier year retired.
"""
function retire_units(input::MasterProblemInput, units::AbstractVector{UnitNPV};
                      decommission::Bool = true)::MasterProblemInput
    isempty(units) && return input
    gens = copy(input.generators)
    bats = copy(input.batteries)
    retired = copy(input.npv_retired_units)
    for u in units
        n = u.node
        if u.unit_type == "generator"
            gen = gens[u.unit_idx]
            mw = gen.rated_power[n] * (1.0 - gen.degradation_rate[n])^gen.initial_age[n]
            gens[u.unit_idx] = with_fields(gen;
                rated_power = _zero_at(gen.rated_power, n),
                reservoir_invest_max = _zero_at(gen.reservoir_invest_max, n))
        elseif u.unit_type == "battery"
            bat = bats[u.unit_idx]
            mw = bat.max_discharge_power[n] * battery_degradation_factor(bat, n, 1)
            bats[u.unit_idx] = with_fields(bat;
                max_discharge_power = _zero_at(bat.max_discharge_power, n),
                max_charge_power = _zero_at(bat.max_charge_power, n),
                capacity = _zero_at(bat.capacity, n))
        else
            error("Unknown unit type: $(u.unit_type)")
        end
        push!(retired, RetiredUnit(u.unit_type, u.unit_idx, n, decommission ? mw : 0.0))
    end
    return with_fields(input; generators = gens, batteries = bats,
                       npv_retired_units = retired)
end

"""
    solve_with_npv_iteration(input; max_iterations=5, npv_threshold=0.0,
                             use_representative_days=true, solver=:monolithic,
                             extra_years=0, benders_kwargs...) -> NPVIterationResult

Solve the master and retire the existing units that do not pay for
themselves: compute each unit's NPV in the solved plan
(`calculate_unit_npv`), retire those below `npv_threshold` for the whole
horizon (`retire_units`), re-solve, and repeat until no new unit falls below
it or after `max_iterations` rounds of retirements. A retired unit stays
retired. `solver = :benders` solves each master by Benders decomposition
(`run_benders_decomposition` with `benders_kwargs`); `extra_years` is
passed to `calculate_unit_npv` (a myopic year's master).

Each master is solved with `optimize_master!` (a retry with the solver's
numerical parameters, a warning for curtailment above the annual limits).
When a re-solve is not optimal, curtails more above the annual curtailment
limit than the plan before (`curtailment_excess_mwh`: retiring the store
that held the limit; the limit was a hard constraint, under which such a
re-solve was infeasible), or costs more in total than the plan before
(`cost_grew` of its objective over `npv_round_reference_cost`: retiring the
units saved less than it cost elsewhere), the round that led to it is
undone: the result is the last optimal plan with the retirements made up to
it, and a warning names the units of the undone round and the reason.

`iterations` counts the master solves; `npv_history` holds, per solve, the
NPV of every unit evaluated (`"generator_g_bus"` / `"battery_b_bus"`);
`forced_retirements` the units retired, in order; `final_result` the last
optimal plan. Errors when the first solve is not optimal.
"""
function solve_with_npv_iteration(
    input::MasterProblemInput;
    max_iterations::Int = 5,
    npv_threshold::Float64 = 0.0,
    use_representative_days::Bool = true,
    solver::Symbol = :monolithic,
    extra_years::Int = 0,
    benders_kwargs...
)::NPVIterationResult
    optimal(st) = st in (MOI.OPTIMAL, MOI.LOCALLY_SOLVED)
    # (plan or nothing when not optimal, termination status, curtailment
    # above the annual limits in MWh/yr: Benders holds them hard)
    function solve_once(inp)
        if solver === :benders
            sol = run_benders_decomposition(inp; use_representative_days = use_representative_days,
                                            record_unit_operation = true,
                                            benders_kwargs...).solution
            return (optimal(sol.status) ? sol : nothing), sol.status, 0.0
        end
        model, vars, _ = create_master_problem(inp; use_representative_days = use_representative_days)
        optimize_master!(model)
        st = termination_status(model)
        optimal(st) || return nothing, st, 0.0
        return extract_master_solution(model, vars, inp; unit_operation = true), st,
               curtailment_excess_mwh(model)
    end
    describe(units) = join(["$(u.unit_type) $(u.unit_idx) at bus $(u.node) " *
                            "(NPV $(round(u.npv, sigdigits = 4)))" for u in units], ", ")

    retired = UnitNPV[]
    history = Dict{String, Float64}[]
    current = input
    previous = nothing
    last_round = 0
    iter = 0
    prev_excess = 0.0
    prev_cost = 0.0
    while true
        iter += 1
        result, status, excess = solve_once(current)
        why = if result === nothing
            previous === nothing &&
                error("NPV retirement: the first master solve is not optimal ($(status))")
            "is not optimal ($(status))"
        elseif previous !== nothing && excess_grew(prev_excess, excess)
            "curtails $(round(excess / 1e3; sigdigits = 4)) GWh/yr above the annual " *
            "curtailment limit (the plan before: $(round(prev_excess / 1e3; sigdigits = 4)))"
        elseif previous !== nothing && cost_grew(prev_cost, result.objective)
            "raises the plan's total cost to $(round(result.objective; sigdigits = 8)) M\$ " *
            "(the plan before: $(round(prev_cost; sigdigits = 8)) M\$)"
        else
            nothing
        end
        if why !== nothing
            # Undo the last round: the previous plan still has its units
            undone = retired[end - last_round + 1:end]
            @warn "NPV retirement: the master re-solve after retiring " * describe(undone) *
                  " $(why); keeping these units and the last optimal plan"
            return NPVIterationResult(iter, false, previous,
                                      retired[1:end - last_round], history)
        end
        prev_excess = excess
        npvs = unit_npvs(result, current; npv_threshold = npv_threshold,
                         extra_years = extra_years)
        push!(history, Dict("$(u.unit_type)_$(u.unit_idx)_$(u.node)" => u.npv for u in npvs))
        flagged = filter(u -> u.recommend_retirement, npvs)
        if isempty(flagged)
            @info "NPV retirement: no unit below the threshold after solve $(iter)"
            return NPVIterationResult(iter, true, result, retired, history)
        end
        if iter > max_iterations
            @info "NPV retirement: $(length(flagged)) unit(s) still below the " *
                  "threshold after $(max_iterations) round(s); stopping"
            return NPVIterationResult(iter, false, result, retired, history)
        end
        @info "NPV retirement round $(iter): retiring $(length(flagged)) unit(s): " *
              describe(flagged)
        append!(retired, flagged)
        last_round = length(flagged)
        prev_cost = npv_round_reference_cost(result, current, flagged; extra_years = extra_years)
        current = retire_units(current, flagged)
        previous = result
    end
end

# =============================================================================
# Infeasibility Diagnostics
# =============================================================================

"""
    diagnose_infeasibility(model::Model, vars::MasterProblemVariables, input::MasterProblemInput)

Diagnose model infeasibility by checking slack variable values.

Returns Dict with constraint category => violation amount.
"""
function diagnose_infeasibility(
    model::Model,
    vars::MasterProblemVariables,
    input::MasterProblemInput
)::Dict{String, Float64}
    violations = Dict{String, Float64}()

    num_years = length(input.years)
    n_buses = input.network.num_buses

    # Check if model is solved
    status = termination_status(model)
    if status == MOI.OPTIMAL || status == MOI.LOCALLY_SOLVED
        # Check slack values
        total_re_slack = 0.0
        total_budget_slack = 0.0
        total_capacity_slack = 0.0

        # Renewable energy missing from the RE targets (MWh/year), per
        # system and year (summed over the scenarios of a stochastic master)
        for ((_, s, y_idx), v) in vars.slack_re_target
            re_slack_val = value(v)
            if re_slack_val > 1e-6
                total_re_slack += re_slack_val
                key = "re_target_s$(s)_y$(y_idx)"
                violations[key] = get(violations, key, 0.0) + re_slack_val
            end
        end
        # Renewable energy over the RE growth cap (MWh/year)
        total_re_cap_slack = 0.0
        for ((_, s, y_idx), v) in get(model.ext, :re_cap_slack, Dict{Tuple{Int,Int,Int}, VariableRef}())
            val = value(v)
            if val > 1e-6
                total_re_cap_slack += val
                key = "re_max_increment_s$(s)_y$(y_idx)"
                violations[key] = get(violations, key, 0.0) + val
            end
        end

        for y_idx in 1:num_years
            budget_slack_val = value(vars.slack_budget[y_idx])
            if budget_slack_val > 1e-6
                total_budget_slack += budget_slack_val
                violations["budget_y$(y_idx)"] = budget_slack_val
            end

            for ni in 1:input.network.num_nodes
                cap_slack_val = value(vars.slack_capacity[(y_idx, ni)])
                if cap_slack_val > 1e-6
                    total_capacity_slack += cap_slack_val
                    violations["capacity_y$(y_idx)_node$(ni)"] = cap_slack_val
                end
            end
        end

        violations["total_re_slack"] = total_re_slack
        violations["total_re_cap_slack"] = total_re_cap_slack
        violations["total_budget_slack"] = total_budget_slack
        violations["total_capacity_slack"] = total_capacity_slack
    else
        violations["model_status"] = -1.0
        violations["status_code"] = Float64(Int(status))
    end

    return violations
end

"""
    log_solution_summary(result::MasterProblemResult, input::MasterProblemInput; ev_demand::Union{Dict{Int, Float64}, Nothing}=nothing)

Generate a formatted solution summary string with detailed debug information.

Args:
    result: MasterProblemResult with optimization results
    input: MasterProblemInput with problem configuration
    ev_demand: Optional dict mapping year_idx => total EV demand (MWh) for that year
"""
function log_solution_summary(
    result::MasterProblemResult,
    input::MasterProblemInput;
    ev_demand::Union{Dict{Int, Float64}, Nothing}=nothing
)::String
    lines = String[]
    num_years = length(input.years)
    n_buses = input.network.num_buses
    b2n = input.network.bus_to_node
    n_gen = length(input.generators)
    n_bat = length(input.batteries)
    system_name = input.system_name

    push!(lines, "=" ^ 100)
    push!(lines, "MASTER PROBLEM SOLUTION SUMMARY - SYSTEM: $(uppercase(system_name))")
    push!(lines, "=" ^ 100)

    # ==========================================================================
    # Basic Status
    # ==========================================================================
    push!(lines, "\n[STATUS - $(system_name)]")
    push!(lines, "  Termination Status: $(result.status)")
    push!(lines, "  Total Objective (NPV): \$$(round(result.objective, digits=2)) M")
    push!(lines, "  Solve Time: $(round(result.solve_time, digits=1)) s")

    # ==========================================================================
    # DEMAND AND RE TARGET SUMMARY TABLE (Years in rows, Demand/Target in columns)
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[DEMAND AND RE TARGET SUMMARY - $(system_name)]")
    push!(lines, "-" ^ 100)

    targets = calculate_target_ratios(input)
    base_demand_total = sum(input.base_demand)
    hours = size(input.base_demand, 1)

    # Table header
    push!(lines, "┌─────────┬──────────────────┬──────────────────┬──────────────────┬────────────┬────────────┬────────────┐")
    push!(lines, "│  Year   │  Base Demand     │  EV Demand       │  Total Demand    │ Target RE% │Achieved RE%│   Diff     │")
    push!(lines, "│         │     (GWh)        │     (GWh)        │     (GWh)        │            │            │            │")
    push!(lines, "├─────────┼──────────────────┼──────────────────┼──────────────────┼────────────┼────────────┼────────────┤")

    for y_idx in 1:num_years
        year = input.years[y_idx]
        growth_factor = (1 + input.demand_growth) ^ (y_idx - 1)
        base_dem_gwh = base_demand_total * growth_factor / 1000.0  # Convert MWh to GWh

        # EV demand for this year (if provided)
        ev_dem_gwh = if ev_demand !== nothing && haskey(ev_demand, y_idx)
            ev_demand[y_idx] / 1000.0
        else
            0.0
        end

        total_dem_gwh = base_dem_gwh + ev_dem_gwh

        target_pct = targets[(1, y_idx)] * 100
        achieved_pct = result.re_penetration_by_year[y_idx] * 100
        diff_pct = achieved_pct - target_pct
        diff_str = diff_pct >= 0 ? "+$(round(diff_pct, digits=1))" : "$(round(diff_pct, digits=1))"

        push!(lines, "│ $(rpad(year, 7)) │ $(lpad(round(base_dem_gwh, digits=1), 16)) │ $(lpad(round(ev_dem_gwh, digits=1), 16)) │ $(lpad(round(total_dem_gwh, digits=1), 16)) │ $(lpad(round(target_pct, digits=1), 10)) │ $(lpad(round(achieved_pct, digits=1), 10)) │ $(lpad(diff_str, 10)) │")
    end
    push!(lines, "└─────────┴──────────────────┴──────────────────┴──────────────────┴────────────┴────────────┴────────────┘")

    # ==========================================================================
    # Installed Capacity (Initial) - TABLE FORMAT (Nodes in rows, Technologies in columns)
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[INITIAL INSTALLED CAPACITY - $(system_name)]")
    push!(lines, "-" ^ 100)

    # Collect all technology names
    gen_names = [input.generators[g].name for g in 1:n_gen]
    bat_names = [input.batteries[b].name for b in 1:n_bat]

    if n_gen > 0 || n_bat > 0
        col_width = max(12, maximum(vcat(length.(gen_names), length.(bat_names), [8])) + 2)

        # Header row for generators
        push!(lines, "\n  GENERATORS (MW):")
        header = "    " * rpad("Node", 10)
        for name in gen_names
            header *= rpad(name, col_width)
        end
        header *= rpad("TOTAL", col_width)
        push!(lines, header)
        push!(lines, "    " * "-" ^ (10 + col_width * (n_gen + 1)))

        # Data rows for generators
        node_totals = zeros(n_buses)
        for n in 1:n_buses
            row = "    " * rpad("Node $n", 10)
            node_total = 0.0
            for g in 1:n_gen
                gen = input.generators[g]
                val = n <= length(gen.rated_power) ? gen.rated_power[n] : 0.0
                node_total += val
                row *= rpad(val > 0 ? "$(round(val, digits=1))" : "-", col_width)
            end
            node_totals[n] = node_total
            row *= rpad("$(round(node_total, digits=1))", col_width)
            push!(lines, row)
        end

        # Total row for generators
        push!(lines, "    " * "-" ^ (10 + col_width * (n_gen + 1)))
        total_row = "    " * rpad("TOTAL", 10)
        for g in 1:n_gen
            total_row *= rpad("$(round(sum(input.generators[g].rated_power), digits=1))", col_width)
        end
        total_row *= rpad("$(round(sum(node_totals), digits=1))", col_width)
        push!(lines, total_row)

        # Battery table
        if n_bat > 0
            push!(lines, "\n  BATTERIES (MW / MWh):")
            header = "    " * rpad("Node", 10)
            for name in bat_names
                header *= rpad(name * " MW", col_width)
                header *= rpad(name * " MWh", col_width)
            end
            push!(lines, header)
            push!(lines, "    " * "-" ^ (10 + col_width * 2 * n_bat))

            for n in 1:n_buses
                row = "    " * rpad("Node $n", 10)
                for b in 1:n_bat
                    bat = input.batteries[b]
                    pow = n <= length(bat.max_discharge_power) ? bat.max_discharge_power[n] : 0.0
                    cap = n <= length(bat.capacity) ? bat.capacity[n] : 0.0
                    row *= rpad(pow > 0 ? "$(round(pow, digits=1))" : "-", col_width)
                    row *= rpad(cap > 0 ? "$(round(cap, digits=1))" : "-", col_width)
                end
                push!(lines, row)
            end

            # Total row for batteries
            push!(lines, "    " * "-" ^ (10 + col_width * 2 * n_bat))
            total_row = "    " * rpad("TOTAL", 10)
            for b in 1:n_bat
                total_row *= rpad("$(round(sum(input.batteries[b].max_discharge_power), digits=1))", col_width)
                total_row *= rpad("$(round(sum(input.batteries[b].capacity), digits=1))", col_width)
            end
            push!(lines, total_row)
        end
    end

    # Summary
    renewable_capacity = n_gen > 0 ? sum(sum(input.generators[g].rated_power) for g in 1:n_gen if input.generators[g].type == "Renewable"; init=0.0) : 0.0
    conventional_capacity = n_gen > 0 ? sum(sum(input.generators[g].rated_power) for g in 1:n_gen if input.generators[g].type != "Renewable"; init=0.0) : 0.0
    total_bat_power = n_bat > 0 ? sum(sum(input.batteries[b].max_discharge_power) for b in 1:n_bat) : 0.0
    total_bat_energy = n_bat > 0 ? sum(sum(input.batteries[b].capacity) for b in 1:n_bat) : 0.0

    push!(lines, "\n  SUMMARY:")
    push!(lines, "    Renewable: $(round(renewable_capacity, digits=1)) MW")
    push!(lines, "    Conventional: $(round(conventional_capacity, digits=1)) MW")
    push!(lines, "    Battery: $(round(total_bat_power, digits=1)) MW / $(round(total_bat_energy, digits=1)) MWh")

    # ==========================================================================
    # Cumulative Capacity by Year (including investments)
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[CUMULATIVE RENEWABLE CAPACITY BY YEAR - $(system_name)]")
    push!(lines, "-" ^ 100)
    push!(lines, "  Year    Existing RE      RE Invested      Total RE (MW)")
    push!(lines, "-" ^ 60)

    n_tech = length(input.technologies)
    for y_idx in 1:num_years
        year = input.years[y_idx]
        existing_re = 0.0
        invested_re = 0.0

        for g in 1:n_gen
            gen = input.generators[g]
            if gen.type == "Renewable"
                existing_re += sum(gen.rated_power)
            end
        end

        # Cumulative technology investment up to this year
        for t in 1:n_tech
            tech = input.technologies[t]
            if tech.type == "Renewable"
                for prev_y in 1:y_idx
                    if haskey(result.tech_investment, prev_y) && haskey(result.tech_investment[prev_y], t)
                        invested_re += sum(result.tech_investment[prev_y][t])
                    end
                end
            end
        end

        total_re = existing_re + invested_re
        push!(lines, "  $(rpad(year, 6))  $(rpad(round(existing_re, digits=1), 15))  $(rpad(round(invested_re, digits=1), 15))  $(round(total_re, digits=1))")
    end

    # ==========================================================================
    # Investment Details by Year - TABLE FORMAT (Nodes in rows, Technologies in columns)
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[INVESTMENT DETAILS BY YEAR - $(system_name)]")
    push!(lines, "-" ^ 100)

    annuities = annuity_costs(result, num_years)
    for y_idx in 1:num_years
        year = input.years[y_idx]
        total_inv = result.total_investment_by_year[y_idx]
        push!(lines, "\n  Year $(year): Total Investment = \$$(round(total_inv, digits=2)) M " *
                     "(overnight; annuities paid this year: \$$(round(annuities[y_idx], digits=2)) M)")

        # Collect all technologies with investments this year
        tech_names = String[]
        tech_indices = Int[]
        for t in 1:n_tech
            if haskey(result.tech_investment[y_idx], t) && sum(result.tech_investment[y_idx][t]) > 0.1
                push!(tech_names, input.technologies[t].name)
                push!(tech_indices, t)
            end
        end

        bat_tech_names = String[]
        bat_tech_indices = Int[]
        n_bat_tech = length(input.battery_technologies)
        for bt in 1:n_bat_tech
            has_pow = haskey(result.bat_tech_power_investment[y_idx], bt) && sum(result.bat_tech_power_investment[y_idx][bt]) > 0.1
            has_cap = haskey(result.bat_tech_capacity_investment[y_idx], bt) && sum(result.bat_tech_capacity_investment[y_idx][bt]) > 0.1
            if has_pow || has_cap
                push!(bat_tech_names, input.battery_technologies[bt].name)
                push!(bat_tech_indices, bt)
            end
        end

        if isempty(tech_names) && isempty(bat_tech_names)
            push!(lines, "    No investments this year")
            continue
        end

        # Build header with technology names (columns)
        all_tech_labels = vcat(tech_names, [n * " (MW)" for n in bat_tech_names], [n * " (MWh)" for n in bat_tech_names])
        col_width = max(12, maximum(length.(all_tech_labels)) + 2)

        # Header row
        header = "    " * rpad("Node", 10)
        for name in tech_names
            header *= rpad(name, col_width)
        end
        for name in bat_tech_names
            header *= rpad(name * " (MW)", col_width)
        end
        for name in bat_tech_names
            header *= rpad(name * " (MWh)", col_width)
        end
        push!(lines, header)
        push!(lines, "    " * "-" ^ (10 + col_width * (length(tech_names) + 2 * length(bat_tech_names))))

        # Data rows (one per node)
        for n in 1:n_buses
            row = "    " * rpad("Node $n", 10)

            # Technology investments for this node
            for t in tech_indices
                tech_inv = result.tech_investment[y_idx][t]
                val = length(tech_inv) >= n ? tech_inv[n] : 0.0
                row *= rpad(val > 0.1 ? "$(round(val, digits=1))" : "-", col_width)
            end

            # Battery technology power investments for this node
            for bt in bat_tech_indices
                bat_pow = result.bat_tech_power_investment[y_idx][bt]
                val = length(bat_pow) >= n ? bat_pow[n] : 0.0
                row *= rpad(val > 0.1 ? "$(round(val, digits=1))" : "-", col_width)
            end

            # Battery technology capacity investments for this node
            for bt in bat_tech_indices
                bat_cap = result.bat_tech_capacity_investment[y_idx][bt]
                val = length(bat_cap) >= n ? bat_cap[n] : 0.0
                row *= rpad(val > 0.1 ? "$(round(val, digits=1))" : "-", col_width)
            end

            push!(lines, row)
        end

        # Total row
        total_row = "    " * rpad("TOTAL", 10)
        for t in tech_indices
            total_row *= rpad("$(round(sum(result.tech_investment[y_idx][t]), digits=1))", col_width)
        end
        for bt in bat_tech_indices
            total_row *= rpad("$(round(sum(result.bat_tech_power_investment[y_idx][bt]), digits=1))", col_width)
        end
        for bt in bat_tech_indices
            total_row *= rpad("$(round(sum(result.bat_tech_capacity_investment[y_idx][bt]), digits=1))", col_width)
        end
        push!(lines, "    " * "-" ^ (10 + col_width * (length(tech_names) + 2 * length(bat_tech_names))))
        push!(lines, total_row)
    end

    # ==========================================================================
    # Operational Cost by Year
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[OPERATIONAL COST BY YEAR - $(system_name)]")
    push!(lines, "-" ^ 100)
    push!(lines, "  Year    Operational Cost     Cumulative NPV")
    push!(lines, "-" ^ 50)
    cumulative_npv = 0.0
    for y_idx in 1:num_years
        year = input.years[y_idx]
        op_cost = result.total_operational_cost_by_year[y_idx]
        discount_factor = 1.0 / ((1.0 + input.discount_rate) ^ (y_idx - 1))
        discounted = op_cost * discount_factor
        cumulative_npv += discounted + annuities[y_idx] * discount_factor
        push!(lines, "  $(rpad(year, 6))  \$$(rpad(round(op_cost, digits=2), 18))M  \$$(round(cumulative_npv, digits=2))M")
    end

    # ==========================================================================
    # Retirement Summary (unified: capacity expires at lifetime)
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[RETIREMENT STATUS - $(system_name)]")
    push!(lines, "-" ^ 100)

    # Collect generators with capacity that will retire during the horizon
    retirement_gens = Set{Int}()
    for g in 1:n_gen
        gen = input.generators[g]
        for n in 1:n_buses
            if gen.rated_power[n] > 0
                initial_age = gen.initial_age[n]
                lifetime = gen.life_time[n]
                # Check if unit retires during the planning horizon
                for y_idx in 1:num_years
                    age = initial_age + (y_idx - 1)
                    if age >= lifetime
                        push!(retirement_gens, g)
                        break
                    end
                end
            end
        end
    end

    if isempty(retirement_gens)
        push!(lines, "  No retirements during planning horizon")
    else
        # Collect tech names
        tech_names = String[]
        tech_indices = Int[]
        for g in sort(collect(retirement_gens))
            push!(tech_names, input.generators[g].name)
            push!(tech_indices, g)
        end

        col_width = max(15, maximum(length.(tech_names)) + 2)

        # Header
        header = "    " * rpad("Year", 10)
        for name in tech_names
            header *= rpad(name, col_width)
        end
        push!(lines, header)
        push!(lines, "    " * "-" ^ (10 + col_width * length(tech_names)))

        # One row per year showing status
        for y_idx in 1:num_years
            year = input.years[y_idx]
            row = "    " * rpad("$(year)", 10)
            for g in tech_indices
                gen = input.generators[g]
                # Sum capacity across nodes for this tech
                active_cap = 0.0
                retired_cap = 0.0
                for n in 1:n_buses
                    if gen.rated_power[n] > 0
                        age = gen.initial_age[n] + (y_idx - 1)
                        if age < gen.life_time[n]
                            deg = (1.0 - gen.degradation_rate[n]) ^ age
                            active_cap += gen.rated_power[n] * deg
                        else
                            retired_cap += gen.rated_power[n]
                        end
                    end
                end
                if retired_cap > 0 && active_cap == 0
                    row *= rpad("RETIRED", col_width)
                elseif retired_cap > 0
                    row *= rpad("PARTIAL", col_width)
                else
                    row *= rpad("ACTIVE", col_width)
                end
            end
            push!(lines, row)
        end
    end

    # ==========================================================================
    # Investment Retirement Summary
    # ==========================================================================
    push!(lines, "\n" * "-" ^ 100)
    push!(lines, "[INVESTMENT RETIREMENT STATUS - $(system_name)]")
    push!(lines, "-" ^ 100)

    # Collect investments that will retire during the horizon
    investment_retirements = Vector{NamedTuple{(:tech, :inv_year, :retire_year, :capacity, :type), Tuple{String, Int, Int, Float64, String}}}()

    # Technology investments
    for t in 1:n_tech
        tech = input.technologies[t]
        for inv_year_idx in 1:num_years
            inv_year = input.years[inv_year_idx]
            for n in 1:n_buses
                if haskey(result.tech_investment[inv_year_idx], t)
                    inv_cap = result.tech_investment[inv_year_idx][t][n]
                    if inv_cap > 0.01  # Threshold to avoid numerical noise
                        lifetime = tech.life_time[n]
                        retire_year_idx = inv_year_idx + Int(floor(lifetime))
                        if retire_year_idx <= num_years
                            retire_year = input.years[retire_year_idx]
                            push!(investment_retirements, (
                                tech = tech.name,
                                inv_year = inv_year,
                                retire_year = retire_year,
                                capacity = inv_cap,
                                type = "technology"
                            ))
                        end
                    end
                end
            end
        end
    end

    # Battery technology investments
    n_bat_tech = length(input.battery_technologies)
    for bt in 1:n_bat_tech
        bat_tech = input.battery_technologies[bt]
        for inv_year_idx in 1:num_years
            inv_year = input.years[inv_year_idx]
            for n in 1:n_buses
                if haskey(result.bat_tech_capacity_investment[inv_year_idx], bt)
                    inv_cap = result.bat_tech_capacity_investment[inv_year_idx][bt][n]
                    if inv_cap > 0.01  # Threshold to avoid numerical noise
                        lifetime = bat_tech.life_time[n]
                        retire_year_idx = inv_year_idx + Int(floor(lifetime))
                        if retire_year_idx <= num_years
                            retire_year = input.years[retire_year_idx]
                            push!(investment_retirements, (
                                tech = bat_tech.name,
                                inv_year = inv_year,
                                retire_year = retire_year,
                                capacity = inv_cap,
                                type = "battery_technology"
                            ))
                        end
                    end
                end
            end
        end
    end

    if isempty(investment_retirements)
        push!(lines, "  No investment retirements during planning horizon")
        push!(lines, "  (Investments made will outlive the simulation period)")
    else
        # Sort by retirement year
        sort!(investment_retirements, by = x -> (x.retire_year, x.inv_year))

        # Header
        push!(lines, "    " * rpad("Technology", 20) * rpad("Invested", 12) * rpad("Retires", 12) * rpad("Capacity", 15) * "Type")
        push!(lines, "    " * "-" ^ 70)

        for ret in investment_retirements
            cap_str = ret.type == "battery" ? "$(round(ret.capacity, digits=1)) MWh" : "$(round(ret.capacity, digits=1)) MW"
            push!(lines, "    " * rpad(ret.tech, 20) * rpad("$(ret.inv_year)", 12) * rpad("$(ret.retire_year)", 12) * rpad(cap_str, 15) * ret.type)
        end

        # Summary by year
        push!(lines, "\n    Retirement summary by year:")
        years_with_retirements = unique([r.retire_year for r in investment_retirements])
        for yr in sort(years_with_retirements)
            yr_retirements = filter(r -> r.retire_year == yr, investment_retirements)
            gen_cap = sum([r.capacity for r in yr_retirements if r.type == "generator"], init=0.0)
            bat_cap = sum([r.capacity for r in yr_retirements if r.type == "battery"], init=0.0)
            summary = "      $(yr): "
            parts = String[]
            if gen_cap > 0
                push!(parts, "$(round(gen_cap, digits=1)) MW generators")
            end
            if bat_cap > 0
                push!(parts, "$(round(bat_cap, digits=1)) MWh batteries")
            end
            push!(lines, summary * join(parts, ", "))
        end
    end

    # ==========================================================================
    # Debug: Objective Function Components
    # ==========================================================================
    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "[OBJECTIVE FUNCTION BREAKDOWN - $(system_name)]")
    push!(lines, "-" ^ 100)
    total_investment = sum(result.total_investment_by_year)
    total_operational = sum(result.total_operational_cost_by_year)
    push!(lines, "  Total Investment (undiscounted, overnight): \$$(round(total_investment, digits=2)) M")
    push!(lines, "  Total Investment Annuities (undiscounted): \$$(round(sum(annuities), digits=2)) M")
    push!(lines, "  Total Operational (undiscounted): \$$(round(total_operational, digits=2)) M")
    push!(lines, "  NPV Objective: \$$(round(result.objective, digits=2)) M")

    # Discounted investment annuities + operating cost; the rest of the
    # objective is the annual cost of the assets in service (fixed O&M, life
    # extension, decommissioning) and the budget and adequacy slacks
    calculated_npv = 0.0
    for y_idx in 1:num_years
        df = 1.0 / ((1.0 + input.discount_rate) ^ (y_idx - 1))
        calculated_npv += (annuities[y_idx] + result.total_operational_cost_by_year[y_idx]) * df
    end
    push!(lines, "  NPV of investment + operation: \$$(round(calculated_npv, digits=2)) M")
    push!(lines, "  Asset costs and slack penalties (NPV): \$$(round(result.objective - calculated_npv, digits=2)) M")

    push!(lines, "\n" * "=" ^ 100)
    push!(lines, "END OF SOLUTION SUMMARY FOR SYSTEM: $(uppercase(system_name))")
    push!(lines, "=" ^ 100)

    return join(lines, "\n")
end

# =============================================================================
# Solution Export
# =============================================================================

"""
    export_solution_to_dict(result::MasterProblemResult, input::MasterProblemInput)

Export solution to a dictionary for JSON/HDF5 serialization.
"""
function export_solution_to_dict(
    result::MasterProblemResult,
    input::MasterProblemInput
)::Dict{String, Any}
    export_dict = Dict{String, Any}()

    export_dict["status"] = string(result.status)
    export_dict["objective"] = result.objective
    export_dict["solve_time"] = result.solve_time
    export_dict["years"] = input.years

    # Technology investment decisions
    tech_inv_export = Dict{String, Any}()
    for (y_idx, tech_dict) in result.tech_investment
        year_str = string(input.years[y_idx])
        tech_inv_export[year_str] = Dict{String, Vector{Float64}}()
        for (t, vals) in tech_dict
            tech_inv_export[year_str][input.technologies[t].name] = vals
        end
    end
    export_dict["tech_investment"] = tech_inv_export

    # Battery technology investment decisions
    bat_tech_pow_export = Dict{String, Any}()
    bat_tech_cap_export = Dict{String, Any}()
    for (y_idx, bt_dict) in result.bat_tech_power_investment
        year_str = string(input.years[y_idx])
        bat_tech_pow_export[year_str] = Dict{String, Vector{Float64}}()
        bat_tech_cap_export[year_str] = Dict{String, Vector{Float64}}()
        for (bt, vals) in bt_dict
            bat_tech_pow_export[year_str][input.battery_technologies[bt].name] = vals
            bat_tech_cap_export[year_str][input.battery_technologies[bt].name] = result.bat_tech_capacity_investment[y_idx][bt]
        end
    end
    export_dict["bat_tech_power_investment"] = bat_tech_pow_export
    export_dict["bat_tech_capacity_investment"] = bat_tech_cap_export

    # Share in service (1.0 = active or fully life-extended, 0.0 = retired)
    gen_life_export = Dict{String, Any}()
    for (y_idx, gen_dict) in result.gen_life_extension
        year_str = string(input.years[y_idx])
        gen_life_export[year_str] = Dict{String, Vector{Float64}}()
        for (g, vals) in gen_dict
            gen_life_export[year_str][input.generators[g].name] = vals
        end
    end
    export_dict["retirement_status"] = gen_life_export

    # Converter investment decisions (MW built that year, by converter name)
    convs = Dict(master_converters(input.network))
    export_dict["converter_investment"] = Dict{String, Any}(
        string(input.years[y_idx]) => Dict{String, Float64}(
            convs[key].name => v for (key, v) in conv_dict)
        for (y_idx, conv_dict) in result.converter_investment)

    # Summary metrics
    export_dict["total_investment_by_year"] = result.total_investment_by_year
    export_dict["annuity_cost_by_year"] = annuity_costs(result, length(input.years))
    export_dict["total_operational_cost_by_year"] = result.total_operational_cost_by_year
    export_dict["re_penetration_by_year"] = result.re_penetration_by_year
    export_dict["re_penetration_by_system"] = Dict{String, Any}(
        k => collect(v) for (k, v) in result.re_penetration_by_system
    )

    return export_dict
end
