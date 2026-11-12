"""
types.jl - Core type definitions for ESFEX optimization models

Defines Julia structs that mirror the Python Pydantic models for
seamless data transfer between Python and Julia.

All vectors are indexed per-node unless otherwise specified.
Matrices are indexed as [time, node] (Julia column-major order).
"""

# UNIT CONVENTION: All monetary values in this module are in M$ (millions of USD).
# Physical units: MW (power), MWh (energy), tonnes (CO2), km, etc.
# The Python adapter layer (converters.py) scales user-facing $ values by 1e-6.

# Note: JuMP and MOI are imported from the parent module (ESFEX.jl)
# These will be available when types.jl is included from ESFEX.jl

"""Hours in every modelled year. Leap years are modelled as 8760 h too."""
const HOURS_STD_YEAR = 8760

"""Hours of a modelled year: always `HOURS_STD_YEAR`. Demand and profiles come
in 8760-h blocks (Feb 29 is dropped on input), so a leap year is not longer."""
hours_in_year(year::Int) = HOURS_STD_YEAR

# =============================================================================
# PWL (Piecewise Linear) Transmission Loss Model
# =============================================================================

"""
    PWLLossSegments

Pre-computed piecewise linear loss segments for transmission lines.

Approximates quadratic losses P_loss(f) = g_l × f² using N linear segments.
For each line l and segment k, the marginal loss slope is m_k = g_l × (f_k + f_{k-1}).
Convexity guarantees correct LP behavior without binary variables.
"""
struct PWLLossSegments
    num_segments::Int                       # N segments per line
    segment_widths::Vector{Vector{Float64}} # [line][k] = Δf_k = f_max/N
    slopes::Vector{Vector{Float64}}         # [line][k] = m_k = g_l × (2k-1) × Δf
    conductances::Vector{Float64}           # [line] = g_l = R/(R²+X²)
end

# =============================================================================
# Configuration Structs (Input)
# =============================================================================

"""
    TransmissionLineData

Per-line transmission data for enhanced DC/AC power flow.

# Fields
- `line_id::String`: Unique line identifier (e.g., "line_0")
- `from_node::Int`: Source node (1-indexed)
- `to_node::Int`: Destination node (1-indexed)
- `capacity_mw::Float64`: Thermal capacity of the whole line, all circuits together (MW)
- `reactance_pu::Float64`: Series reactance of the whole line in p.u. (on system base)
- `resistance_pu::Float64`: Series resistance of the whole line in p.u. (for AC PF / losses)
- `susceptance_pu::Float64`: Shunt charging susceptance of the whole line in p.u. (for AC PF)
- `length_km::Float64`: Line length (km)
- `voltage_kv::Float64`: Rated voltage (kV)
- `num_circuits::Int`: Number of parallel circuits. Descriptive: the values
  above already include all circuits, so the model does not rescale them.
- `frequency_hz::Float64`, `current_type::String`: "AC" or "DC"
- `flow_cost::Float64`: Cost per MWh carried in either direction (solver cost
  units/MWh; 0 = free). Inter-system links of a merged run get
  `distance_km × cost_per_mw_km`. Optional (12-argument constructor = 0).
"""
struct TransmissionLineData
    line_id::String
    from_node::Int
    to_node::Int
    capacity_mw::Float64
    reactance_pu::Float64
    resistance_pu::Float64
    susceptance_pu::Float64
    length_km::Float64
    voltage_kv::Float64
    num_circuits::Int
    frequency_hz::Float64
    current_type::String  # "AC" or "DC"
    flow_cost::Float64    # cost per MWh carried, either direction (0 = free)
end

TransmissionLineData(line_id, from_node, to_node, capacity_mw, reactance_pu,
                     resistance_pu, susceptance_pu, length_km, voltage_kv,
                     num_circuits, frequency_hz, current_type) =
    TransmissionLineData(line_id, from_node, to_node, capacity_mw, reactance_pu,
                         resistance_pu, susceptance_pu, length_km, voltage_kv,
                         num_circuits, frequency_hz, current_type, 0.0)

"""
    BusData

Electrical bus within a geographic node.

# Fields
- `bus_id::Int`: Bus position index (1-indexed)
- `parent_node::Int`: Parent geographic node (1-indexed)
- `voltage_kv::Float64`: Nominal voltage (kV)
- `frequency_hz::Float64`: Nominal frequency (Hz)
- `current_type::String`: "AC" or "DC"
- `bus_type::String`: "PQ", "PV", or "slack" (AC power flow classification)
- `role::String`: "connection", "load", or "mixed" — semantic purpose of the bus.
  Connection buses do not carry demand and are skipped from load_shed/reserve
  variable creation; their KCL only enforces flow balance.
- `demand_fraction::Float64`: Fraction of parent node's demand served by this bus
"""
struct BusData
    bus_id::Int
    parent_node::Int
    voltage_kv::Float64
    frequency_hz::Float64
    current_type::String
    bus_type::String
    role::String
    demand_fraction::Float64
end

"""
    TransformerData

Transformer branch connecting two buses at different voltage levels.

In DC power flow: modeled as a branch with series reactance.
In AC power flow: full model with tap ratio, resistance, and reactance.

# Fields
- `name::String`: Transformer identifier
- `from_node::Int`: Primary (HV) bus (1-indexed)
- `to_node::Int`: Secondary (LV) bus (1-indexed)
- `from_voltage_kv::Float64`: Primary voltage (kV)
- `to_voltage_kv::Float64`: Secondary voltage (kV)
- `rated_power_mva::Float64`: Rated apparent power (MVA)
- `impedance_pu::Float64`: Series impedance magnitude (p.u. on transformer base)
- `resistance_pu::Float64`: Series resistance (p.u.), derived from losses
- `reactance_pu::Float64`: Series reactance (p.u.), sqrt(z² - r²)
- `tap_ratio::Float64`: Off-nominal tap ratio = from_voltage / to_voltage (p.u.)
- `losses_fraction::Float64`: Active power losses as fraction of rated
"""
struct TransformerData
    name::String
    from_node::Int
    to_node::Int
    from_voltage_kv::Float64
    to_voltage_kv::Float64
    rated_power_mva::Float64
    impedance_pu::Float64
    resistance_pu::Float64
    reactance_pu::Float64
    tap_ratio::Float64
    losses_fraction::Float64
end

"""
    ACDCConverterData

AC/DC converter (rectifier/inverter) branch connecting AC and DC buses.

In DC power flow: bidirectional power transfer with directional efficiencies.
from_node = AC side, to_node = DC side.

# Fields
- `name::String`: Converter identifier
- `converter_type::String`: "VSC" (Voltage Source Converter) or "LCC" (Line Commutated)
- `from_node::Int`: AC-side bus (1-indexed)
- `to_node::Int`: DC-side bus (1-indexed)
- `from_voltage_kv::Float64`: AC-side voltage (kV)
- `dc_voltage_kv::Float64`: DC-side voltage (kV)
- `rated_power_mva::Float64`: Rated apparent power (MVA)
- `min_power_mva::Float64`: Minimum operating power (MVA)
- `efficiency_rectify::Float64`: AC→DC efficiency (0-1)
- `efficiency_invert::Float64`: DC→AC efficiency (0-1)
- `standby_losses_mw::Float64`: Standby power losses (MW)
- `reactive_power_min_mvar::Float64`: Min reactive power (MVAr, negative = absorb)
- `reactive_power_max_mvar::Float64`: Max reactive power (MVAr)
- `power_factor::Float64`: Power factor
- `impedance_pu::Float64`: Series impedance (p.u.)
- `resistance_pu::Float64`: Series resistance (p.u.)
- `invest_cost::Float64`: Investment cost (\$/MW)
- `fixed_cost::Float64`: Fixed O&M cost (\$/MW/year)
- `variable_cost::Float64`: Variable cost (\$/MWh)
- `invest_max_power::Float64`: Maximum investment capacity (MW)
- `life_time::Int`: Economic lifetime (years)
- `initial_age::Int`: Initial age (years)
- `degradation_rate::Float64`: Annual degradation rate
"""
struct ACDCConverterData
    name::String
    converter_type::String
    from_node::Int
    to_node::Int
    from_voltage_kv::Float64
    dc_voltage_kv::Float64
    rated_power_mva::Float64
    min_power_mva::Float64
    efficiency_rectify::Float64
    efficiency_invert::Float64
    standby_losses_mw::Float64
    reactive_power_min_mvar::Float64
    reactive_power_max_mvar::Float64
    power_factor::Float64
    impedance_pu::Float64
    resistance_pu::Float64
    invest_cost::Float64
    fixed_cost::Float64
    variable_cost::Float64
    invest_max_power::Float64
    life_time::Int
    initial_age::Int
    degradation_rate::Float64
end

"""
    FrequencyConverterData

Frequency converter branch connecting buses at different frequencies.

Bidirectional power transfer with directional efficiencies.
from_node = frequency A side, to_node = frequency B side.

# Fields
- `name::String`: Converter identifier
- `from_node::Int`: Frequency-A bus (1-indexed)
- `to_node::Int`: Frequency-B bus (1-indexed)
- `from_frequency_hz::Float64`: Frequency on from_node side (Hz)
- `to_frequency_hz::Float64`: Frequency on to_node side (Hz)
- `rated_power_mva::Float64`: Rated apparent power (MVA)
- `min_power_mva::Float64`: Minimum operating power (MVA)
- `efficiency_a_to_b::Float64`: A→B efficiency (0-1)
- `efficiency_b_to_a::Float64`: B→A efficiency (0-1)
- `standby_losses_mw::Float64`: Standby power losses (MW)
- `reactive_power_min_mvar::Float64`: Min reactive power (MVAr)
- `reactive_power_max_mvar::Float64`: Max reactive power (MVAr)
- `impedance_pu::Float64`: Series impedance (p.u.)
- `resistance_pu::Float64`: Series resistance (p.u.)
- `invest_cost::Float64`: Investment cost (\$/MW)
- `fixed_cost::Float64`: Fixed O&M cost (\$/MW/year)
- `variable_cost::Float64`: Variable cost (\$/MWh)
- `invest_max_power::Float64`: Maximum investment capacity (MW)
- `life_time::Int`: Economic lifetime (years)
- `initial_age::Int`: Initial age (years)
- `degradation_rate::Float64`: Annual degradation rate
"""
struct FrequencyConverterData
    name::String
    from_node::Int
    to_node::Int
    from_frequency_hz::Float64
    to_frequency_hz::Float64
    rated_power_mva::Float64
    min_power_mva::Float64
    efficiency_a_to_b::Float64
    efficiency_b_to_a::Float64
    standby_losses_mw::Float64
    reactive_power_min_mvar::Float64
    reactive_power_max_mvar::Float64
    impedance_pu::Float64
    resistance_pu::Float64
    invest_cost::Float64
    fixed_cost::Float64
    variable_cost::Float64
    invest_max_power::Float64
    life_time::Int
    initial_age::Int
    degradation_rate::Float64
end

"""
    NetworkConfig

Network configuration for DC power flow modeling.

# Fields
- `num_nodes::Int`: Number of geographic nodes in the network
- `num_buses::Int`: Number of electrical buses (>= num_nodes)
- `buses::Vector{BusData}`: Bus definitions (one or more per node)
- `bus_to_node::Vector{Int}`: Mapping from bus index to parent node index (1-indexed)
- `connections::Matrix{Float64}`: Adjacency matrix with line capacities (MW)
- `distances::Matrix{Float64}`: Distance matrix (km)
- `s_base::Float64`: System power base (MVA) of the per-unit line data (`ac_power_flow.base_mva`)
- `reactance_per_km::Float64`: Line reactance per km (Ω/km)
- `voltage_level_kv::Float64`: Nominal voltage level (kV)
- `max_angle_diff_rad::Float64`: Maximum voltage angle difference (radians)
- `slack_bus::Int`: Slack bus index (1-indexed in Julia)
- `transference_invest_cost::Vector{Float64}`: Transmission investment cost per node (\$/MW)
- `transference_invest_max::Vector{Float64}`: Maximum transmission investment per node (MW)
- `transmission_lines::Vector{TransmissionLineData}`: Per-line data (empty = legacy adjacency mode)
- `transformers::Vector{TransformerData}`: Transformer branches (empty = none)
- `acdc_converters::Vector{ACDCConverterData}`: AC/DC converter branches (empty = none)
- `freq_converters::Vector{FrequencyConverterData}`: Frequency converter branches (empty = none)
"""
struct NetworkConfig
    num_nodes::Int
    num_buses::Int
    buses::Vector{BusData}
    bus_to_node::Vector{Int}
    connections::Matrix{Float64}
    distances::Matrix{Float64}
    s_base::Float64
    reactance_per_km::Float64
    voltage_level_kv::Float64
    max_angle_diff_rad::Float64
    slack_bus::Int
    transference_invest_cost::Vector{Float64}
    transference_invest_max::Vector{Float64}
    transmission_lines::Vector{TransmissionLineData}
    transformers::Vector{TransformerData}
    acdc_converters::Vector{ACDCConverterData}
    freq_converters::Vector{FrequencyConverterData}
    default_r_to_x_ratio::Float64       # Default R/X ratio when resistance not specified (0.1)
end

"""
    GeneratorConfig

Generator/technology configuration.

# Fields
- `name::String`: Generator name/identifier
- `type::String`: Technology type ("Renewable", "Non-renewable", "Storage")
- `fuel::String`: Fuel type (e.g., "Solar", "Wind", "Gas", "Diesel")
- `rated_power::Vector{Float64}`: Rated power per node (MW)
- `min_power::Vector{Float64}`: Minimum power as fraction of rated (per node)
- `efficiency_rated::Vector{Float64}`: Efficiency at rated power
- `efficiency_min::Vector{Float64}`: Efficiency at minimum power
- `ramp_up::Vector{Float64}`: Ramp up limit per node (fraction of capacity per hour; a step of resolution_hours may move ramp × resolution_hours, capped at 1; <= 0 = no limit)
- `ramp_down::Vector{Float64}`: Ramp down limit per node (fraction of capacity per hour; a step of resolution_hours may move ramp × resolution_hours, capped at 1; <= 0 = no limit)
- `min_up_time::Vector{Float64}`: Minimum up time per node (hours)
- `min_down_time::Vector{Float64}`: Minimum down time per node (hours)
- `start_up_cost::Vector{Float64}`: Startup cost per node (\$/start)
- `fuel_cost::Vector{Float64}`: Variable fuel cost per node (\$/MWh)
- `fixed_cost::Vector{Float64}`: Fixed O&M cost per node (\$/MW/year of power in service)
- `maintenance_cost::Vector{Float64}`: Maintenance cost per node (\$/MWh)
- `inertia::Vector{Float64}`: Inertia constant H per node (seconds)
- `invest_cost::Vector{Float64}`: Investment cost per node (\$/MW); prices the master's life extension
- `availability::Matrix{Float64}`: Availability matrix [hours × nodes]
- `reservable::Bool`: Whether this generator can provide reserves
- `life_time::Vector{Float64}`: Economic lifetime per node (years)
- `initial_age::Vector{Float64}`: Initial age per node (years)
- `degradation_rate::Vector{Float64}`: Annual degradation rate per node
- `decommissioning_cost::Vector{Float64}`: Decommissioning cost per node (\$/MW)
- `frequency_hz::Float64`: Operating frequency (Hz), default 50.0
- `current_type::String`: Current type ("AC", "DC", or "AC_DC")
- `reservoir_capacity::Vector{Float64}`: Reservoir capacity per node (MWh-eq), 0 = no reservoir
- `reservoir_initial_level::Vector{Float64}`: Initial reservoir level fraction (0-1)
- `reservoir_min_level::Vector{Float64}`: Minimum reservoir level fraction (0-1)
- `reservoir_max_level::Vector{Float64}`: Maximum reservoir level fraction (0-1)
- `reservoir_inflow::Matrix{Float64}`: Inflow time series [hours × nodes] (MW-eq)
- `reservoir_turbine_efficiency::Vector{Float64}`: Water→electricity efficiency (0-1)
- `reservoir_evaporation_rate::Vector{Float64}`: Per-hour evaporation fraction
- `reservoir_pump_capacity::Vector{Float64}`: Pump-back capacity per node (MW), 0 = no pumping
- `reservoir_pump_efficiency::Vector{Float64}`: Pump-back efficiency (0-1)
- `reservoir_spillage_allowed::Bool`: Whether reservoir spillage is permitted
- `reservoir_invest_cost::Vector{Float64}`: Reservoir expansion cost per node (\$/MWh)
- `reservoir_invest_max::Vector{Float64}`: Max reservoir expansion per node (MWh)
- `num_units::Vector{Int}`: Identical units the capacity at each bus is made of
  (clustered unit commitment: `min_power`, start-ups and min up/down apply per
  unit of `rated_power / num_units`; `start_up_cost` is per start of all of
  them). Fewer than 1 counts as 1, see [`units_of`](@ref).
"""
struct GeneratorConfig
    name::String
    type::String
    fuel::String
    rated_power::Vector{Float64}
    min_power::Vector{Float64}
    efficiency_rated::Vector{Float64}
    efficiency_min::Vector{Float64}
    ramp_up::Vector{Float64}
    ramp_down::Vector{Float64}
    min_up_time::Vector{Float64}
    min_down_time::Vector{Float64}
    start_up_cost::Vector{Float64}
    fuel_cost::Vector{Float64}
    fixed_cost::Vector{Float64}
    maintenance_cost::Vector{Float64}
    inertia::Vector{Float64}
    invest_cost::Vector{Float64}
    availability::Matrix{Float64}
    reservable::Bool
    life_time::Vector{Float64}
    initial_age::Vector{Float64}
    degradation_rate::Vector{Float64}
    decommissioning_cost::Vector{Float64}
    frequency_hz::Float64
    current_type::String
    # Reservoir hydroelectric (optional — zeros = no reservoir)
    reservoir_capacity::Vector{Float64}
    reservoir_initial_level::Vector{Float64}
    reservoir_min_level::Vector{Float64}
    reservoir_max_level::Vector{Float64}
    reservoir_inflow::Matrix{Float64}
    reservoir_turbine_efficiency::Vector{Float64}
    reservoir_evaporation_rate::Vector{Float64}
    reservoir_pump_capacity::Vector{Float64}
    reservoir_pump_efficiency::Vector{Float64}
    reservoir_spillage_allowed::Bool
    reservoir_invest_cost::Vector{Float64}
    reservoir_invest_max::Vector{Float64}
    risk_coefficient::Vector{Float64}     # Geographic risk derating per node (0-1)
    # Mandatory minimum release (MW-eq) per node — ecological / minimum
    # environmental flow. Default 0 is neutral. (Minimum stable generation is
    # already handled by the existing `min_power` field.)
    reservoir_min_release::Vector{Float64}
    # Hydraulic cascade. Water released by this reservoir (turbined + spilled)
    # becomes inflow to the reservoir named `cascade_downstream` after
    # `cascade_delay_hours` of travel time. Empty name = terminal reservoir
    # (its release leaves the modelled system). Plant-level (scalar), since the
    # cascade topology links whole hydro plants, not individual nodes.
    cascade_downstream::String
    cascade_delay_hours::Int
    # Head dependence (per node). Power-availability factor at the MINIMUM
    # reservoir level, in (0,1]. 1.0 = no head effect (nameplate power at any
    # level). Below 1.0, available turbine power scales linearly from
    # head_min_factor*rated at the minimum level up to rated at the maximum
    # level — a depleted reservoir means low head means less peak power.
    reservoir_head_min_factor::Vector{Float64}
    # Clustered unit commitment: identical units per bus (1 = one unit)
    num_units::Vector{Int}
end

# Backward-compatible constructors. The struct grew over time, so older call
# sites pass fewer positional arguments and the newer fields take neutral
# defaults: reservoir_min_release = zeros, cascade_downstream = "" (terminal),
# cascade_delay_hours = 0, reservoir_head_min_factor = ones (no head effect),
# num_units = ones (one unit per bus).
# Dispatch is by argument count (38 / 39 / 41 / 42 / full 43).

# 38-arg: through risk_coefficient.
function GeneratorConfig(
    name, type, fuel, rated_power, min_power, efficiency_rated, efficiency_min,
    ramp_up, ramp_down, min_up_time, min_down_time, start_up_cost, fuel_cost,
    fixed_cost, maintenance_cost, inertia, invest_cost, availability,
    reservable, life_time, initial_age, degradation_rate, decommissioning_cost,
    frequency_hz, current_type, reservoir_capacity, reservoir_initial_level,
    reservoir_min_level, reservoir_max_level, reservoir_inflow,
    reservoir_turbine_efficiency, reservoir_evaporation_rate,
    reservoir_pump_capacity, reservoir_pump_efficiency, reservoir_spillage_allowed,
    reservoir_invest_cost, reservoir_invest_max, risk_coefficient,
)
    n = length(rated_power)
    return GeneratorConfig(
        name, type, fuel, rated_power, min_power, efficiency_rated, efficiency_min,
        ramp_up, ramp_down, min_up_time, min_down_time, start_up_cost, fuel_cost,
        fixed_cost, maintenance_cost, inertia, invest_cost, availability,
        reservable, life_time, initial_age, degradation_rate, decommissioning_cost,
        frequency_hz, current_type, reservoir_capacity, reservoir_initial_level,
        reservoir_min_level, reservoir_max_level, reservoir_inflow,
        reservoir_turbine_efficiency, reservoir_evaporation_rate,
        reservoir_pump_capacity, reservoir_pump_efficiency, reservoir_spillage_allowed,
        reservoir_invest_cost, reservoir_invest_max, risk_coefficient,
        zeros(Float64, n), "", 0, ones(Float64, n),  # min_release, cascade_*, head_min_factor
        ones(Int, n),  # num_units
    )
end

# 39-arg: through reservoir_min_release (cascade + head default to none).
function GeneratorConfig(
    name, type, fuel, rated_power, min_power, efficiency_rated, efficiency_min,
    ramp_up, ramp_down, min_up_time, min_down_time, start_up_cost, fuel_cost,
    fixed_cost, maintenance_cost, inertia, invest_cost, availability,
    reservable, life_time, initial_age, degradation_rate, decommissioning_cost,
    frequency_hz, current_type, reservoir_capacity, reservoir_initial_level,
    reservoir_min_level, reservoir_max_level, reservoir_inflow,
    reservoir_turbine_efficiency, reservoir_evaporation_rate,
    reservoir_pump_capacity, reservoir_pump_efficiency, reservoir_spillage_allowed,
    reservoir_invest_cost, reservoir_invest_max, risk_coefficient,
    reservoir_min_release,
)
    n = length(rated_power)
    return GeneratorConfig(
        name, type, fuel, rated_power, min_power, efficiency_rated, efficiency_min,
        ramp_up, ramp_down, min_up_time, min_down_time, start_up_cost, fuel_cost,
        fixed_cost, maintenance_cost, inertia, invest_cost, availability,
        reservable, life_time, initial_age, degradation_rate, decommissioning_cost,
        frequency_hz, current_type, reservoir_capacity, reservoir_initial_level,
        reservoir_min_level, reservoir_max_level, reservoir_inflow,
        reservoir_turbine_efficiency, reservoir_evaporation_rate,
        reservoir_pump_capacity, reservoir_pump_efficiency, reservoir_spillage_allowed,
        reservoir_invest_cost, reservoir_invest_max, risk_coefficient,
        reservoir_min_release, "", 0, ones(Float64, n),  # cascade_*, head_min_factor
        ones(Int, n),  # num_units
    )
end

# 41-arg: through cascade_delay_hours (head defaults to no effect).
function GeneratorConfig(
    name, type, fuel, rated_power, min_power, efficiency_rated, efficiency_min,
    ramp_up, ramp_down, min_up_time, min_down_time, start_up_cost, fuel_cost,
    fixed_cost, maintenance_cost, inertia, invest_cost, availability,
    reservable, life_time, initial_age, degradation_rate, decommissioning_cost,
    frequency_hz, current_type, reservoir_capacity, reservoir_initial_level,
    reservoir_min_level, reservoir_max_level, reservoir_inflow,
    reservoir_turbine_efficiency, reservoir_evaporation_rate,
    reservoir_pump_capacity, reservoir_pump_efficiency, reservoir_spillage_allowed,
    reservoir_invest_cost, reservoir_invest_max, risk_coefficient,
    reservoir_min_release, cascade_downstream, cascade_delay_hours,
)
    n = length(rated_power)
    return GeneratorConfig(
        name, type, fuel, rated_power, min_power, efficiency_rated, efficiency_min,
        ramp_up, ramp_down, min_up_time, min_down_time, start_up_cost, fuel_cost,
        fixed_cost, maintenance_cost, inertia, invest_cost, availability,
        reservable, life_time, initial_age, degradation_rate, decommissioning_cost,
        frequency_hz, current_type, reservoir_capacity, reservoir_initial_level,
        reservoir_min_level, reservoir_max_level, reservoir_inflow,
        reservoir_turbine_efficiency, reservoir_evaporation_rate,
        reservoir_pump_capacity, reservoir_pump_efficiency, reservoir_spillage_allowed,
        reservoir_invest_cost, reservoir_invest_max, risk_coefficient,
        reservoir_min_release, cascade_downstream, cascade_delay_hours,
        ones(Float64, n),  # reservoir_head_min_factor
        ones(Int, n),  # num_units
    )
end

# 42-arg: through reservoir_head_min_factor (one unit per bus).
function GeneratorConfig(
    name, type, fuel, rated_power, min_power, efficiency_rated, efficiency_min,
    ramp_up, ramp_down, min_up_time, min_down_time, start_up_cost, fuel_cost,
    fixed_cost, maintenance_cost, inertia, invest_cost, availability,
    reservable, life_time, initial_age, degradation_rate, decommissioning_cost,
    frequency_hz, current_type, reservoir_capacity, reservoir_initial_level,
    reservoir_min_level, reservoir_max_level, reservoir_inflow,
    reservoir_turbine_efficiency, reservoir_evaporation_rate,
    reservoir_pump_capacity, reservoir_pump_efficiency, reservoir_spillage_allowed,
    reservoir_invest_cost, reservoir_invest_max, risk_coefficient,
    reservoir_min_release, cascade_downstream, cascade_delay_hours,
    reservoir_head_min_factor,
)
    return GeneratorConfig(
        name, type, fuel, rated_power, min_power, efficiency_rated, efficiency_min,
        ramp_up, ramp_down, min_up_time, min_down_time, start_up_cost, fuel_cost,
        fixed_cost, maintenance_cost, inertia, invest_cost, availability,
        reservable, life_time, initial_age, degradation_rate, decommissioning_cost,
        frequency_hz, current_type, reservoir_capacity, reservoir_initial_level,
        reservoir_min_level, reservoir_max_level, reservoir_inflow,
        reservoir_turbine_efficiency, reservoir_evaporation_rate,
        reservoir_pump_capacity, reservoir_pump_efficiency, reservoir_spillage_allowed,
        reservoir_invest_cost, reservoir_invest_max, risk_coefficient,
        reservoir_min_release, cascade_downstream, cascade_delay_hours,
        reservoir_head_min_factor, ones(Int, length(rated_power)),
    )
end

"""
    units_of(gen, b) -> Int

Identical units generator `gen` has at bus `b` (clustered unit commitment):
`gen.num_units[b]`, at least 1, and 1 where the vector is shorter.
"""
units_of(gen, b::Int) = length(gen.num_units) >= b ? max(1, gen.num_units[b]) : 1

# Fuels of variable renewables (spaces, '_' and '-' removed, lower case):
# weather and ocean resources, and no fuel at all. Water / hydro is variable
# only without a reservoir (run of river).
const VARIABLE_RENEWABLE_FUELS = Set(["sun", "solar", "pv", "solarpv", "photovoltaic",
    "wind", "windonshore", "windoffshore", "onshorewind", "offshorewind",
    "otec", "wave", "tidal", "none", ""])
const HYDRO_FUELS = Set(["water", "hydro", "runofriver"])

"""
    is_variable_renewable(unit, b) -> Bool

Whether generator or technology `unit` is a variable renewable at bus `b`:
type `"Renewable"` and a fuel-less resource whose output follows its
availability profile — sun, wind, ocean (OTEC, wave, tidal), no fuel
(`"None"` or empty), or water / hydro without a reservoir at `b`.

A variable renewable is not committed (no on/off status or start-ups in
unit commitment), holds no reserve, is not counted in generation N-1
and is what curtailment measures. Every other unit, renewable or not
(biomass, biogas, geothermal, reservoir hydro, thermal), is dispatchable:
committed in unit commitment and holding reserve when `reservable`. The
renewable-energy share still counts every `"Renewable"` unit.
Mirrored in Python by `esfex.config.schema.is_variable_renewable`.
"""
function is_variable_renewable(unit, b::Int)
    unit.type == "Renewable" || return false
    fuel = replace(lowercase(strip(unit.fuel)), r"[\s_\-]" => "")
    fuel in VARIABLE_RENEWABLE_FUELS && return true
    fuel in HYDRO_FUELS || return false
    hasproperty(unit, :reservoir_capacity) || return true
    res = unit.reservoir_capacity
    return !(b <= length(res) && res[b] > 0)
end

"""
    committed_buses(gen, buses) -> Vector{Int}

The buses of `buses` where generator `gen` is committed in unit commitment
(has `gen_status` / `gen_startup`): those where it is not a variable
renewable ([`is_variable_renewable`](@ref)).
"""
committed_buses(gen, buses) = [b for b in buses if !is_variable_renewable(gen, b)]

"""
    BatteryConfig

Battery/storage configuration.

# Fields
- `name::String`: Battery name/identifier
- `capacity::Vector{Float64}`: Energy capacity per node (MWh)
- `max_charge_power::Vector{Float64}`: Maximum charge power per node (MW)
- `max_discharge_power::Vector{Float64}`: Maximum discharge power per node (MW)
- `charge_efficiency::Vector{Float64}`: Charging efficiency (0-1)
- `discharge_efficiency::Vector{Float64}`: Discharging efficiency (0-1)
- `soc_min::Vector{Float64}`: Minimum SOC as fraction (0-1)
- `soc_max::Vector{Float64}`: Maximum SOC as fraction (0-1)
- `soc_initial::Vector{Float64}`: Initial SOC as fraction (0-1)
- `self_discharge::Vector{Float64}`: Self-discharge rate per hour
- `invest_cost_power::Vector{Float64}`: Power investment cost (\$/MW); prices the master's life extension
- `invest_max_power::Vector{Float64}`: Power (MW) > 0 places the battery at a bus in the master's representative days
- `life_time::Vector{Float64}`: Economic lifetime per node (years)
- `initial_age::Vector{Float64}`: Initial age per node (years)
- `decommissioning_cost::Vector{Float64}`: Decommissioning cost per node (\$/MW)
- `min_duration_hours::Float64`: Minimum energy-to-power ratio (hours)
- `max_duration_hours::Float64`: Maximum energy-to-power ratio (hours)
- `maintenance_cost::Vector{Float64}`: Maintenance cost per node (\$/MWh throughput)
- `inertia::Vector{Float64}`: Inertia constant per node (seconds) - for synthetic inertia contribution
- `spillage::Bool`: Whether spillage is allowed for this battery (release energy without grid injection)
- `current_type::String`: Current type ("AC" or "DC")
- `degradation_rate::Vector{Float64}`: Age-based capacity degradation rate per node
- `throughput_degradation_cost::Vector{Float64}`: Cycling wear cost per MWh discharged
- `risk_coefficient::Vector{Float64}`: Geographic risk derating per node (0-1)
- `ramp_up::Vector{Float64}`: Fraction of the rated charge/discharge power by
  which charging or discharging may rise per hour (× the step length; 0 = no limit)
- `ramp_down::Vector{Float64}`: Same for a fall in charging or discharging power
- `fixed_cost::Vector{Float64}`: Fixed O&M per MW of discharge power per year
- `reservable::Bool`: Whether the battery's headroom counts towards reserves
- `availability::Matrix{Float64}`: Factor on the charge and discharge power
  limits, `[step × bus]`; a 0-row matrix means fully available
"""
struct BatteryConfig
    name::String
    capacity::Vector{Float64}
    max_charge_power::Vector{Float64}
    max_discharge_power::Vector{Float64}
    charge_efficiency::Vector{Float64}
    discharge_efficiency::Vector{Float64}
    soc_min::Vector{Float64}
    soc_max::Vector{Float64}
    soc_initial::Vector{Float64}
    self_discharge::Vector{Float64}
    invest_cost_power::Vector{Float64}
    invest_max_power::Vector{Float64}
    life_time::Vector{Float64}
    initial_age::Vector{Float64}
    decommissioning_cost::Vector{Float64}
    min_duration_hours::Float64
    max_duration_hours::Float64
    maintenance_cost::Vector{Float64}  # Matches Python legacy batteries[bat]['maintenance_cost'][node]
    inertia::Vector{Float64}  # Matches Python legacy batteries[bat]['inertia'][node]
    spillage::Bool  # Matches Python legacy batteries[bat].get('spillage', False)
    current_type::String  # "AC" or "DC"
    degradation_rate::Vector{Float64}  # Age-based capacity degradation rate per node
    throughput_degradation_cost::Vector{Float64}  # $/MWh discharged — cycling wear cost
    risk_coefficient::Vector{Float64}  # Geographic risk derating per node (0-1)
    ramp_up::Vector{Float64}       # fraction of rated power per hour, 0 = no limit
    ramp_down::Vector{Float64}
    fixed_cost::Vector{Float64}    # per MW of discharge power per year
    reservable::Bool
    availability::Matrix{Float64}  # [step × bus]; 0 rows = always 1.0
end

# Without the operating fields added later: no ramp limit, no fixed cost,
# reservable, fully available.
function BatteryConfig(
    name, capacity, max_charge_power, max_discharge_power, charge_efficiency,
    discharge_efficiency, soc_min, soc_max, soc_initial, self_discharge,
    invest_cost_power, invest_max_power,
    life_time, initial_age, decommissioning_cost, min_duration_hours,
    max_duration_hours, maintenance_cost, inertia, spillage, current_type,
    degradation_rate, throughput_degradation_cost, risk_coefficient,
)
    n = length(capacity)
    return BatteryConfig(
        name, capacity, max_charge_power, max_discharge_power, charge_efficiency,
        discharge_efficiency, soc_min, soc_max, soc_initial, self_discharge,
        invest_cost_power, invest_max_power,
        life_time, initial_age, decommissioning_cost, min_duration_hours,
        max_duration_hours, maintenance_cost, inertia, spillage, current_type,
        degradation_rate, throughput_degradation_cost, risk_coefficient,
        zeros(Float64, n), zeros(Float64, n), zeros(Float64, n), true,
        zeros(Float64, 0, n),
    )
end

"""
    is_renewable_unit(unit) -> Bool

The one rule for what counts as renewable generation in an RE share
(dispatch result and target, master, Benders, stochastic, MGA, runner,
HDF5): a generator or candidate technology of type "Renewable" (wind, solar,
hydro, biomass… as the case declares them). Battery discharge never counts,
as renewable or as generation: a battery may charge from fossil units. The
RE share is renewable generation over total generation (all generators and
technologies), so 100 % means no fossil generation.
"""
is_renewable_unit(unit) = unit.type == "Renewable"

"""
    storage_availability(availability, t, b) -> Float64

Factor on a battery's (or battery technology's) charge and discharge power
at step `t` and bus `b`: `availability[t, b]`, or 1.0 when the unit has no
profile (0-row matrix).
"""
storage_availability(availability::AbstractMatrix, t::Int, b::Int) =
    size(availability, 1) == 0 ? 1.0 : availability[t, b]

"""
    storage_ramp_limit(ramp, tres, power) -> Union{Nothing, Any}

Largest change of a battery's charge or of its discharge power over one step
of `tres` hours: `min(1, ramp × tres) × power`, or `nothing` (no limit) when
`ramp ≤ 0` or `ramp × tres ≥ 1`, since charge and discharge each stay within
`[0, power]` anyway.
"""
function storage_ramp_limit(ramp::Real, tres::Real, power)
    (ramp <= 0 || ramp * tres >= 1) && return nothing
    return ramp * tres * power
end

# =============================================================================
# Candidate Technology Structs (for per-technology investment in MasterProblem)
# =============================================================================

"""
    TechnologyConfig

Candidate generation technology for new investment.
Unlike GeneratorConfig (existing physical units), technologies define
what CAN be built. The master problem creates investment variables per-technology,
not per-generator.

# Fields
- `name::String`: Technology name (e.g., "Solar PV", "Wind")
- `type::String`: "Renewable" or "Non-renewable"
- `fuel::String`: Fuel type (e.g., "Solar", "Wind", "Gas")
- `invest_cost::Vector{Float64}`: Investment cost per bus (\$/MW)
- `invest_max::Vector{Float64}`: Maximum investment per bus (MW)
- `availability::Matrix{Float64}`: Availability matrix [hours × buses]
- Other fields mirror GeneratorConfig for operational dispatch
"""
struct TechnologyConfig
    name::String
    type::String
    fuel::String
    invest_cost::Vector{Float64}
    invest_max::Vector{Float64}
    availability::Matrix{Float64}
    eff_at_rated::Vector{Float64}
    eff_at_min::Vector{Float64}
    ramp_up::Vector{Float64}
    ramp_down::Vector{Float64}
    min_up_time::Vector{Float64}
    min_down_time::Vector{Float64}
    min_power::Vector{Float64}
    fuel_cost::Vector{Float64}
    fixed_cost::Vector{Float64}    # per MW of power in service per year
    maintenance_cost::Vector{Float64}
    start_up_cost::Vector{Float64}
    inertia::Vector{Float64}
    life_time::Vector{Float64}
    degradation_rate::Vector{Float64}
    decommissioning_cost::Vector{Float64}
    frequency_hz::Float64
    current_type::String
    reservable::Bool
    risk_coefficient::Vector{Float64}  # Geographic risk derating per node (0-1)
end

"""
    BatteryTechnologyConfig

Candidate battery/storage technology for new investment.

# Fields
- `name::String`: Technology name (e.g., "Lithium-Ion")
- Investment: cost and max per bus for power (MW) and capacity (MWh)
- Duration: min/max energy-to-power ratio
- Performance: charge/discharge efficiency, degradation
"""
struct BatteryTechnologyConfig
    name::String
    invest_cost_power::Vector{Float64}
    invest_cost_capacity::Vector{Float64}
    invest_max_power::Vector{Float64}
    invest_max_capacity::Vector{Float64}
    min_duration_hours::Float64
    max_duration_hours::Float64
    charge_efficiency::Vector{Float64}
    discharge_efficiency::Vector{Float64}
    degradation_rate::Vector{Float64}
    life_time::Vector{Float64}
    soc_initial::Vector{Float64}
    soc_min::Vector{Float64}
    soc_max::Vector{Float64}
    maintenance_cost::Vector{Float64}
    inertia::Vector{Float64}
    throughput_degradation_cost::Vector{Float64}
    spillage::Bool
    current_type::String
    decommissioning_cost::Vector{Float64}
    risk_coefficient::Vector{Float64}  # Geographic risk derating per node (0-1)
    # Operating fields of the batteries it builds (see BatteryConfig)
    ramp_up::Vector{Float64}
    ramp_down::Vector{Float64}
    fixed_cost::Vector{Float64}    # per MW of power built per year
    reservable::Bool
    availability::Matrix{Float64}  # [step × bus]; 0 rows = always 1.0
end

# Without the operating fields added later (see BatteryConfig)
function BatteryTechnologyConfig(
    name, invest_cost_power, invest_cost_capacity, invest_max_power,
    invest_max_capacity, min_duration_hours, max_duration_hours,
    charge_efficiency, discharge_efficiency, degradation_rate, life_time,
    soc_initial, soc_min, soc_max, maintenance_cost, inertia,
    throughput_degradation_cost, spillage, current_type, decommissioning_cost,
    risk_coefficient,
)
    n = length(invest_cost_power)
    return BatteryTechnologyConfig(
        name, invest_cost_power, invest_cost_capacity, invest_max_power,
        invest_max_capacity, min_duration_hours, max_duration_hours,
        charge_efficiency, discharge_efficiency, degradation_rate, life_time,
        soc_initial, soc_min, soc_max, maintenance_cost, inertia,
        throughput_degradation_cost, spillage, current_type, decommissioning_cost,
        risk_coefficient,
        zeros(Float64, n), zeros(Float64, n), zeros(Float64, n), true,
        zeros(Float64, 0, n),
    )
end

"""
    TemporalConfig

Temporal configuration for simulation.

# Fields
- `hours::Int`: Total simulation hours
- `resolution_hours::Int`: Time step resolution (hours)
- `rolling_horizon_hours::Int`: Rolling horizon window size (hours)
- `overlap_hours::Int`: Overlap between windows (hours)
- `investment_resolution::Int`: Investment decision resolution (hours)
- `primary_energy_resolution::Int`: Primary energy model resolution (hours)
- `battery_soc_resolution::Int`: Points per day for battery SOC upscaling (Python: 6)
- `ev_resolution::Int`: Points per day for EV upscaling (Python: 6)
- `reserve_resolution::Int`: Blocks per day for reserve upscaling (Python: 4)
"""
struct TemporalConfig
    hours::Int
    resolution_hours::Int
    rolling_horizon_hours::Int
    overlap_hours::Int
    investment_resolution::Int
    primary_energy_resolution::Int
    battery_soc_resolution::Int
    ev_resolution::Int
    reserve_resolution::Int
end

"""
    PenaltyConfig

Penalty costs for constraint violations.

# Fields
- `loss_of_load::Float64`: Value of lost load (\$/MWh)
- `curtailment::Float64`: Curtailment penalty (\$/MWh)
- `loss_of_reserve_static::Float64`: Static reserve shortage penalty (\$/MW)
- `loss_of_reserve_dynamic::Float64`: Dynamic reserve shortage penalty (\$/MW)
- `co2_cost::Float64`: CO2 emission cost (\$/tonne)
"""
struct PenaltyConfig
    loss_of_load::Float64
    curtailment::Float64
    loss_of_reserve_static::Float64
    loss_of_reserve_dynamic::Float64
    co2_cost::Float64
end

"""
    TargetConfig

System targets and constraints.

# Fields
- `re_penetration_target::Float64`: RE share target (fraction 0-1 of the total generation that is renewable, `is_renewable_unit`)
- `co2_budget::Float64`: CO2 budget (tonnes)
- `inertia_limit::Float64`: Minimum system inertia (MWs)
"""
struct TargetConfig
    re_penetration_target::Float64
    co2_budget::Float64
    inertia_limit::Float64
end

"""
    SolverSettings

Solver configuration.

# Fields
- `threads::Int`: Number of solver threads
- `time_limit::Float64`: Maximum solve time (seconds)
- `gap::Float64`: MIP optimality gap tolerance
- `verbose::Bool`: Enable solver output
"""
struct SolverSettings
    threads::Int
    time_limit::Float64
    gap::Float64
    verbose::Bool
end

# =============================================================================
# Electric Vehicle Types (must be before PowerSystemInput)
# =============================================================================

"""
    EVConfig

Configuration for electric vehicle fleet at a node.

# Fields
- `num_vehicles::Vector{Float64}`: Number of vehicles per node
- `battery_capacity_kwh::Float64`: Battery capacity per vehicle (kWh)
- `max_charge_power_kw::Float64`: Maximum charging power per vehicle (kW)
- `max_discharge_power_kw::Float64`: Maximum V2G discharge power per vehicle (kW)
- `charge_efficiency::Float64`: Charging efficiency (0-1)
- `discharge_efficiency::Float64`: V2G discharge efficiency (0-1)
- `min_soc::Float64`: Minimum SOC fraction (0-1)
- `max_soc::Float64`: Maximum SOC fraction (0-1)
- `target_soc::Float64`: Target SOC at end of day (0-1)
- `availability_profile::Matrix{Float64}`: Fraction of vehicles available [hour, node]
- `driving_consumption_profile::Matrix{Float64}`: Driving energy drawn from the fleet battery [hour, node] (MWh)
- `v2g_compensation::Float64`: Payment to owners for V2G energy (\$/MWh); reported, not part of the system cost
- `loss_penalty::Float64`: Penalty for driving energy the fleet cannot supply (\$/MWh)
- `initial_soc::Vector{Float64}`: Initial SOC per node (MWh)
"""
struct EVConfig
    num_vehicles::Vector{Float64}
    battery_capacity_kwh::Float64
    max_charge_power_kw::Float64
    max_discharge_power_kw::Float64
    charge_efficiency::Float64
    discharge_efficiency::Float64
    min_soc::Float64
    max_soc::Float64
    target_soc::Float64
    availability_profile::Matrix{Float64}
    driving_consumption_profile::Matrix{Float64}
    v2g_compensation::Float64
    loss_penalty::Float64
    initial_soc::Vector{Float64}  # Initial SOC per node (MWh)
end

"""
    EVVariables

Container for EV decision variables.
"""
mutable struct EVVariables
    # Charging power (node × hour)
    charging::Matrix{VariableRef}
    # V2G discharge power (node × hour)
    v2g::Matrix{VariableRef}
    # State of charge (node × hour+1)
    soc::Matrix{VariableRef}
    # SOC loss/violation (node × hour)
    loss::Matrix{VariableRef}
end

"""
    EVResult

Solution output from EV model.
"""
struct EVResult
    charging::Matrix{Float64}
    v2g::Matrix{Float64}
    soc::Matrix{Float64}
    loss::Matrix{Float64}
    total_charging::Float64
    total_v2g::Float64
    total_loss::Float64
end

# =============================================================================
# Electrolyzer Types (must be before PowerSystemInput)
# =============================================================================

"""
    ElectrolyzerConfig

Configuration for electrolyzer/hydrogen production system.

# Fields
- `rated_power::Vector{Float64}`: Existing rated power per node (MW)
- `eff_at_rated::Vector{Float64}`: Efficiency at rated power (kWh_e/kg_H2)
- `eff_at_min::Vector{Float64}`: Efficiency at minimum power
- `energy_per_kg_h2::Float64`: Specific energy consumption (kWh/kg H2)
- `ramp_up::Vector{Float64}`: Ramp up rate (fraction/hour)
- `ramp_down::Vector{Float64}`: Ramp down rate (fraction/hour)
- `invest_cost::Vector{Float64}`: Investment cost (\$/MW)
- `invest_max_power::Vector{Float64}`: Maximum investment per node (MW)
- `fixed_cost::Vector{Float64}`: Fixed O&M cost (\$/MW·year of rated power)
- `variable_cost::Vector{Float64}`: Variable cost (\$/MWh)
- `water_cost::Float64`: Water cost (\$/kg H2)
- `life_time::Vector{Float64}`: Economic lifetime per node (years)
"""
struct ElectrolyzerConfig
    rated_power::Vector{Float64}
    eff_at_rated::Vector{Float64}
    eff_at_min::Vector{Float64}
    energy_per_kg_h2::Float64
    ramp_up::Vector{Float64}
    ramp_down::Vector{Float64}
    invest_cost::Vector{Float64}
    invest_max_power::Vector{Float64}
    fixed_cost::Vector{Float64}
    variable_cost::Vector{Float64}
    water_cost::Float64
    life_time::Vector{Float64}
end

"""
    electrolyzer_h2_per_mwh(ec::ElectrolyzerConfig, i::Int) -> Float64

Hydrogen produced per MWh drawn at index `i` (kg/MWh):
1000 × mean(eff_at_rated, eff_at_min) / energy_per_kg_h2. Shared by the
primary-energy hydrogen balance and the dispatch results.
"""
function electrolyzer_h2_per_mwh(ec::ElectrolyzerConfig, i::Int)
    ec.energy_per_kg_h2 > 0 || return 0.0
    avg_eff = (ec.eff_at_rated[i] + ec.eff_at_min[i]) / 2.0
    return 1000.0 * avg_eff / ec.energy_per_kg_h2
end

"""
    ElectrolyzerVariables

Container for electrolyzer decision variables.
"""
mutable struct ElectrolyzerVariables
    # Investment (node)
    investment::Vector{VariableRef}
    # Operational (node × hour)
    power::Matrix{VariableRef}
    h2_production::Matrix{VariableRef}
end

"""
    ElectrolyzerResult

Solution output from the electrolyzer model.
"""
struct ElectrolyzerResult
    # Investment decisions (MW per node)
    investment::Vector{Float64}
    # Operational results (node × hour)
    power::Matrix{Float64}
    h2_production::Matrix{Float64}
    # Totals
    total_investment::Float64
    total_h2_produced::Float64
    total_power_consumed::Float64
end

# =============================================================================
# PowerSystem Input (Complete Configuration)
# =============================================================================

"""
    PowerSystemInput

Complete input configuration for the PowerSystem optimization model.

This struct contains all configuration needed to build and solve the
operational dispatch or unit commitment problem.

# Fields
## System Identification
- `name::String`: System name/identifier
- `year::Int`: Simulation year
- `base_year::Int`: Base year for calculations

## Network
- `network::NetworkConfig`: Network topology and parameters

## Units
- `generators::Vector{GeneratorConfig}`: Generator configurations
- `batteries::Vector{BatteryConfig}`: Battery configurations

## Demand
- `demand::Matrix{Float64}`: Demand matrix [hours × nodes] (MW)
- `sectoral_demand::Dict{String, Matrix{Float64}}`: Demand by sector

## Temporal
- `temporal::TemporalConfig`: Time configuration

## Penalties
- `penalties::PenaltyConfig`: Penalty costs for violations

## Targets
- `targets::TargetConfig`: System targets (RE penetration, CO2, etc.)

## Mode
- `mode::String`: Operation mode ("development", "unit_commitment", "economic_dispatch")

## Solver
- `solver::SolverSettings`: Solver configuration

## Optional: CO2 factors by fuel
- `fuel_co2::Dict{String, Float64}`: CO2 emission factors (tonnes per MWh of fuel; divided by each unit's rated efficiency)
"""

# =============================================================================
# Cost Curve (Bidding/Offer Curve) Segment
# =============================================================================

"""
    CostSegment

A single segment in a piecewise-linear (PWL) cost/bidding curve.

- `fraction`: Fraction of total capacity (Pmax) covered by this segment (0-1).
              All segments for a generator must sum to 1.0.
- `marginal_cost`: Marginal cost for energy dispatched in this segment (dollars/MWh).
"""
struct CostSegment
    fraction::Float64
    marginal_cost::Float64
end

"""
    CO2BudgetGroup

An annual CO2 budget over a set of nodes. A merged multi-system run has one
per system, so each system is held to its own budget and pays its own
violation penalty.

# Fields
- `name::String`: System name (used in the constraint name).
- `nodes::Vector{Int}`: 1-indexed nodes whose generators count against the budget.
- `budget::Float64`: Annual budget (t CO2). `Inf` or `<= 0` = no budget.
- `violation_penalty::Float64`: Cost per tonne over budget (scaled like the other penalties).
"""
struct CO2BudgetGroup
    name::String
    nodes::Vector{Int}
    budget::Float64
    violation_penalty::Float64
end

struct PowerSystemInput
    # System identification
    name::String
    year::Int
    base_year::Int

    # Network
    network::NetworkConfig

    # Units
    generators::Vector{GeneratorConfig}
    batteries::Vector{BatteryConfig}

    # Demand (hours × nodes)
    demand::Matrix{Float64}
    sectoral_demand::Dict{String, Matrix{Float64}}

    # Temporal
    temporal::TemporalConfig

    # Penalties (expanded for clarity)
    loss_of_load_penalty::Float64
    loss_of_reserve_static::Float64
    loss_of_reserve_dynamic::Float64
    co2_cost::Float64
    curtailment_cost::Float64           # $/MWh — penalty for spilled RE energy

    # Targets
    re_penetration_target::Float64
    co2_budget::Float64
    inertia_limit::Float64

    # Mode
    mode::String

    # Solver settings
    solver_name::String
    threads::Int
    time_limit::Float64
    gap::Float64
    verbose::Bool
    solver_options::Dict{String, Any}

    # Optional CO2 factors
    fuel_co2::Dict{String, Float64}

    # EV configuration (optional)
    ev_config::Union{EVConfig, Nothing}

    # Electrolyzers (each one per-bus arrays); empty = none
    electrolyzers::Vector{ElectrolyzerConfig}

    # Sectoral load criticality weights for load shedding penalty
    sectoral_criticality::Dict{String, Float64}

    # Demand shifting of flexible sectors: how far (hours) and how much
    # (fraction of the sector's demand per step) demand can move.
    # Sectors absent from these dicts are not shifted.
    sectoral_delay_tolerance::Dict{String, Int}
    sectoral_flexibility_ratio::Dict{String, Float64}

    # Hourly inertia limit vector (if empty, use scalar inertia_limit)
    inertia_limit_hourly::Vector{Float64}

    # Inertia penalty for loss of inertia
    loss_of_inertia_penalty::Float64

    # N-1 Security parameters (matches Python legacy power_system.py lines 55-61)
    n1_security_enabled::Bool
    n1_transmission_enabled::Bool
    n1_generation_enabled::Bool
    n1_transmission_reserve_factor::Float64  # Fraction of capacity usable under N-1 (e.g., 0.7)
    n1_generation_reserve_type::String  # "largest_unit" or "percentage"
    n1_generation_reserve_percentage::Float64  # Used if reserve_type == "percentage"
    n1_scopf_enabled::Bool                    # Use SCOPF instead of preventive N-1
    n1_corrective_enabled::Bool               # Allow corrective actions (battery response)

    # Rooftop solar generation (hours × nodes) - optional
    rooftop_generation::Union{Matrix{Float64}, Nothing}

    # Generator initial status for carry-over between rolling horizons
    # Dict{gen_idx => Dict{node => status (0.0 or 1.0)}}
    # Matches Python legacy generator_initial_status (lines 163-209)
    generator_initial_status::Dict{Int, Dict{Int, Float64}}

    # Last-timestep generator output from previous rolling window (MW).
    # Empty Dict = no boundary; ramp constraint at t=1 is then relaxed
    # (preserves legacy behaviour). Populated when present:
    # Dict{gen_idx => Dict{bus_idx => MW}}.
    generator_output_prev::Dict{Int, Dict{Int, Float64}}

    # Last-timestep reservoir level from previous rolling window (MWh-eq).
    # Empty Dict = no boundary; reservoir uses configured initial fraction.
    # Populated when present: Dict{gen_idx => Dict{bus_idx => MWh}}.
    reservoir_level_prev::Dict{Int, Dict{Int, Float64}}
    # Battery SOC at the start of this window from the previous one (MWh).
    # Empty = no boundary; batteries use soc_initial × capacity.
    # Dict{bat_idx => Dict{bus_idx => MWh}}.
    battery_soc_prev::Dict{Int, Dict{Int, Float64}}
    # Battery discharge and charge power at the last step of the previous
    # window (MW), for the ramp limits at t=1. Empty = no boundary.
    battery_discharge_prev::Dict{Int, Dict{Int, Float64}}
    battery_charge_prev::Dict{Int, Dict{Int, Float64}}

    # Electricity price vector (hourly) - for spillage cost calculation
    # Matches Python legacy self.electricity_price[t] (line 1763)
    electricity_price::Vector{Float64}

    # Penalty coefficients from config (instead of deriving from VOLL)
    fre_penetration_penalty::Float64
    transfer_margin_penalty::Float64
    rooftop_curtailment_penalty::Float64
    co2_budget_violation_penalty::Float64

    # Reserve requirements per bus from config
    reserve_static_requirement::Dict{Int, Float64}
    reserve_dynamic_requirement::Dict{Int, Float64}

    # Demand constraints
    max_annual_system_cost::Float64

    # Configurable parameters (previously hardcoded)
    soc_end_tolerance::Float64              # Battery end-of-horizon SOC tolerance (±fraction)
    cyclic_end_soc::Bool                    # If true, force end-of-window SOC == initial (cyclic)
    bat_soc_end_min::Dict{Int, Dict{Int, Float64}}  # Soft floor on end-of-window SOC (MWh) [battery][bus]; empty = none
    bat_soc_end_min_cyclic::Bool            # Soft floor = the window's own initial SOC (single-window year)
    bat_soc_end_penalty::Float64            # Cost per MWh below the floor; <= 0 → 10% of VOLL per time step
    min_cycling_ratio::Float64              # Min battery cycling as fraction of capacity
    min_cycling_period_days::Float64        # Period for min cycling calculation (days)
    reserve_static_default_ratio::Float64   # Default static reserve as fraction of demand
    soc_violation_penalty::Float64          # Penalty for SOC limit violation
    demand_shift_cost_rate::Float64         # Cost rate per hour of shift distance
    dynamic_reserve_contribution::Float64   # Fraction of rated_power for dynamic reserve

    # Hours in this year (always 8760: leap years are modelled as 8760 h)
    hours_per_year::Int

    # PWL loss model: 0=no losses, -1=linear legacy, N>0=PWL with N segments
    pwl_loss_segments::Int

    # Bidding/offer cost curves: gen_index => Dict{bus_index => Vector{CostSegment}}
    # Only populated for generators whose curve changes their price (several
    # blocks, or one block priced away from fuel_cost); a single block prices
    # the output at its own marginal cost. Empty otherwise → zero overhead.
    gen_cost_curves::Dict{Int, Dict{Int, Vector{CostSegment}}}
    bat_cost_curves::Dict{Int, Dict{Int, Vector{CostSegment}}}

    # ACOPF configuration
    # Power flow mode: "dcopf", "dcopf_ac_verify", "acopf_soc", "acopf_qc",
    #                  "acopf_sdp", "acopf_polar", "acopf_rect"
    power_flow_mode::String
    acopf_base_mva::Float64            # Base MVA for ACOPF (100.0)
    acopf_v_min::Float64               # Min voltage p.u. (0.9)
    acopf_v_max::Float64               # Max voltage p.u. (1.1)
    acopf_default_power_factor::Float64 # Default PF for Q limit estimation (0.85)
    acopf_load_power_factor::Float64   # Load PF for reactive demand (0.9)
    acopf_q_slack_penalty::Float64     # Q slack penalty $/MVAr (100.0)
    acopf_min_reactance_pu::Float64    # Min reactance clamp (0.01)
    acopf_tap_ratio_min::Float64       # Tap ratio lower bound (0.5)
    acopf_tap_ratio_max::Float64       # Tap ratio upper bound (2.0)
    acopf_q_min_ratio::Float64         # Q_min = -ratio × Q_max (0.5)
    # Reactive power limits: gen_idx → Vector{Float64} per node (MVAr)
    gen_q_limits::Dict{Int, Vector{Float64}}      # Q_max per gen
    gen_q_limits_min::Dict{Int, Vector{Float64}}   # Q_min per gen

    # Deterministic scheduled interruptions (Capa 1). Keyed by element category:
    #  - position-indexed elements (e.g. "battery") ⇒ [n_elements × hours] Matrix
    #  - id-addressed elements (e.g. "line", multi-stage filtered) ⇒
    #    Dict{String, Vector{Float64}} (element_id → hourly factor)
    # Values are in [0,1] (1.0 = no outage). Empty/missing ⇒ no masking (identical
    # model). Generators fold their outage into `availability` instead.
    outage_masks::Dict{String, Any}

    # Per-system CO2 budgets of a merged multi-system run. Empty = one budget
    # over all nodes from `co2_budget` / `co2_budget_violation_penalty`.
    co2_budget_groups::Vector{CO2BudgetGroup}
end

# Constructor with sensible defaults
function PowerSystemInput(;
    name::String,
    year::Int,
    base_year::Int = year,
    network::NetworkConfig,
    generators::Vector{GeneratorConfig},
    batteries::Vector{BatteryConfig},
    demand::Matrix{Float64},
    sectoral_demand::Dict{String, Matrix{Float64}} = Dict{String, Matrix{Float64}}(),
    temporal::TemporalConfig,
    loss_of_load_penalty::Float64 = 0.01,
    loss_of_reserve_static::Float64 = 0.01,
    loss_of_reserve_dynamic::Float64 = 0.01,
    co2_cost::Float64 = 0.0,
    curtailment_cost::Float64 = 2e-5,   # M$/MWh — already-scaled (\$20/MWh × 1e-6)
    re_penetration_target::Float64 = 0.0,
    co2_budget::Float64 = Inf,
    inertia_limit::Float64 = 0.0,
    mode::String = "economic_dispatch",
    solver_name::String = "highs",
    threads::Int = 4,
    time_limit::Float64 = 3600.0,
    gap::Float64 = 0.01,
    verbose::Bool = false,
    solver_options::Dict{String, Any} = Dict{String, Any}(),
    fuel_co2::Dict{String, Float64} = Dict{String, Float64}(),
    ev_config::Union{EVConfig, Nothing} = nothing,
    electrolyzers::Vector{ElectrolyzerConfig} = ElectrolyzerConfig[],
    # Single-electrolyzer form, kept for older callers; appended to `electrolyzers`.
    electrolyzer_config::Union{ElectrolyzerConfig, Nothing} = nothing,
    sectoral_criticality::Dict{String, Float64} = Dict{String, Float64}(),
    sectoral_delay_tolerance::Dict{String, Int} = Dict{String, Int}(),
    sectoral_flexibility_ratio::Dict{String, Float64} = Dict{String, Float64}(),
    inertia_limit_hourly::Vector{Float64} = Float64[],
    loss_of_inertia_penalty::Float64 = 1.0,
    # N-1 Security parameters (matches Python legacy power_system.py lines 55-61)
    n1_security_enabled::Bool = false,
    n1_transmission_enabled::Bool = false,
    n1_generation_enabled::Bool = false,
    n1_transmission_reserve_factor::Float64 = 0.7,
    n1_generation_reserve_type::String = "largest_unit",
    n1_generation_reserve_percentage::Float64 = 0.15,
    n1_scopf_enabled::Bool = false,
    n1_corrective_enabled::Bool = false,
    # Rooftop solar generation
    rooftop_generation::Union{Matrix{Float64}, Nothing} = nothing,
    # Generator initial status for rolling horizon carry-over
    generator_initial_status::Dict{Int, Dict{Int, Float64}} = Dict{Int, Dict{Int, Float64}}(),
    # Last-timestep gen_output from previous window (for t=1 ramp continuity).
    # Empty = no boundary; preserves legacy behaviour (no t=1 ramp).
    generator_output_prev::Dict{Int, Dict{Int, Float64}} = Dict{Int, Dict{Int, Float64}}(),
    # Last-timestep reservoir level from previous window (MWh-eq).
    # Empty = no boundary; reservoir uses configured initial fraction.
    reservoir_level_prev::Dict{Int, Dict{Int, Float64}} = Dict{Int, Dict{Int, Float64}}(),
    # Battery SOC carried from the previous window (MWh). Empty = soc_initial.
    battery_soc_prev::Dict{Int, Dict{Int, Float64}} = Dict{Int, Dict{Int, Float64}}(),
    # Battery discharge / charge (MW) at the previous window's last step
    battery_discharge_prev::Dict{Int, Dict{Int, Float64}} = Dict{Int, Dict{Int, Float64}}(),
    battery_charge_prev::Dict{Int, Dict{Int, Float64}} = Dict{Int, Dict{Int, Float64}}(),
    # Electricity price vector (hourly) - matches Python legacy self.electricity_price
    electricity_price::Vector{Float64} = Float64[],
    # Penalty coefficients from config (instead of deriving from VOLL)
    fre_penetration_penalty::Float64 = 0.0,       # 0 = fallback to loss_of_load * tres * 100
    transfer_margin_penalty::Float64 = 0.0,        # 0 = fallback to loss_of_load * 0.1
    rooftop_curtailment_penalty::Float64 = 5e-6,
    co2_budget_violation_penalty::Float64 = 5e-4,
    # Reserve requirements per bus from config
    reserve_static_requirement::Dict{Int, Float64} = Dict{Int, Float64}(),
    reserve_dynamic_requirement::Dict{Int, Float64} = Dict{Int, Float64}(),
    # Demand constraints
    max_annual_system_cost::Float64 = Inf,
    # Configurable parameters (previously hardcoded)
    soc_end_tolerance::Float64 = 0.05,
    cyclic_end_soc::Bool = true,
    bat_soc_end_min::Dict{Int, Dict{Int, Float64}} = Dict{Int, Dict{Int, Float64}}(),
    bat_soc_end_min_cyclic::Bool = false,
    bat_soc_end_penalty::Float64 = 0.0,
    min_cycling_ratio::Float64 = 0.8,
    min_cycling_period_days::Float64 = 7.0,
    reserve_static_default_ratio::Float64 = 0.15,
    soc_violation_penalty::Float64 = 1.0,
    demand_shift_cost_rate::Float64 = 1e-7,
    dynamic_reserve_contribution::Float64 = 0.5,
    hours_per_year::Int = hours_in_year(year),
    # PWL loss model: 0=no losses, -1=linear legacy, N>0=PWL with N segments
    pwl_loss_segments::Int = 3,
    # Bidding/offer cost curves (empty = all generators use flat fuel_cost)
    gen_cost_curves::Dict{Int, Dict{Int, Vector{CostSegment}}} = Dict{Int, Dict{Int, Vector{CostSegment}}}(),
    bat_cost_curves::Dict{Int, Dict{Int, Vector{CostSegment}}} = Dict{Int, Dict{Int, Vector{CostSegment}}}(),
    # ACOPF configuration
    power_flow_mode::String = "dcopf",
    acopf_base_mva::Float64 = 100.0,
    acopf_v_min::Float64 = 0.90,
    acopf_v_max::Float64 = 1.10,
    acopf_default_power_factor::Float64 = 0.85,
    acopf_load_power_factor::Float64 = 0.9,
    acopf_q_slack_penalty::Float64 = 1e-4,
    acopf_min_reactance_pu::Float64 = 0.01,
    acopf_tap_ratio_min::Float64 = 0.5,
    acopf_tap_ratio_max::Float64 = 2.0,
    acopf_q_min_ratio::Float64 = 0.5,
    gen_q_limits::Dict{Int, Vector{Float64}} = Dict{Int, Vector{Float64}}(),
    gen_q_limits_min::Dict{Int, Vector{Float64}} = Dict{Int, Vector{Float64}}(),
    outage_masks::Dict{String, Any} = Dict{String, Any}(),
    co2_budget_groups::Vector{CO2BudgetGroup} = CO2BudgetGroup[]
)
    return PowerSystemInput(
        name, year, base_year,
        network, generators, batteries,
        demand, sectoral_demand,
        temporal,
        loss_of_load_penalty,
        loss_of_reserve_static, loss_of_reserve_dynamic,
        co2_cost,
        curtailment_cost,
        re_penetration_target, co2_budget, inertia_limit,
        mode,
        solver_name, threads, time_limit, gap, verbose, solver_options,
        fuel_co2,
        ev_config,
        electrolyzer_config === nothing ? electrolyzers : vcat(electrolyzers, [electrolyzer_config]),
        sectoral_criticality,
        sectoral_delay_tolerance,
        sectoral_flexibility_ratio,
        inertia_limit_hourly,
        loss_of_inertia_penalty,
        n1_security_enabled,
        n1_transmission_enabled,
        n1_generation_enabled,
        n1_transmission_reserve_factor,
        n1_generation_reserve_type,
        n1_generation_reserve_percentage,
        n1_scopf_enabled,
        n1_corrective_enabled,
        rooftop_generation,
        generator_initial_status,
        generator_output_prev,
        reservoir_level_prev,
        battery_soc_prev,
        battery_discharge_prev,
        battery_charge_prev,
        electricity_price,
        fre_penetration_penalty,
        transfer_margin_penalty,
        rooftop_curtailment_penalty,
        co2_budget_violation_penalty,
        reserve_static_requirement,
        reserve_dynamic_requirement,
        max_annual_system_cost,
        soc_end_tolerance,
        cyclic_end_soc,
        bat_soc_end_min,
        bat_soc_end_min_cyclic,
        bat_soc_end_penalty,
        min_cycling_ratio,
        min_cycling_period_days,
        reserve_static_default_ratio,
        soc_violation_penalty,
        demand_shift_cost_rate,
        dynamic_reserve_contribution,
        hours_per_year,
        pwl_loss_segments,
        gen_cost_curves,
        bat_cost_curves,
        power_flow_mode,
        acopf_base_mva,
        acopf_v_min,
        acopf_v_max,
        acopf_default_power_factor,
        acopf_load_power_factor,
        acopf_q_slack_penalty,
        acopf_min_reactance_pu,
        acopf_tap_ratio_min,
        acopf_tap_ratio_max,
        acopf_q_min_ratio,
        gen_q_limits,
        gen_q_limits_min,
        outage_masks,
        co2_budget_groups
    )
end

# =============================================================================
# PowerSystem Variables (Decision Variables Container)
# =============================================================================

"""
    PowerSystemVariables

Container for all JuMP decision variables in the PowerSystem model.

Variables are organized by category. Optional variables (unit commitment,
EVs, reservoirs, ...) are `nothing` when not applicable. The dispatch has no
investment variables: the master's builds reach it as existing capacity.

# Variable Dimensions
- Generation: [generator, node, hour]
- Storage: [battery, node, hour]
- Network: [node, hour] or Dict{(from, to), Vector}
"""
mutable struct PowerSystemVariables
    # Generation (gen × bus × hour) — SparseAxisArray (only active gen-bus pairs)
    gen_output::Any
    gen_status::Any      # Nothing in non-UC mode
    gen_startup::Any     # Nothing in non-UC mode
    gen_shutdown::Any    # Nothing in non-UC mode

    # Curtailment (node × hour) - spilled renewable energy per node
    curtailment::Array{VariableRef, 2}

    # FRE penetration loss slack (node × hour) - RE constraint slack
    fre_penetration_loss::Array{VariableRef, 2}

    # CO2 emissions (node × hour)
    co2_emissions::Union{Array{VariableRef, 2}, Nothing}

    # Storage (bat × node × hour) — SparseAxisArray (only active bat-bus pairs)
    bat_charge::Any
    bat_discharge::Any
    bat_soc::Any
    bat_charge_status::Any      # Nothing in non-UC mode
    soc_violation::Any           # Nothing if not needed
    bat_spillage::Any            # Nothing if not needed

    # Transmission: (from_bus, to_bus) pair → indices into power_flow_by_line
    # of every branch (line or transformer) between the pair, so parallel
    # branches are summed wherever a pair flow is meant (see `pair_flow`)
    pair_lines::Dict{Tuple{Int,Int}, Vector{Int}}
    # Per physical line flow variables (line_index → hour vector) — for parallel lines
    power_flow_by_line::Union{Vector{Vector{VariableRef}}, Nothing}
    voltage_angle::Array{VariableRef, 2}  # node × hour
    # Transfer margin violation (from × to × hour)
    # Slack variable for transmission capacity violations
    transfer_margin::Union{Dict{Tuple{Int,Int}, Vector{VariableRef}}, Nothing}

    # Reserves (node × hour)
    reserve_static::Array{VariableRef, 2}
    reserve_dynamic::Array{VariableRef, 2}
    reserve_static_loss::Array{VariableRef, 2}
    reserve_dynamic_loss::Array{VariableRef, 2}

    # Load shedding (node × hour)
    load_shed::Array{VariableRef, 2}

    # EV variables (node × hour) - optional
    ev_charging::Union{Array{VariableRef, 2}, Nothing}
    ev_v2g::Union{Array{VariableRef, 2}, Nothing}
    ev_soc::Union{Array{VariableRef, 2}, Nothing}  # node × (hour+1)
    ev_loss::Union{Array{VariableRef, 2}, Nothing}

    # Electrolyzer power (electrolyzer × bus × hour) - optional
    electrolyzer_power::Union{Array{VariableRef, 3}, Nothing}

    # Inertia (hour) - optional
    loss_of_inertia::Union{Vector{VariableRef}, Nothing}

    # Constraint references (for dual extraction)
    balance_constraints::Union{Dict{Tuple{Int,Int}, Any}, Nothing}

    # Sectoral load shedding (sector -> node × hour)
    loss_of_load_sectoral::Union{Dict{String, Matrix{VariableRef}}, Nothing}

    # CO2 budget violation slack (t), one per budget group with a budget
    # (group index => variable; see `co2_budget_groups`)
    co2_budget_violation::Union{Dict{Int, VariableRef}, Nothing}

    # Rooftop solar curtailment (node × hour)
    rooftop_curtailment::Union{Array{VariableRef, 2}, Nothing}

    # EV charge/discharge mutual exclusivity status (node × hour)
    ev_charge_status::Union{Array{VariableRef, 2}, Nothing}

    # Demand shifting variables - sparse t-to-t_dest pairs (P3)
    # sector → Dict{(bus, t, t_dest) => VariableRef}
    # Matches Python legacy flexible_demand_shifted[sector][node][t][t_dest]
    demand_shift::Union{Dict{String, Dict{Tuple{Int,Int,Int}, VariableRef}}, Nothing}

    # AC/DC converter variables (converter × hour)
    acdc_rectify::Union{Matrix{VariableRef}, Nothing}   # AC→DC power per converter per hour
    acdc_invert::Union{Matrix{VariableRef}, Nothing}    # DC→AC power per converter per hour

    # Frequency converter variables (converter × hour)
    freq_flow_a_to_b::Union{Matrix{VariableRef}, Nothing}  # A→B power per converter per hour
    freq_flow_b_to_a::Union{Matrix{VariableRef}, Nothing}  # B→A power per converter per hour

    # Reservoir hydroelectric variables (gen × node × hour) — SparseAxisArray
    reservoir_level::Any         # Water level (MWh-eq), dim3 = hours+1
    reservoir_spillage::Any      # Overflow (MW-eq)
    reservoir_pump::Any          # Pump-back power consumed (MW)

    # Sparse lookup maps for gen/bat active bus pairs
    buses_of_gen::Vector{Vector{Int}}   # gen_idx → [active bus indices]
    gens_at_bus::Vector{Vector{Int}}    # bus_idx → [gen indices with capacity here]
    buses_of_bat::Vector{Vector{Int}}   # bat_idx → [active bus indices]
    bats_at_bus::Vector{Vector{Int}}    # bus_idx → [bat indices with capacity here]

    # Segment output variables for PWL cost curves (bidding curves)
    # Only populated for units with a curve (generators: one segment or more;
    # batteries: more than one); empty otherwise.
    # (gen/bat_idx, bus) → Matrix{VariableRef} [segment × hour]
    gen_seg_output::Dict{Tuple{Int,Int}, Any}
    bat_seg_discharge::Dict{Tuple{Int,Int}, Any}

    # ACOPF variables (nothing when using DCOPF)
    acopf_vars::Any  # Union{ACOPFVariables, Nothing}

    # Transmission loss per line (line × hour, MW) of the DC power flow's
    # loss model; nothing when the flow is lossless or not DC
    line_losses::Union{Vector{Vector{VariableRef}}, Nothing}
end

# Constructor with optional fields defaulting to nothing
function PowerSystemVariables(
    gen_output, gen_status, gen_startup, gen_shutdown,
    curtailment, fre_penetration_loss,
    bat_charge, bat_discharge, bat_soc,
    pair_lines, voltage_angle,
    reserve_static, reserve_dynamic, reserve_static_loss, reserve_dynamic_loss,
    load_shed,
    buses_of_gen, gens_at_bus, buses_of_bat, bats_at_bus;
    power_flow_by_line = nothing,
    co2_emissions = nothing,
    bat_charge_status = nothing,
    soc_violation = nothing,
    bat_spillage = nothing,
    transfer_margin = nothing,
    ev_charging = nothing,
    ev_v2g = nothing,
    ev_soc = nothing,
    ev_loss = nothing,
    electrolyzer_power = nothing,
    loss_of_inertia = nothing,
    balance_constraints = nothing,
    loss_of_load_sectoral = nothing,
    co2_budget_violation = nothing,
    rooftop_curtailment = nothing,
    ev_charge_status = nothing,
    demand_shift = nothing,
    acdc_rectify = nothing,
    acdc_invert = nothing,
    freq_flow_a_to_b = nothing,
    freq_flow_b_to_a = nothing,
    reservoir_level = nothing,
    reservoir_spillage = nothing,
    reservoir_pump = nothing,
    gen_seg_output = Dict{Tuple{Int,Int}, Any}(),
    bat_seg_discharge = Dict{Tuple{Int,Int}, Any}(),
    acopf_vars = nothing,
    line_losses = nothing
)
    return PowerSystemVariables(
        gen_output, gen_status, gen_startup, gen_shutdown,
        curtailment, fre_penetration_loss, co2_emissions,
        bat_charge, bat_discharge, bat_soc, bat_charge_status, soc_violation, bat_spillage,
        pair_lines, power_flow_by_line, voltage_angle, transfer_margin,
        reserve_static, reserve_dynamic, reserve_static_loss, reserve_dynamic_loss,
        load_shed,
        ev_charging, ev_v2g, ev_soc, ev_loss,
        electrolyzer_power,
        loss_of_inertia,
        balance_constraints,
        loss_of_load_sectoral,
        co2_budget_violation,
        rooftop_curtailment,
        ev_charge_status,
        demand_shift,
        acdc_rectify,
        acdc_invert,
        freq_flow_a_to_b,
        freq_flow_b_to_a,
        reservoir_level,
        reservoir_spillage,
        reservoir_pump,
        buses_of_gen,
        gens_at_bus,
        buses_of_bat,
        bats_at_bus,
        gen_seg_output,
        bat_seg_discharge,
        acopf_vars,
        line_losses
    )
end

# =============================================================================
# PowerSystem Result (Solution Output)
# =============================================================================

"""
    CostBreakdown

Granular decomposition of the operational cost from `build_objective!`.
Energy-based costs are already scaled by `temporal_resolution_hours`.
"""
struct CostBreakdown
    fuel_cost::Float64
    fixed_om_cost::Float64
    maintenance_cost::Float64
    startup_cost::Float64
    battery_maintenance_cost::Float64
    battery_degradation_cost::Float64
    load_shedding_cost::Float64
    curtailment_cost::Float64
    reserve_static_cost::Float64
    reserve_dynamic_cost::Float64
    co2_emission_cost::Float64
    fre_penetration_cost::Float64
    inertia_cost::Float64
    soc_violation_cost::Float64
    transfer_margin_cost::Float64
    v2g_compensation::Float64
    electrolyzer_cost::Float64
    converter_cost::Float64
    spillage_cost::Float64
    reservoir_spillage_cost::Float64
    demand_shift_cost::Float64
    rooftop_curtailment_cost::Float64
    # PrimaryEnergy sub-costs (merged into cost_expressions by
    # PrimaryEnergyAdapter.integrate_with_power_system).
    pe_supply_cost::Float64
    pe_loss_cost::Float64
    pe_excess_cost::Float64
    pe_transport_cost::Float64
    pe_investment_cost::Float64
    # The electrolyzers' cost (electrolyzer_cost) when their hydrogen fills the
    # primary-energy Hydrogen tank; a view of it, not an extra cost.
    pe_electrolyzer_cost::Float64
    # N-1 SCOPF reliability-shortfall penalty.
    n1_security_shortfall_cost::Float64
    # Flow cost of lines with a flow_cost (inter-system links, × |flow|).
    line_flow_cost::Float64
    total::Float64
end


"""
    PowerSystemResult

Solution output from the PowerSystem optimization model: all solution values
and metrics (the dispatch makes no investment decisions).
"""
struct PowerSystemResult
    # Solve status
    status::MOI.TerminationStatusCode
    objective::Float64
    solve_time::Float64

    # Generation (gen × node × hour)
    gen_output::Array{Float64, 3}
    gen_status::Union{Array{Float64, 3}, Nothing}
    gen_startup::Union{Array{Float64, 3}, Nothing}
    gen_shutdown::Union{Array{Float64, 3}, Nothing}

    # Curtailment (node × hour) - spilled renewable energy
    curtailment::Array{Float64, 2}
    total_curtailment::Float64

    # Storage
    bat_charge::Array{Float64, 3}
    bat_discharge::Array{Float64, 3}
    bat_soc::Array{Float64, 3}

    # Transmission
    power_flow::Dict{Tuple{Int,Int}, Vector{Float64}}
    power_flow_by_line::Union{Vector{Vector{Float64}}, Nothing}  # Per physical line flows
    voltage_angle::Matrix{Float64}
    # ACOPF AC-specific outputs (filled when power_flow_mode is acopf_*).
    # Default to nothing for DC runs.
    voltage_magnitude::Union{Matrix{Float64}, Nothing}              # [bus × hour] in p.u.
    reactive_generation::Union{Array{Float64, 3}, Nothing}          # [gen × bus × hour] in MVAr

    # Reserves (node × hour)
    reserve_static::Matrix{Float64}
    reserve_dynamic::Matrix{Float64}
    reserve_static_loss::Matrix{Float64}
    reserve_dynamic_loss::Matrix{Float64}

    # Load shedding (node × hour)
    load_shed::Matrix{Float64}

    # CO2 emission rate, tCO2/h (bus × step); × resolution_hours for tonnes
    co2_emissions::Matrix{Float64}

    # Dual prices (node × hour)
    energy_prices::Matrix{Float64}

    # System metrics: energy totals in MWh and total_co2 in tCO2 over the
    # window (step rates × resolution_hours)
    total_generation::Float64
    total_demand::Float64
    total_losses::Float64
    re_penetration::Float64      # renewable over total generation (is_renewable_unit), committed steps
    total_co2::Float64
    load_shed_total::Float64

    # Battery spillage (bat × node × hour)
    bat_spillage::Union{Array{Float64, 3}, Nothing}

    # EV variables (node × hour)
    ev_charging::Union{Matrix{Float64}, Nothing}
    ev_v2g::Union{Matrix{Float64}, Nothing}
    ev_soc::Union{Matrix{Float64}, Nothing}
    ev_loss::Union{Matrix{Float64}, Nothing}

    # System-wide variables
    loss_of_inertia::Union{Vector{Float64}, Nothing}

    # Transfer margin (node-pair → hours)
    transfer_margin::Union{Dict{Tuple{Int,Int}, Vector{Float64}}, Nothing}

    # Reservoir hydroelectric results (gen × node × hour)
    reservoir_level::Union{Array{Float64, 3}, Nothing}
    reservoir_spillage::Union{Array{Float64, 3}, Nothing}
    reservoir_pump::Union{Array{Float64, 3}, Nothing}

    # N-1 security results
    n1_gen_reserve_duals::Union{Vector{Float64}, Nothing}       # [hour] — dual of gen N-1 reserve
    n1_trans_reserve_duals::Union{Dict{Tuple{Int,Int,Int}, Vector{Float64}}, Nothing}  # (line,outage,dir) → [hour]
    n1_binding_contingencies::Union{Vector{String}, Nothing}    # names of binding contingencies
    n1_security_cost::Float64                                   # incremental cost of N-1 constraints

    # Granular cost decomposition (populated after solve)
    cost_breakdown::Union{CostBreakdown, Nothing}

    # Electrolyzers (electrolyzer × bus × hour): power drawn (MW) and
    # hydrogen produced (kg/h, see electrolyzer_h2_per_mwh)
    electrolyzer_power::Union{Array{Float64, 3}, Nothing}
    electrolyzer_h2::Union{Array{Float64, 3}, Nothing}
end


# =============================================================================
# Primary Energy Types
# =============================================================================

"""
    FuelConfig

Configuration for a primary energy fuel/source.

# Fields
- `name::String`: Fuel identifier (e.g., "Gas", "Diesel", "Hydrogen")
- `price_base::Float64`: Base price (\$/unit)
- `price_growth_rate::Float64`: Annual price growth rate
- `energy_content::Float64`: Energy content (MWh_th/physical unit)
- `emission_factor::Float64`: CO2 emissions (tonnes/MWh_th)
- `max_availability::Vector{Float64}`: Max annual availability per node (units/year)
- `storage_capacity::Vector{Float64}`: Storage capacity per node (units)
- `initial_storage_level::Vector{Float64}`: Initial storage as fraction (0-1)
- `min_storage_level::Float64`: Minimum storage level fraction
- `import_cost::Vector{Float64}`: Import cost per node (\$/unit)
- `transport_cost::Float64`, `transport_losses::Float64`: Transport cost
  (\$/unit/km) and losses (%/100km) for the routes the legacy
  distance-matrix `PrimaryEnergyInput` constructor creates. Runs pass routes
  (`TransportRoute`), whose own `FuelRouteParams` carry cost and losses.
"""
struct FuelConfig
    name::String
    price_base::Float64
    price_growth_rate::Float64
    energy_content::Float64
    emission_factor::Float64
    max_availability::Vector{Float64}
    storage_capacity::Vector{Float64}
    initial_storage_level::Vector{Float64}
    min_storage_level::Float64
    import_cost::Vector{Float64}
    transport_cost::Float64
    transport_losses::Float64
    # Source -> plant-tank replenishment lead time, in days per 100 km of route
    # distance (symmetric with transport_losses). 0 = instantaneous (the prior
    # behaviour). The per-route delay in time steps is derived from this and
    # the route's distance; shipments reach the destination tank that much later.
    transport_transit_days_per_100km::Float64
    # Source disruption: during the hour window [disruption_start_hour,
    # disruption_end_hour) the source's availability is scaled to
    # disruption_availability (0 = full cut, 1 = none). end <= start disables it.
    # Hours are absolute horizon hours (hour 0 = Jan 1 of base_year, 8760 h
    # per modelled year), like scheduled outage windows.
    # Lets a supply cut (terminal down for N days) be configured; combined with
    # the transit lead time and a finite tank it reproduces supply stress.
    disruption_start_hour::Int
    disruption_end_hour::Int
    disruption_availability::Float64
end

# Backward-compatible constructors. Older call sites pass fewer positional args;
# the newer fields take neutral defaults (transit = 0 instantaneous; no
# disruption). Dispatch is by argument count (12 / 13 / full 16).

# 12-arg: through transport_losses.
function FuelConfig(
    name, price_base, price_growth_rate, energy_content, emission_factor,
    max_availability, storage_capacity, initial_storage_level, min_storage_level,
    import_cost, transport_cost, transport_losses,
)
    return FuelConfig(
        name, price_base, price_growth_rate, energy_content, emission_factor,
        max_availability, storage_capacity, initial_storage_level, min_storage_level,
        import_cost, transport_cost, transport_losses, 0.0, 0, 0, 1.0,
    )
end

# 13-arg: through transport_transit_days_per_100km (no disruption).
function FuelConfig(
    name, price_base, price_growth_rate, energy_content, emission_factor,
    max_availability, storage_capacity, initial_storage_level, min_storage_level,
    import_cost, transport_cost, transport_losses, transport_transit_days_per_100km,
)
    return FuelConfig(
        name, price_base, price_growth_rate, energy_content, emission_factor,
        max_availability, storage_capacity, initial_storage_level, min_storage_level,
        import_cost, transport_cost, transport_losses, transport_transit_days_per_100km,
        0, 0, 1.0,
    )
end

"""
    FuelInfrastructureConfig

Configuration for fuel transport and storage infrastructure.

# Fields
- `transport_capacity::Float64`: Daily transport capacity (units/day)
- `transport_investment_cost::Float64`: Investment cost (\$/unit-day/km)
- `transport_expansion_limit::Float64`: Maximum expansion factor
- `storage_investment_cost::Float64`: Storage investment cost (\$/unit)
- `storage_expansion_limit::Float64`: Maximum storage expansion factor
- `storage_efficiency::Float64`: Storage round-trip efficiency
- `lifetime_transport::Float64`: Transport infrastructure lifetime (years)
- `lifetime_storage::Float64`: Storage infrastructure lifetime (years)
- `max_hourly_dispatch_rate::Float64`: Max hourly dispatch as fraction of capacity (-1.0 = no limit)
"""
struct FuelInfrastructureConfig
    transport_capacity::Float64
    transport_investment_cost::Float64
    transport_expansion_limit::Float64
    storage_investment_cost::Float64
    storage_expansion_limit::Float64
    storage_efficiency::Float64
    lifetime_transport::Float64
    lifetime_storage::Float64
    max_hourly_dispatch_rate::Float64  # -1.0 means no limit
end

"""
    FuelRouteParams

Per-fuel transport parameters for a specific route.

# Fields
- `capacity::Float64`: Daily transport capacity for this fuel on this route (units/day)
- `transport_cost::Float64`: Transport cost per unit per km (\$/unit/km)
- `transport_losses::Float64`: Loss fraction per 100 km
"""
struct FuelRouteParams
    capacity::Float64
    transport_cost::Float64
    transport_losses::Float64
end

"""
    TransportRoute

A unidirectional fuel transport route between two nodes.

# Fields
- `route_id::String`: Unique route identifier
- `from_node::Int`: Origin node (1-indexed)
- `to_node::Int`: Destination node (1-indexed)
- `distance_km::Float64`: Route distance in km
- `fuel_params::Dict{String, FuelRouteParams}`: Per-fuel parameters on this route
"""
struct TransportRoute
    route_id::String
    from_node::Int
    to_node::Int
    distance_km::Float64
    fuel_params::Dict{String, FuelRouteParams}
end

"""
    NonElectricDemandConfig

Configuration for non-electric fuel demand by sector.

# Fields
- `sector::String`: Sector name (e.g., "Industrial", "Transport", "Residential")
- `fuel::String`: Fuel type
- `annual_demand::Vector{Float64}`: Annual demand per node (units/year)
- `growth_rate::Float64`: Annual growth rate
- `seasonal_factors::Vector{Float64}`: Monthly seasonal factors (12 values, sum to 1)
"""
struct NonElectricDemandConfig
    sector::String
    fuel::String
    annual_demand::Vector{Float64}
    growth_rate::Float64
    seasonal_factors::Vector{Float64}
end

"""
    PrimaryEnergyInput

Complete input for the Primary Energy optimization model.

# Fields
- `year::Int`: Simulation year
- `base_year::Int`: Base year for growth calculations
- `num_nodes::Int`: Number of nodes
- `hours::Int`: Total simulation hours
- `fuels::Vector{FuelConfig}`: Fuel configurations
- `infrastructure::Dict{String, FuelInfrastructureConfig}`: Infrastructure per fuel
- `non_electric_demand::Vector{NonElectricDemandConfig}`: Non-electric demands
- `transport_routes::Vector{TransportRoute}`: Fuel transport routes with per-fuel parameters
- `generator_fuel_map::Dict{Int, Tuple{String, Float64, Float64, Float64}}`:
    Generator to fuel mapping {gen_idx => (fuel_id, MWhe/unit, MWhth/unit, efficiency)}
- `primary_energy_resolution::Int`: Resolution for planning (hours)
- `investment_resolution::Int`: Resolution for investment decisions (hours)
- `discount_rate::Float64`: Discount rate for annualization
- `loss_of_fuel_supply_penalty::Float64`: Penalty for fuel shortfall (\$/unit)
- `mode::String`: The run's simulation mode ("development", "economic_dispatch",
    "unit_commitment"; "master" for a representative day of the master), not
    the dispatch formulation: a development run's windows get "development",
    which ends each window with its tanks at least 10 % full
- `cumulative_capacities::Dict`: Fuel storage/transport capacity built by the
    master: {"storage"|"transport" => {fuel => {node or route => capacity}}}
- `initial_storage_levels::Union{Dict{String, Vector{Float64}}, Nothing}`: Initial storage levels
- `generator_rated_power::Dict{Int, Vector{Float64}}`: Rated power per node for each gen in fuel map
    Used to skip zero-capacity nodes in PE coupling
- `scheduled_arrivals::Union{Nothing, Dict{String, Matrix{Float64}}}`: fuel
    shipped in an earlier window that is still in transit at this window's
    start: fuel => [node, step], arriving at the destination tank in that
    step (net of transport losses; `in_transit_arrivals` builds it)
"""
struct PrimaryEnergyInput
    year::Int
    base_year::Int
    num_nodes::Int
    hours::Int
    fuels::Vector{FuelConfig}
    infrastructure::Dict{String, FuelInfrastructureConfig}
    non_electric_demand::Vector{NonElectricDemandConfig}
    transport_routes::Vector{TransportRoute}
    generator_fuel_map::Dict{Int, Tuple{String, Float64, Float64, Float64}}
    primary_energy_resolution::Int
    investment_resolution::Int
    discount_rate::Float64
    loss_of_fuel_supply_penalty::Float64
    mode::String
    cumulative_capacities::Dict{String, Any}
    initial_storage_levels::Union{Dict{String, Vector{Float64}}, Nothing}
    # Rated power per node for generators in fuel map, used to skip zero-capacity
    # nodes in PE coupling
    generator_rated_power::Dict{Int, Vector{Float64}}
    # Penalty per unit of non-electric fuel demand left unmet (per sector)
    non_electric_demand_loss_penalty::Float64
    # Length of a time step (h) and the window's first hour within the year:
    # place each period in its month and prorate annual quantities
    step_hours::Int
    year_hour_offset::Int
    # Fuel still in transit from earlier windows: fuel => [node, step] arrivals
    scheduled_arrivals::Union{Nothing, Dict{String, Matrix{Float64}}}
end

# Without scheduled arrivals: no fuel in transit at the window's start.
PrimaryEnergyInput(args::Vararg{Any, 20}) = PrimaryEnergyInput(args..., nothing)

# Without the last four fields: unmet non-electric demand at the fuel-supply
# penalty, 1 h steps, window at the start of the year.
function PrimaryEnergyInput(
    year::Int, base_year::Int, num_nodes::Int, hours::Int,
    fuels::Vector{FuelConfig},
    infrastructure::Dict{String, FuelInfrastructureConfig},
    non_electric_demand::Vector{NonElectricDemandConfig},
    transport_routes::Vector{TransportRoute},
    generator_fuel_map::Dict{Int, Tuple{String, Float64, Float64, Float64}},
    primary_energy_resolution::Int, investment_resolution::Int,
    discount_rate::Float64, loss_of_fuel_supply_penalty::Float64,
    mode::String,
    cumulative_capacities::Dict{String, Any},
    initial_storage_levels::Union{Dict{String, Vector{Float64}}, Nothing},
    generator_rated_power::Dict{Int, Vector{Float64}}
)
    return PrimaryEnergyInput(
        year, base_year, num_nodes, hours, fuels, infrastructure, non_electric_demand,
        transport_routes, generator_fuel_map, primary_energy_resolution,
        investment_resolution, discount_rate, loss_of_fuel_supply_penalty,
        mode, cumulative_capacities, initial_storage_levels,
        generator_rated_power, loss_of_fuel_supply_penalty, 1, 0
    )
end

"""
    _auto_routes_from_distances(dist, n, fuels, infrastructure)

Generate transport routes from a legacy distance matrix, using global FuelConfig defaults.
Each non-zero distance entry (i,j) with j>i generates two unidirectional routes.
"""
function _auto_routes_from_distances(
    dist::Matrix{Float64}, n::Int,
    fuels::Vector{FuelConfig},
    infrastructure::Dict{String, FuelInfrastructureConfig}
)
    routes = TransportRoute[]
    for i in 1:n
        for j in (i+1):n
            if dist[i, j] > 0
                fparams = Dict{String, FuelRouteParams}()
                for f in fuels
                    if haskey(infrastructure, f.name)
                        inf = infrastructure[f.name]
                        fparams[f.name] = FuelRouteParams(
                            inf.transport_capacity,
                            f.transport_cost,
                            f.transport_losses
                        )
                    end
                end
                push!(routes, TransportRoute("auto_$(i)_$(j)", i, j, dist[i, j], fparams))
                push!(routes, TransportRoute("auto_$(j)_$(i)", j, i, dist[i, j], fparams))
            end
        end
    end
    return routes
end

# Backward-compatible constructor: accepts distance matrix, auto-generates routes
function PrimaryEnergyInput(
    year::Int, base_year::Int, num_nodes::Int, hours::Int,
    fuels::Vector{FuelConfig},
    infrastructure::Dict{String, FuelInfrastructureConfig},
    non_electric_demand::Vector{NonElectricDemandConfig},
    transport_distances::Matrix{Float64},  # OLD field type
    generator_fuel_map::Dict{Int, Tuple{String, Float64, Float64, Float64}},
    primary_energy_resolution::Int,
    investment_resolution::Int,
    discount_rate::Float64,
    loss_of_fuel_supply_penalty::Float64,
    mode::String,
    cumulative_capacities::Dict{String, Any},
    initial_storage_levels::Union{Dict{String, Vector{Float64}}, Nothing},
    generator_rated_power::Dict{Int, Vector{Float64}}
)
    routes = _auto_routes_from_distances(transport_distances, num_nodes, fuels, infrastructure)
    return PrimaryEnergyInput(
        year, base_year, num_nodes, hours, fuels, infrastructure, non_electric_demand,
        routes,
        generator_fuel_map, primary_energy_resolution, investment_resolution,
        discount_rate, loss_of_fuel_supply_penalty, mode,
        cumulative_capacities, initial_storage_levels, generator_rated_power,
        loss_of_fuel_supply_penalty, 1, 0
    )
end

"""
    PrimaryEnergyVariables

Container for primary energy decision variables organized by temporal scale.

# Investment Scale
- `transport_capacity_investment`: New transport capacity [fuel, inv_period, route]
- `storage_capacity_investment`: New storage capacity [fuel, inv_period, node]

# Primary Period Scale (planning resolution: supply limits and shipments)
- `fuel_supply_periodic`: Local fuel supply [fuel, node, period]
- `fuel_excess_supply_periodic`: Supply above the period's limit (penalised) [fuel, node, period]
- `fuel_transport_periodic`: Fuel shipped on each route [fuel, route, period]
- `non_electric_consumption_periodic`: NE consumption [fuel, sector, node, period]
- `ne_demand_shortfall_periodic`: Unmet NE demand [fuel, sector, node, period]

# Hourly Operational Scale (the only fuel storage layer)
- `fuel_storage_level_hourly`: Tank level [fuel, node, hour+1]
- `fuel_storage_in_hourly`: Local supply delivered into the tank [fuel, node, hour]
- `fuel_storage_out_hourly`: Fuel drawn from the tank for consumption [fuel, node, hour]
- `fuel_for_power_hourly`: Fuel for power generation [gen_idx, node, hour]
- `non_electric_consumption_hourly`: Hourly NE consumption [fuel, sector, node, hour]
- `fuel_loss_of_supply_hourly`: Consumption not covered by the tank [fuel, node, hour]
"""
mutable struct PrimaryEnergyVariables
    # Investment scale
    transport_capacity_investment::Dict{String, Matrix{VariableRef}}  # fuel => [inv_p, route]
    storage_capacity_investment::Dict{String, Matrix{VariableRef}}  # fuel => [inv_p, node]

    # Primary period scale
    fuel_supply_periodic::Dict{String, Matrix{VariableRef}}  # fuel => [node, period]
    fuel_transport_periodic::Dict{String, Matrix{VariableRef}}  # fuel => [route, period]
    non_electric_consumption_periodic::Dict{Tuple{String, String}, Matrix{VariableRef}}  # (fuel, sector) => [node, period]
    fuel_excess_supply_periodic::Dict{String, Matrix{VariableRef}}  # fuel => [node, period]

    # Hourly operational scale
    fuel_storage_level_hourly::Dict{String, Matrix{VariableRef}}  # fuel => [node, hour+1]
    fuel_storage_in_hourly::Dict{String, Matrix{VariableRef}}  # fuel => [node, hour]
    fuel_storage_out_hourly::Dict{String, Matrix{VariableRef}}  # fuel => [node, hour]
    fuel_for_power_hourly::Dict{Int, Matrix{VariableRef}}  # gen_idx => [node, hour]
    non_electric_consumption_hourly::Dict{Tuple{String, String}, Matrix{VariableRef}}  # (fuel, sector) => [node, hour]
    fuel_loss_of_supply_hourly::Dict{String, Matrix{VariableRef}}  # fuel => [node, hour]
    primary_sector_emissions_hourly::Dict{String, Matrix{VariableRef}}  # fuel => [node, hour]
    total_primary_emissions_hourly::Matrix{VariableRef}  # [node, hour]

    # Unmet non-electric demand per sector
    ne_demand_shortfall_periodic::Dict{Tuple{String, String}, Matrix{VariableRef}}  # (fuel, sector) => [node, period]
end

"""
    PrimaryEnergyResult

Solution output from the Primary Energy model.
"""
struct PrimaryEnergyResult
    # Investment results
    transport_investments::Dict{String, Matrix{Float64}}
    storage_investments::Dict{String, Matrix{Float64}}

    # Periodic aggregates
    total_fuel_supply::Dict{String, Vector{Float64}}  # fuel => [period]
    total_ne_demand_satisfied::Dict{String, Vector{Float64}}  # fuel => [period]
    total_loss_of_supply::Dict{String, Vector{Float64}}  # fuel => [period]

    # Final storage levels for carry-over
    final_storage_levels::Dict{String, Vector{Float64}}  # fuel => [node]

    # Per-route transport flows
    transport_flows::Dict{String, Matrix{Float64}}  # fuel => [route × period]

    # Cost breakdown
    total_fuel_cost::Float64
    total_transport_cost::Float64
    total_loss_penalty::Float64
end

# =============================================================================
# Master Problem Types
# =============================================================================

# ── M2 (#89): fuel block of the master ──────────────────────────────────────
"""
    MasterFuelInput

Primary energy as the master problem sees it (`master_fuel.jl`).

- `pe`: the system's `PrimaryEnergyInput` (fuels, tanks, routes, fuel
  infrastructure, non-electric demand, penalties). Its `generator_fuel_map`
  holds the master's generators under their index and candidate technology
  `tc` under `length(generators) + tc`, like the dispatch's virtual
  generators. `cumulative_capacities` is what earlier master years built
  (myopic runs); `base_year` is the run's first year (fuel price growth,
  disruption hours). The year, length and offset are set per day.
- `electrolyzers`: per master bus, as the dispatch maps them.
"""
struct MasterFuelInput
    pe::PrimaryEnergyInput
    electrolyzers::Vector{ElectrolyzerConfig}
end

"""
    MasterFuelVariables

Fuel infrastructure the master builds, per investment-period start year:
`storage[y][fuel][node]` (fuel units), `transport[y][fuel][route]` (units/day,
indexed like `pe.transport_routes`) and `electrolyzer[y][e][bus]` (MW).
"""
struct MasterFuelVariables
    storage::Dict{Int, Dict{String, Vector{VariableRef}}}
    transport::Dict{Int, Dict{String, Vector{VariableRef}}}
    electrolyzer::Dict{Int, Dict{Int, Vector{VariableRef}}}
end
# ── end M2 ───────────────────────────────────────────────────────────────────

"""
    MasterProblemInput

Input configuration for the Master Problem (capacity expansion planning).

# Fields
## Planning Horizon
- `years::Vector{Int}`: Years in the planning horizon
- `base_year::Int`: Base year for discount calculations

## System Configuration
- `system_name::String`: System identifier
- `network::NetworkConfig`: Network configuration
- `generators::Vector{GeneratorConfig}`: Generator configurations
- `batteries::Vector{BatteryConfig}`: Battery configurations

## Demand and Profiles
- `base_demand::Matrix{Float64}`: Base demand [hour, node]
- `demand_growth::Float64`: Extra annual growth applied on top of each year's slice of `base_demand` (default 0)

## Economic Parameters
- `discount_rate::Float64`: Discount rate for NPV calculations
- `max_annual_investment::Float64`: Maximum annual investment budget (\$)

## Targets
- `target_re_penetration::Float64`: Target RE penetration (final year)
- `initial_re_penetration::Float64`: Initial RE penetration (first year)
- `min_re_increment`, `max_re_increment`: RE-3 bounds. The minimum sets a
  floor `initial + y × min` on the share (`re_required_share`); the maximum
  caps the growth of the ACTUAL share over the previous year's
  (`add_re_growth_cap!`)
- `re_share_anchor::Vector{Float64}`: actual RE share per system the year
  before the first modelled year (a myopic year: the share achieved the year
  before); empty = each system's initial share (`re_share_anchor`)

## Scheduled outages
- `gen_outage_factors`, `bat_outage_factors`: the `outage_schedule` windows
  of the generators (`(g, node)`, node 0 = the whole unit) and batteries
  (`(bi, 0)`) as an availability factor per master step (the index of a
  row of `base_demand`), as the dispatch folds them into availability
  (`master_outage_factor`)

## Penalties
- `loss_of_load_penalty::Float64`: VOLL (cost per MWh not served); also prices
  the capacity adequacy slack (`adequacy_slack_penalty`) and the renewable
  energy above the RE growth cap (`re_cap_penalty`)
- `fre_penetration_loss_penalty::Float64`: cost per MWh of renewable energy
  missing from a system's annual RE target (`add_re_target_constraints!`)

## Operational Settings
- `temporal_resolution_hours::Int`: Hours per timestep (for aggregation)
- `representative_days_per_year::Int`: Number of representative days
- `min_day_separation::Int`: Minimum days between representative days
- `rep_day_offsets::Vector{Int}`: first step of each representative day
  within a year (0-based), the same calendar days for every year
  (`master_rep_days`); empty = chosen from the horizon-mean demand and
  renewable availability (`master_rep_day_offsets`)

## Life Extension
- `life_extension_cost_factor::Float64`: Fraction of investment cost for annual extension
- `decommissioning_cost_factor::Float64`: Fraction of investment cost for decommissioning

## NPV-based retirement
- `base_lcoe::Float64`: revenue per MWh generated in a generator's NPV
- `npv_annual_return_rate::Float64`: a battery's yearly revenue in its NPV,
  as a fraction of its power investment cost
- `npv_retired_units::Vector{RetiredUnit}`: units the NPV iteration retired
  (`retire_units`), decommissioned in the first year

## CO2 budget, start-ups
- `co2_budget_groups::Vector{CO2BudgetGroup}`: Annual CO2 budget per system
  with its own violation penalty (empty = none)
- `unit_commitment::Bool`: The dispatch commits units; the master then has
  LP-relaxed commitment and start-up costs

## Solver
- `threads::Int`: Solver threads
- `time_limit::Float64`: Solver time limit
- `gap::Float64`: MIP gap tolerance
- `verbose::Bool`: Verbose output
"""

"""
    RetiredUnit

An existing unit the NPV iteration retired for the whole horizon
(`retire_units`): `unit_type` "generator" or "battery", its index and the bus
(master location) where it is retired, and the MW it pays decommissioning on
in the first modelled year (0 when that was paid in an earlier myopic year).
"""
struct RetiredUnit
    unit_type::String
    unit_idx::Int
    bus::Int
    decommissioned_mw::Float64
end

"""
    SystemNodeRange

Defines which buses belong to a subsystem for per-system RE constraints.
"""
struct SystemNodeRange
    name::String
    first_bus::Int      # 1-indexed
    num_buses::Int
    initial_re::Float64  # Per-system initial RE penetration
end

struct MasterProblemInput
    # Planning horizon
    years::Vector{Int}
    base_year::Int

    # System configuration
    system_name::String
    network::NetworkConfig
    generators::Vector{GeneratorConfig}
    batteries::Vector{BatteryConfig}

    # Candidate technologies for investment (separate from existing units)
    technologies::Vector{TechnologyConfig}
    battery_technologies::Vector{BatteryTechnologyConfig}

    # Demand
    base_demand::Matrix{Float64}
    demand_growth::Float64

    # Economic
    discount_rate::Float64
    max_annual_investment::Float64

    # Targets
    target_re_penetration::Float64
    initial_re_penetration::Float64

    # RE increment bounds (annual min/max change in RE ratio)
    min_re_increment::Float64
    max_re_increment::Float64

    # Per-system node ranges for per-system RE constraints
    # Empty = single global system (backward compat)
    system_node_ranges::Vector{SystemNodeRange}

    # Penalties (from config, not hardcoded)
    loss_of_load_penalty::Float64       # $/MW not supplied (config: Loss_of_load)
    fre_penetration_loss_penalty::Float64  # $/MWh RE shortfall (config: FRE_penetration_loss)
    max_curtailment_ratio::Float64      # Max curtailment as fraction of RE generation (0.05 = 5%)
    curtailment_cost::Float64           # $/MWh penalty for curtailed RE energy

    # Operational
    temporal_resolution_hours::Int
    representative_days_per_year::Int
    min_day_separation::Int
    investment_resolution_hours::Int

    # TSAM configuration
    use_tsam::Bool
    tsam_period_start_hours::Vector{Vector{Int}}     # [year_idx] => [period start hours]
    tsam_period_weights::Vector{Vector{Float64}}      # [year_idx] => [weight per period]
    tsam_chronological_order::Vector{Vector{Int}}     # [year_idx] => [period indices in chrono order]
    tsam_inter_period_linking::Bool

    # Life extension (NOTE: Julia-only feature, may be removed for Python parity)
    life_extension_cost_factor::Float64
    decommissioning_cost_factor::Float64

    # Sectoral demand for operational subproblems (M2)
    # sector_name => Matrix{Float64} (hours × nodes) - full annual demand per sector
    sectoral_demand::Dict{String, Matrix{Float64}}
    # sector_name => criticality weight (higher = more expensive to shed)
    sectoral_criticality::Dict{String, Float64}

    # Configurable parameters (previously hardcoded)
    reserve_margin::Float64             # Capacity adequacy reserve margin (e.g. 1.15 = 15% margin)
    # NPV-based retirement (calculate_unit_npv, solve_with_npv_iteration)
    npv_annual_return_rate::Float64     # Battery NPV revenue per year as a fraction of its power investment cost
    base_lcoe::Float64                  # Revenue per MWh a generator produces in the NPV ($/MWh)

    # Hours per year (indexed by year_idx; always 8760, leap years included)
    hours_per_year::Vector{Int}

    # Reserve parameters (for operational constraints in master)
    reserve_static_default_ratio::Float64   # Default static reserve as fraction of demand
    reserve_static_requirement::Dict{Int, Float64}  # Per-bus static reserve (MW)
    reserve_dynamic_requirement::Dict{Int, Float64}  # Per-bus dynamic reserve (MW)
    dynamic_reserve_contribution::Float64   # Fraction of rated_power for dynamic reserve
    loss_of_reserve_static::Float64         # Penalty $/MW for unmet static reserve
    loss_of_reserve_dynamic::Float64        # Penalty $/MW for unmet dynamic reserve

    # Inertia parameters
    inertia_limit::Float64                  # System inertia requirement (MWs)
    loss_of_inertia_penalty::Float64        # Penalty $/MWs for inertia shortfall

    # CO2
    fuel_co2::Dict{String, Float64}        # Fuel → CO2 factor (t/MWh_fuel)
    co2_cost::Float64                       # $/t CO2

    # Transmission line data (for DC power flow in master)
    transmission_lines::Vector{Tuple{Int,Int}}      # (from_bus, to_bus) per physical line
    transmission_reactances::Vector{Float64}        # Per-line reactance (pu)
    transmission_capacities::Vector{Float64}        # Per-line MW capacity
    transmission_resistances::Vector{Float64}       # Per-line resistance (pu) for PWL losses
    transmission_loss_segments::Int                  # PWL segments (0=no losses, -1=linear, N>0=PWL)

    # Solver
    solver_name::String
    threads::Int
    time_limit::Float64
    gap::Float64
    verbose::Bool
    solver_options::Dict{String, Any}

    # Bidding/offer cost curves for generators and candidate technologies
    gen_cost_curves::Dict{Int, Dict{Int, Vector{CostSegment}}}
    tech_cost_curves::Dict{Int, Dict{Int, Vector{CostSegment}}}
    bat_cost_curves::Dict{Int, Dict{Int, Vector{CostSegment}}}

    # RE targets (system, year) that replace the RE-1 trajectory: a myopic
    # run solves one year at a time, so its year's target comes from the
    # whole horizon's trajectory. Empty = RE-1.
    re_target_override::Dict{Tuple{Int,Int}, Float64}

    # --- CO2 budget, start-ups (as in the dispatch) ---
    # Annual CO2 budgets, one per system (empty = no budget). Each pays its
    # own violation penalty, as the dispatch's `co2_budget_groups`.
    co2_budget_groups::Vector{CO2BudgetGroup}
    # The dispatch commits units (unit commitment): the master's day
    # operation then has LP-relaxed commitment and start-up costs.
    unit_commitment::Bool
    # --- end ---
    # M2 (#89): fuel balance, fuel infrastructure and electrolyzers; nothing
    # when primary energy is off (master_fuel.jl)
    primary_energy::Union{Nothing, MasterFuelInput}
    # Units the NPV iteration retired (retire_units): their capacity is
    # already 0 in `generators` / `batteries`; this charges their
    # decommissioning in the first year (build_annual_asset_cost)
    npv_retired_units::Vector{RetiredUnit}
    # Actual RE share per system the year before the first modelled year,
    # from which year 1's growth cap is measured (re_share_anchor); a myopic
    # year passes the share the previous year achieved. Empty = each
    # system's initial share.
    re_share_anchor::Vector{Float64}
    # First step (0-based, within a year) of each representative day, the
    # same for every year (master_rep_days); empty = chosen from the
    # horizon-mean demand and renewable availability (master_rep_day_offsets)
    rep_day_offsets::Vector{Int}
    # Scheduled outages (outage_schedule) as availability factors per master
    # step (index = step, as rows of `base_demand`): `(generator, node)` →
    # factor, node 0 for all of the unit's buses; `(battery, 0)` → factor.
    # Empty = no outage (master_outage_factor).
    gen_outage_factors::Dict{Tuple{Int,Int}, Vector{Float64}}
    bat_outage_factors::Dict{Tuple{Int,Int}, Vector{Float64}}
end

# Constructor with defaults
function MasterProblemInput(;
    years::Vector{Int},
    base_year::Int = years[1],
    system_name::String = "primary",
    network::NetworkConfig,
    generators::Vector{GeneratorConfig},
    batteries::Vector{BatteryConfig},
    technologies::Vector{TechnologyConfig} = TechnologyConfig[],
    battery_technologies::Vector{BatteryTechnologyConfig} = BatteryTechnologyConfig[],
    base_demand::Matrix{Float64},
    demand_growth::Float64 = 0.0,
    discount_rate::Float64 = 0.05,
    max_annual_investment::Float64 = 1e4,
    target_re_penetration::Float64 = 1.0,
    initial_re_penetration::Float64 = 0.0,
    min_re_increment::Float64 = 0.0,
    max_re_increment::Float64 = 1.0,
    system_node_ranges::Vector{SystemNodeRange} = SystemNodeRange[],
    loss_of_load_penalty::Float64 = 10.0,
    fre_penetration_loss_penalty::Float64 = 1e-4,
    max_curtailment_ratio::Float64 = 0.05,
    curtailment_cost::Float64 = 2e-5,
    temporal_resolution_hours::Int = 1,
    representative_days_per_year::Int = 2,
    min_day_separation::Int = 30,
    investment_resolution_hours::Int = 8760,
    use_tsam::Bool = false,
    tsam_period_start_hours::Vector{Vector{Int}} = Vector{Int}[],
    tsam_period_weights::Vector{Vector{Float64}} = Vector{Float64}[],
    tsam_chronological_order::Vector{Vector{Int}} = Vector{Int}[],
    tsam_inter_period_linking::Bool = true,
    life_extension_cost_factor::Float64 = 0.05,
    decommissioning_cost_factor::Float64 = 0.10,
    sectoral_demand::Dict{String, Matrix{Float64}} = Dict{String, Matrix{Float64}}(),
    sectoral_criticality::Dict{String, Float64} = Dict{String, Float64}(),
    reserve_margin::Float64 = 1.15,
    npv_annual_return_rate::Float64 = 0.15,
    base_lcoe::Float64 = 9.3e-5,
    hours_per_year::Vector{Int} = [hours_in_year(y) for y in years],
    reserve_static_default_ratio::Float64 = 0.15,
    reserve_static_requirement::Dict{Int, Float64} = Dict{Int, Float64}(),
    reserve_dynamic_requirement::Dict{Int, Float64} = Dict{Int, Float64}(),
    dynamic_reserve_contribution::Float64 = 0.5,
    loss_of_reserve_static::Float64 = 0.01,
    loss_of_reserve_dynamic::Float64 = 0.01,
    inertia_limit::Float64 = 0.0,
    loss_of_inertia_penalty::Float64 = 0.01,
    fuel_co2::Dict{String, Float64} = Dict{String, Float64}(),
    co2_cost::Float64 = 0.0,
    transmission_lines::Vector{Tuple{Int,Int}} = Tuple{Int,Int}[],
    transmission_reactances::Vector{Float64} = Float64[],
    transmission_capacities::Vector{Float64} = Float64[],
    transmission_resistances::Vector{Float64} = Float64[],
    transmission_loss_segments::Int = 2,
    solver_name::String = "highs",
    threads::Int = 4,
    time_limit::Float64 = 3600.0,
    gap::Float64 = 0.01,
    verbose::Bool = false,
    solver_options::Dict{String, Any} = Dict{String, Any}(),
    gen_cost_curves::Dict{Int, Dict{Int, Vector{CostSegment}}} = Dict{Int, Dict{Int, Vector{CostSegment}}}(),
    tech_cost_curves::Dict{Int, Dict{Int, Vector{CostSegment}}} = Dict{Int, Dict{Int, Vector{CostSegment}}}(),
    bat_cost_curves::Dict{Int, Dict{Int, Vector{CostSegment}}} = Dict{Int, Dict{Int, Vector{CostSegment}}}(),
    re_target_override::Dict{Tuple{Int,Int}, Float64} = Dict{Tuple{Int,Int}, Float64}(),
    co2_budget_groups::Vector{CO2BudgetGroup} = CO2BudgetGroup[],
    unit_commitment::Bool = false,
    primary_energy::Union{Nothing, MasterFuelInput} = nothing,
    npv_retired_units::Vector{RetiredUnit} = RetiredUnit[],
    re_share_anchor::Vector{Float64} = Float64[],
    rep_day_offsets::Vector{Int} = Int[],
    gen_outage_factors::Dict{Tuple{Int,Int}, Vector{Float64}} = Dict{Tuple{Int,Int}, Vector{Float64}}(),
    bat_outage_factors::Dict{Tuple{Int,Int}, Vector{Float64}} = Dict{Tuple{Int,Int}, Vector{Float64}}()
)
    return MasterProblemInput(
        years, base_year,
        system_name, network, generators, batteries,
        technologies, battery_technologies,
        base_demand, demand_growth,
        discount_rate, max_annual_investment,
        target_re_penetration, initial_re_penetration,
        min_re_increment, max_re_increment,
        system_node_ranges,
        loss_of_load_penalty, fre_penetration_loss_penalty,
        max_curtailment_ratio, curtailment_cost,
        temporal_resolution_hours, representative_days_per_year, min_day_separation,
        investment_resolution_hours,
        use_tsam, tsam_period_start_hours, tsam_period_weights,
        tsam_chronological_order, tsam_inter_period_linking,
        life_extension_cost_factor, decommissioning_cost_factor,
        sectoral_demand, sectoral_criticality,
        reserve_margin, npv_annual_return_rate,
        base_lcoe,
        hours_per_year,
        reserve_static_default_ratio,
        reserve_static_requirement, reserve_dynamic_requirement,
        dynamic_reserve_contribution,
        loss_of_reserve_static, loss_of_reserve_dynamic,
        inertia_limit, loss_of_inertia_penalty,
        fuel_co2, co2_cost,
        transmission_lines, transmission_reactances, transmission_capacities,
        transmission_resistances, transmission_loss_segments,
        solver_name, threads, time_limit, gap, verbose, solver_options,
        gen_cost_curves, tech_cost_curves, bat_cost_curves,
        re_target_override,
        co2_budget_groups, unit_commitment,
        primary_energy, npv_retired_units, re_share_anchor, rep_day_offsets,
        gen_outage_factors, bat_outage_factors
    )
end

"""
    MasterProblemVariables

Container for Master Problem decision variables.

# Investment Variables (indexed by [year, tech/bat_tech, node])
- `tech_investment`: Technology capacity investment (MW)
- `bat_tech_power_investment`: Battery technology power investment (MW)
- `bat_tech_capacity_investment`: Battery technology energy investment (MWh)
- `transfer_investment`: Transmission line investment (MW)

# Life Extension Variables (indexed by [year, unit, node])
- `gen_life_extension`: MW of a generator kept past its lifetime (continuous, `nothing` within it)
- `bat_life_extension`: MW of discharge power of a battery kept past its lifetime (same)

# RE Tracking (indexed by [(system, year)])
- `re_energy_ratio`: a system's renewable generation in a year over its
  demand (`define_re_share!`; in the Benders master the value it gives the
  representative days), the link from one year to the next year's growth
  cap (`add_re_growth_cap!`). NOT the RE share, which is renewable over
  total generation (`master_re_shares`); no upper bound

# Operational Variables (per representative day)
- `operational_subproblems`: Dict mapping (year, day) to PowerSystem models

# Slack Variables
- `slack_re_target`: renewable energy missing from the RE target (MWh/year)
  per (scenario, system, year), scenario 0 when deterministic; created with
  the representative days (`add_re_target_constraints!`). The renewable
  energy over the growth cap (`add_re_growth_cap!`) is in
  `model.ext[:re_cap_slack]`, keyed the same way.
- `slack_capacity`: Capacity adequacy shortfall (MW) per year per node
- `slack_budget`: Investment over the annual budget per year

# Capacity in service
- `capacity_state`: per year, the capacity built and still in service of each
  candidate unit, corridor, reservoir, converter and fuel build, as a state
  variable defined by one equality on the investment variables
  (`add_capacity_states!`); the hourly rows use it instead of the investment
  sum. Empty in the Benders master, which has no hourly rows.
"""
mutable struct MasterProblemVariables
    # Technology investment variables: Dict{year, Dict{tech_idx, Vector{VariableRef}}} (per bus)
    tech_investment::Dict{Int, Dict{Int, Vector{VariableRef}}}
    bat_tech_power_investment::Dict{Int, Dict{Int, Vector{VariableRef}}}
    bat_tech_capacity_investment::Dict{Int, Dict{Int, Vector{VariableRef}}}
    transfer_investment::Dict{Int, Dict{Tuple{Int,Int}, VariableRef}}

    # Life extension: Dict{year, Dict{unit_idx, Vector{Union{VariableRef, Nothing}}}}
    gen_life_extension::Dict{Int, Dict{Int, Vector{Union{VariableRef, Nothing}}}}
    bat_life_extension::Dict{Int, Dict{Int, Vector{Union{VariableRef, Nothing}}}}

    # RE penetration per (system_idx, year_idx)
    re_energy_ratio::Dict{Tuple{Int,Int}, VariableRef}

    # Operational subproblems: (year_idx, day_idx) => (model, vars)
    operational_subproblems::Dict{Tuple{Int,Int}, Tuple{Model, PowerSystemVariables}}

    # Operational costs per year and day
    operational_costs::Dict{Int, Vector{AffExpr}}

    # Slack variables
    slack_re_target::Dict{Tuple{Int,Int,Int}, VariableRef}  # (scenario, system, year)
    slack_capacity::Dict{Tuple{Int,Int}, VariableRef}  # (year, node) => slack
    slack_budget::Dict{Int, VariableRef}

    # Inter-period SOC linking (TSAM): year_idx => (bat_idx, node_idx, period_boundary) => VariableRef
    # period_boundary = 0..K where 0 = year start, K = after last period
    inter_period_soc::Dict{Int, Dict{Tuple{Int,Int,Int}, VariableRef}}

    # Inter-period reservoir-level linking (TSAM seasonal hydro): year_idx =>
    # (gen_idx, node_idx, period_boundary) => VariableRef. Same chronological
    # chain as inter_period_soc, but tracks reservoir energy (MWh-eq) so water
    # can be carried across representative periods (e.g. filled in spring and
    # drawn down in summer) instead of being cyclic within each period.
    inter_period_reservoir::Dict{Int, Dict{Tuple{Int,Int,Int}, VariableRef}}

    # Reservoir capacity investment: year => gen_idx => Vector{VariableRef} (per node, MWh)
    reservoir_investment::Dict{Int, Dict{Int, Vector{VariableRef}}}

    # Investment period grouping: how many years per investment period
    # (derived from investment_resolution_hours ÷ 8760)
    years_per_inv_period::Int

    # Converter investment: period-start year => ("acdc" | "freq", converter idx) => MW
    converter_investment::Dict{Int, Dict{Tuple{String,Int}, VariableRef}}
    # CO2 budget violation (t): (scenario idx, 0 = deterministic; year; group) => slack
    co2_budget_violation::Dict{Tuple{Int,Int,Int}, VariableRef}
    # M2 (#89): fuel storage/transport and electrolyzer builds (nothing
    # without primary energy)
    fuel::Union{Nothing, MasterFuelVariables}
    # Capacity in service per year: key (see `add_capacity_states!`) =>
    # state variable, or 0 when nothing can be built
    capacity_state::Dict{Tuple, AffExpr}
end

"""
    UnitOperation

What the solved master did with each existing unit in one scenario (one
entry, probability 1, for a deterministic master), read by
`calculate_unit_npv`. Keys are `(kind, unit index, bus, year index)` with
kind `:gen` or `:bat`; values are per modelled year, undiscounted, in the
master's cost unit:
- `energy`: MWh the generator produced, its representative days (or TSAM
  periods) scaled to the year as the master scales their cost;
- `operating_cost`: what the master charges the unit for that operation:
  fuel (`fuel_cost`, its offer curve, or with primary energy on the fuel it
  burns at the fuel's price), maintenance,
  CO2 price, LP-relaxed start-ups; for a battery maintenance on charge and
  discharge and its throughput (or offer-curve) cost;
- `asset_cost`: its share of `build_annual_asset_cost`: fixed O&M on the
  capacity in service, life extension and decommissioning;
- `fuel_cost`: the fuel part of a generator's `operating_cost`, and
  `fuel_growth` its yearly growth factor in the master (the primary-energy
  fuel's `price_growth_rate`, a scenario's `fuel_price_growth`), for the
  years past the modelled ones (`calculate_unit_npv`'s `extra_years`).
`discount_rate` is the scenario's (base rate × its multiplier),
`multipliers` its `ScenarioMultipliers` (`nothing` when deterministic).
"""
struct UnitOperation
    probability::Float64
    discount_rate::Float64
    energy::Dict{Tuple{Symbol,Int,Int,Int}, Float64}
    operating_cost::Dict{Tuple{Symbol,Int,Int,Int}, Float64}
    asset_cost::Dict{Tuple{Symbol,Int,Int,Int}, Float64}
    fuel_cost::Dict{Tuple{Symbol,Int,Int,Int}, Float64}
    fuel_growth::Dict{Tuple{Symbol,Int,Int,Int}, Float64}
    multipliers::Any
end

UnitOperation(probability::Float64, discount_rate::Float64; multipliers = nothing) =
    UnitOperation(probability, discount_rate,
        (Dict{Tuple{Symbol,Int,Int,Int}, Float64}() for _ in 1:5)..., multipliers)

"""
    MasterProblemResult

Solution output from the Master Problem.

# Fields
- `status`: Solver termination status
- `objective`: Total NPV cost (\$)
- `solve_time`: Solve time (seconds)

## Investment Decisions
- `tech_investment`: Technology investments [year, tech, node] (MW)
- `bat_tech_power_investment`: Battery technology power investments [year, bat_tech, node] (MW)
- `bat_tech_capacity_investment`: Battery technology energy investments [year, bat_tech, node] (MWh)
- `transfer_investment`: Transmission investments [year, (i,j)] (MW)

## Life Decisions
- `gen_life_extension`: [year, gen, node] share of the generator in service:
    1.0 within its original `life_time`; past it, the life-extended MW over
    the (degraded) capacity the master could keep (0.0 = retired). Not MW: the
    capacity is in `cumulative_gen_capacity`
- `bat_life_extension`: the same share for batteries [year, bat, node]
    (life-extended MW over the discharge power)

## Summary Metrics
- `total_investment_by_year`: overnight cost of the builds of each year (\$,
    undiscounted): what the annual budget limits
- `total_operational_cost_by_year`: Operating cost per year (\$, undiscounted):
    the representative days' (or TSAM periods') cost scaled to the year,
    CO2-budget violations included; the expected value over the scenarios in
    a stochastic master; the subproblems' cost in Benders. The annual asset
    costs (fixed O&M, life extension, decommissioning) are not included
- `re_penetration_by_year`: Achieved RE share per year: the renewable
    generation of the representative days (or TSAM periods) over their total
    generation (`is_renewable_unit`), both scaled to the year (all systems
    together; the expected value in a stochastic master)
- `re_penetration_by_system`: the same per system
- `cumulative_capacity_by_year`: Cumulative capacity by technology and year

## Units' operation
- `unit_operation`: each existing unit's generation and costs per year and
    scenario (`UnitOperation`), for `calculate_unit_npv`; empty unless the
    result was extracted with `unit_operation = true`

## Investment annuities
- `annuity_cost_by_year`: the investment annuities paid in each year (\$,
    undiscounted): every build in service that year pays
    `capital_recovery_factor` × its overnight cost, what the objective
    charges (base costs and discount rate in a stochastic master)
- `annuity_streams`: `(build year index, life in years, annuity per year)`
    of the builds, summed per build year and life: a stream is paid in the
    `annuity_years` of its build year and life (the myopic runner adds the
    streams of earlier years to each year's reported cost)
"""
struct MasterProblemResult
    # Status
    status::MOI.TerminationStatusCode
    objective::Float64
    solve_time::Float64

    # Technology investment decisions (per-technology, not per-generator)
    tech_investment::Dict{Int, Dict{Int, Vector{Float64}}}           # year => tech_idx => [per-bus MW]
    bat_tech_power_investment::Dict{Int, Dict{Int, Vector{Float64}}} # year => bat_tech_idx => [per-bus MW]
    bat_tech_capacity_investment::Dict{Int, Dict{Int, Vector{Float64}}} # year => bat_tech_idx => [per-bus MWh]
    transfer_investment::Dict{Int, Dict{Tuple{Int,Int}, Float64}}

    # Life decisions (per existing unit)
    gen_life_extension::Dict{Int, Dict{Int, Vector{Float64}}}
    bat_life_extension::Dict{Int, Dict{Int, Vector{Float64}}}

    # Summary
    total_investment_by_year::Vector{Float64}
    total_operational_cost_by_year::Vector{Float64}
    re_penetration_by_year::Vector{Float64}
    re_penetration_by_system::Dict{String, Vector{Float64}}  # system_name => [per-year RE]

    # Cumulative capacities for existing units (year => unit_idx => [per-bus])
    cumulative_gen_capacity::Dict{Int, Dict{Int, Vector{Float64}}}
    cumulative_bat_capacity::Dict{Int, Dict{Int, Vector{Float64}}}
    cumulative_bat_power::Dict{Int, Dict{Int, Vector{Float64}}}

    # Cumulative capacities for technology investments (year => tech_idx => [per-bus])
    cumulative_tech_capacity::Dict{Int, Dict{Int, Vector{Float64}}}
    cumulative_bat_tech_power::Dict{Int, Dict{Int, Vector{Float64}}}
    cumulative_bat_tech_capacity::Dict{Int, Dict{Int, Vector{Float64}}}

    # Reservoir capacity investment: year => gen => nodes (MWh)
    reservoir_investment::Dict{Int, Dict{Int, Vector{Float64}}}

    # Converter investment: year => ("acdc" | "freq", converter idx) => MW built that year
    converter_investment::Dict{Int, Dict{Tuple{String,Int}, Float64}}
    # M2 (#89): fuel infrastructure and electrolyzers built by the master
    # (extract_master_fuel_solution); empty without primary energy
    fuel_investment::Dict{String, Any}
    # Each existing unit's operation and costs in the solved plan, for the
    # NPV (extract_master_solution(...; unit_operation = true)); empty
    # unless asked for
    unit_operation::Vector{UnitOperation}
    # Investment annuities paid per year (undiscounted)
    annuity_cost_by_year::Vector{Float64}
    # The annuity streams behind them: (build year, life in years, annuity
    # per year), summed over the builds of a year with the same life
    annuity_streams::Vector{Tuple{Int, Float64, Float64}}
end

# Without the fuel builds (no primary energy)
MasterProblemResult(args::Vararg{Any, 21}) =
    MasterProblemResult(args..., Dict{String, Any}())
# Without the units' operation
MasterProblemResult(args::Vararg{Any, 22}) =
    MasterProblemResult(args..., UnitOperation[])
# Without the annuities
MasterProblemResult(args::Vararg{Any, 23}) =
    MasterProblemResult(args..., Float64[])
# Without the annuity streams
MasterProblemResult(args::Vararg{Any, 24}) =
    MasterProblemResult(args..., Tuple{Int, Float64, Float64}[])

"""
    BendersResult

Output of the Benders decomposition solver for the master problem.

# Fields
- `solution`: the recovered `MasterProblemResult`
- `objective`: best upper bound found (total NPV cost, \$)
- `iterations`: number of Benders iterations performed
- `gap`: final relative optimality gap (UB - LB) / |UB|
- `lb_history`: lower bound per iteration (master objective)
- `ub_history`: upper bound per iteration (investment + subproblem cost)
- `solve_time`: total wall-clock time (seconds)
"""
struct BendersResult
    solution::MasterProblemResult
    objective::Float64
    iterations::Int
    gap::Float64
    lb_history::Vector{Float64}
    ub_history::Vector{Float64}
    solve_time::Float64
end

# =============================================================================
# Stochastic Programming Types
# =============================================================================

"""
    ScenarioMultipliers

Cost and parameter multipliers for a stochastic scenario (all 1.0 by default).

# Fields
- `invest_cost_renewables::Float64`: Investment cost of renewable technologies
- `invest_cost_conventional::Float64`: Investment cost of non-renewable technologies
- `fuel_cost::Float64`: Fuel cost of generators (incl. PWL fuel curves) and technologies,
  and the fuel prices of the master fuel balance
- `maintenance_cost::Float64`: Maintenance cost of generators, batteries and technologies
- `invest_cost_storage::Float64`: Battery technology investment (power and energy)
- `invest_cost_transmission::Float64`: Transmission investment cost
- `discount_rate::Float64`: Scenario discount rate = base rate × multiplier
- `demand_growth::Float64`: Annual demand factor, demand × m^(year_idx − 1)
- `fuel_price_growth::Float64`: Annual fuel price factor, fuel × m^(year_idx − 1)
- `carbon_price::Float64`: CO2 cost
- `gen_fixed_cost::Float64`: Fixed O&M (\$/MW/year) of generators, technologies,
  batteries and battery technologies
- `gen_decommissioning_cost::Float64`: Decommissioning cost of existing generators
- `bat_decommissioning_cost::Float64`: Decommissioning cost of existing batteries
- `bat_invest_cost_power::Float64`: Battery technology power investment (× `invest_cost_storage`)
- `bat_invest_cost_capacity::Float64`: Battery technology energy investment (× `invest_cost_storage`)
- `penalty_loss_of_load::Float64`: Loss-of-load penalty (sectoral penalties included)
- `penalty_loss_of_reserve::Float64`: Static and dynamic reserve shortfall penalties
- `penalty_curtailment::Float64`: Curtailment cost
- `penalty_fre_penetration::Float64`: RE-penetration shortfall penalty
- `gen_start_up_cost::Float64`: Generator start-up cost (LP start-ups of the master)
- `bat_maintenance_cost::Float64`: Battery maintenance only (× `maintenance_cost`)
- `transmission_cost_per_mw_km::Float64`: Flow cost of lines with a per-MW·km
  cost (inter-system links, the lines' `flow_cost`)
- `converter_invest_cost::Float64`: AC/DC and frequency converter investment
- `converter_variable_cost::Float64`: AC/DC and frequency converter variable cost
- `penalty_co2_budget_violation::Float64`: CO2 budget violation penalty
- `fuel_import_cost`: per-node fuel import cost
- `penalty_loss_of_fuel_supply`: fuel shortfall and excess-supply penalty
- `fuel_storage_invest_cost`: fuel storage investment cost
- `fuel_transport_cost`: fuel route transport cost and route investment cost
- `electrolyzer_invest_cost`, `electrolyzer_fixed_cost`,
  `electrolyzer_variable_cost`, `electrolyzer_water_cost`: electrolyzer costs
"""
Base.@kwdef struct ScenarioMultipliers
    invest_cost_renewables::Float64 = 1.0
    invest_cost_conventional::Float64 = 1.0
    fuel_cost::Float64 = 1.0
    maintenance_cost::Float64 = 1.0
    invest_cost_storage::Float64 = 1.0
    invest_cost_transmission::Float64 = 1.0
    discount_rate::Float64 = 1.0
    demand_growth::Float64 = 1.0
    fuel_price_growth::Float64 = 1.0
    carbon_price::Float64 = 1.0
    gen_fixed_cost::Float64 = 1.0
    gen_decommissioning_cost::Float64 = 1.0
    bat_decommissioning_cost::Float64 = 1.0
    bat_invest_cost_power::Float64 = 1.0
    bat_invest_cost_capacity::Float64 = 1.0
    penalty_loss_of_load::Float64 = 1.0
    penalty_loss_of_reserve::Float64 = 1.0
    penalty_curtailment::Float64 = 1.0
    penalty_fre_penetration::Float64 = 1.0
    # --- Start-ups, battery maintenance, MW·km flows, converters, CO2 budget ---
    gen_start_up_cost::Float64 = 1.0
    bat_maintenance_cost::Float64 = 1.0
    transmission_cost_per_mw_km::Float64 = 1.0
    converter_invest_cost::Float64 = 1.0
    converter_variable_cost::Float64 = 1.0
    penalty_co2_budget_violation::Float64 = 1.0
    # --- end ---
    # ── M2 (#89): fuel chain and electrolyzers (master_fuel.jl) ──
    fuel_import_cost::Float64 = 1.0
    penalty_loss_of_fuel_supply::Float64 = 1.0
    fuel_storage_invest_cost::Float64 = 1.0
    fuel_transport_cost::Float64 = 1.0
    electrolyzer_invest_cost::Float64 = 1.0
    electrolyzer_fixed_cost::Float64 = 1.0
    electrolyzer_variable_cost::Float64 = 1.0
    electrolyzer_water_cost::Float64 = 1.0
    # ── end M2 ──
end

# The original ten positional multipliers; the others stay at 1.0
ScenarioMultipliers(invest_cost_renewables, invest_cost_conventional, fuel_cost,
                    maintenance_cost, invest_cost_storage, invest_cost_transmission,
                    discount_rate, demand_growth, fuel_price_growth, carbon_price) =
    ScenarioMultipliers(; invest_cost_renewables, invest_cost_conventional, fuel_cost,
                        maintenance_cost, invest_cost_storage, invest_cost_transmission,
                        discount_rate, demand_growth, fuel_price_growth, carbon_price)

"""
    Scenario

Stochastic scenario definition.

# Fields
- `name::String`: Scenario identifier
- `probability::Float64`: Probability weight (0-1, sum to 1)
- `multipliers::ScenarioMultipliers`: Cost/parameter multipliers
"""
struct Scenario
    name::String
    probability::Float64
    multipliers::ScenarioMultipliers
end

"""
    StochasticMasterInput

Input for stochastic Master Problem with multiple scenarios.

Extends MasterProblemInput with scenario information.
"""
struct StochasticMasterInput
    base_input::MasterProblemInput
    scenarios::Vector{Scenario}
    use_stochastic::Bool
end

# =============================================================================
# NPV Calculation Types
# =============================================================================

"""
    UnitNPV

NPV calculation result for a single unit.

# Fields
- `unit_type::String`: "generator" or "battery"
- `unit_idx::Int`: Unit index
- `node::Int`: Bus index (the master's location of the unit)
- `system::String`: System name (for multi-system)
- `npv::Float64`: Net present value
- `remaining_lifetime::Float64`: Modelled years the unit is in service in the plan
- `recommend_retirement::Bool`: NPV below the threshold: retire it
"""
struct UnitNPV
    unit_type::String
    unit_idx::Int
    node::Int
    system::String
    npv::Float64
    remaining_lifetime::Float64
    recommend_retirement::Bool
end

"""
    NPVIterationResult

Result from NPV-based iterative retirement analysis.

# Fields
- `iterations::Int`: Number of master solves
- `converged::Bool`: The last solve left no unit below the threshold
- `final_result::MasterProblemResult`: Final solution
- `forced_retirements::Vector{UnitNPV}`: Units forced to retire
- `npv_history::Vector{Dict{String, Float64}}`: per master solve, the NPV of
  every unit evaluated (`"generator_g_bus"`, `"battery_b_bus"`)
"""
struct NPVIterationResult
    iterations::Int
    converged::Bool
    final_result::MasterProblemResult
    forced_retirements::Vector{UnitNPV}
    npv_history::Vector{Dict{String, Float64}}
end

# =============================================================================
# MGA/SPORES Types
# =============================================================================

"""
    MGAResult

Result container for MGA/SPORES (Modeling to Generate Alternatives /
Spatially-explicit Practically Optimal REsultS).

Contains multiple near-optimal alternatives with diverse investment patterns.

# Fields
- `alternatives::Vector{MasterProblemResult}`: All solutions (index 1 = cost-optimal)
- `num_alternatives::Int`: Total number of alternatives (including cost-optimal)
- `slack_fraction::Float64`: Near-optimal slack parameter ε
- `optimal_cost::Float64`: Cost-optimal objective value C*
- `alternative_costs::Vector{Float64}`: Actual cost of each alternative
- `diversity_objectives::Vector{Float64}`: Diversity objective for alternatives 2..K
"""
struct MGAResult
    alternatives::Vector{MasterProblemResult}
    num_alternatives::Int
    slack_fraction::Float64
    optimal_cost::Float64
    alternative_costs::Vector{Float64}
    diversity_objectives::Vector{Float64}
    # Tag for each non-optimal alternative naming the SPORES objective
    # that produced it (e.g. "hsj_diversity", "min_total_build",
    # "max_tech_equity"…). For the classical MGA loop every entry is
    # "hsj_diversity"; for SPORES runs each entry matches one of the
    # SporesObjective values in the Python schema. Length matches
    # ``diversity_objectives``; empty for back-compat with old result
    # files that predate the SPORES roadmap.
    objective_labels::Vector{String}
end

# Convenience constructor for back-compat with sites that don't pass
# labels (e.g., old test fixtures): defaults the labels to all
# "hsj_diversity", which is what every pre-SPORES caller did.
function MGAResult(
    alternatives::Vector{MasterProblemResult},
    num_alternatives::Int,
    slack_fraction::Float64,
    optimal_cost::Float64,
    alternative_costs::Vector{Float64},
    diversity_objectives::Vector{Float64},
)
    labels = fill("hsj_diversity", length(diversity_objectives))
    return MGAResult(alternatives, num_alternatives, slack_fraction,
                     optimal_cost, alternative_costs,
                     diversity_objectives, labels)
end

