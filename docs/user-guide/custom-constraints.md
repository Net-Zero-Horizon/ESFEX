# Custom Constraints

## Overview

Custom constraints add your own restrictions to the optimization model without changing ESFEX source code. There are two complementary ways to write them:

- **Declarative constraints**: a `custom_constraints` list in the system config, edited from the Studio or in YAML. Each one is a linear expression over named decision variables, `Σ coefficient · variable  sense  rhs`. No code is required.
- **Plugin Julia overlays**: for anything a single linear expression cannot express, a plugin ships a `.jl` file that registers a new constraint *type* with full JuMP access. The core `.jl` files are never touched.

Both are added *after* the native formulation, so a model without custom constraints is unchanged. Each constraint targets either the **operational** model (dispatch, unit commitment, AC-OPF) or the **investment** model (the capacity-expansion master problem).

!!! note "Current limitations (0.3.0)"
    - **Single-node systems only.** Custom constraints are applied only when the run consists of one system with one node. For two or more nodes, or several systems, the run merges the systems into one network, and the merged configuration does not carry `custom_constraints`. They are then ignored without a warning.
    - **Investment constraints** (`target: investment`) are applied only by the deterministic monolithic master. With `solver_method: benders`, the run solves the monolithic master instead (with a warning in the log) so that they apply. They are ignored with `stochastic: true` (when scenarios are defined) and by MGA/SPORES.

---

## Declarative constraints

### From the Studio

**Edit → Custom Constraints…** opens an editor for the current system, where you can add, edit or remove constraints. Each constraint has a name, a target model (operational or investment), a sense (`<=`, `>=`, `==`), a right-hand side, and a table of **terms** (a variable, an index and a coefficient). Constraints are saved with the project and travel inside `.esfexp` bundles.

### From YAML

The same data lives under each system's `custom_constraints`:

```yaml
systems:
  MySystem:
    custom_constraints:
      - name: cap_coal_output
        target: operational        # or: investment
        sense: "<="                # <=, >=, ==
        rhs: 500.0
        terms:
          - variable: gen_output
            index: [Coal, all]      # generator key "Coal", summed over all hours of the window
            coefficient: 1.0
```

This caps the output of generator `Coal` at 500 MWh **per operational window** (see [Scope of an operational constraint](#scope-of-an-operational-constraint)).

| Field | Default | Description |
|-------|---------|-------------|
| `name` | (required) | Constraint name, used in error messages and as the JuMP constraint name |
| `type` | `linear` | `linear` for declarative constraints, or a type registered by a plugin |
| `target` | `operational` | `operational` or `investment` |
| `sense` | `<=` | `<=`, `>=` or `==` |
| `rhs` | 0.0 | Right-hand side |
| `terms` | `[]` | List of `{variable, index, coefficient}`; a `linear` constraint needs at least one |
| `params` | `{}` | Parameters for plugin types (ignored by `linear`) |

### Index syntax

Each term's `index` lists one entry per axis of the variable:

- **Unit axis** (generator or battery): the unit's **config key** (not its display name). An integer that is not a key is the unit's 1-based position in the config.
- **Technology axis**: the technology's config key, or its 1-based position.
- **Year axis**: a **year value** of the horizon (for example `2030`). Years are labelled from the year of `date_start` (or `esfex run --start-year`).
- **Bus, node and hour axes**: a 1-based integer.
- `all`: sum over that axis. For **operational** variables, `all` is accepted only on the **hour** axis; on any other axis the model build fails. For investment variables, `all` works on every axis.

An unknown generator, battery, technology or year name raises an error such as `custom constraint 'cap_coal_output': unknown generator 'Coal' for variable 'gen_output'` when the model is built. The same happens for an invalid index (for example `all` on an operational unit axis). The affected model is then not solved: an operational window is logged as `Window <n> failed: …` and the run moves on, so check the log when a constraint seems to have no effect.

### Scope of an operational constraint

An operational constraint is added to **each** operational model the run builds. It is not a single constraint over the year:

- With the default `temporal.use_rolling_horizon: true`, every rolling-horizon window (`rolling_horizon_hours`, default 48, overlap included) is a separate model and gets its own copy of the constraint.
- Hour numbers count from 1 **within the window**, in timesteps of `temporal.resolution_hours`. `[GasCC, 12]` means the 12th timestep of every window, not hour 12 of the year.
- `all` sums over the timesteps of one window. A yearly cap therefore needs `use_rolling_horizon: false`, which makes each year a single window.

Operational constraints do not apply to the master problem's representative days.

| Goal | `variable` | `index` |
| --- | --- | --- |
| Generator `GasCC` output in timestep 12 of each window | `gen_output` | `[GasCC, 12]` |
| Generator `GasCC` total output over each window | `gen_output` | `[GasCC, all]` |
| Battery `Bat1` state of charge summed over each window | `bat_soc` | `[Bat1, all]` |
| `SolarPV` investment in 2030, all buses | `tech_investment` | `[2030, SolarPV, all]` |

### Supported variables

**Operational** (`target: operational`):

| Variable | Index | Meaning |
| --- | --- | --- |
| `gen_output` | `[generator, hour]` | Generation (MW), summed over the unit's buses |
| `bat_charge` / `bat_discharge` / `bat_soc` | `[battery, hour]` | Storage, summed over the unit's buses |
| `load_shed` | `[bus, hour]` | Unserved load at a **bus** |
| `curtailment` | `[node, hour]` | Curtailed renewable energy at a **node** |
| `power_flow` | `[from_bus, to_bus, hour]` | Flow on the line between two **buses** |

Notes on the network variables:

- Bus numbers are 1-based positions in the system's `buses` list (one bus per node when `buses` is empty). A node can have several buses, so a node number and a bus number usually refer to different elements. With `network_reduction.enabled`, the model uses the reduced bus list.
- `power_flow` exists only in the DC power-flow model (`power_flow_mode: dcopf` or `dcopf_ac_verify`). The pair must be given in the direction in which the line is defined. When several lines connect the same two buses, only the first of them is used.

**Investment** (`target: investment`):

| Variable | Index | Meaning |
| --- | --- | --- |
| `tech_investment` | `[year, technology, bus]` | New capacity of a `technologies` entry (MW) |
| `bat_tech_power_investment` | `[year, technology, bus]` | New storage power (MW). Use `all` for the technology axis (see note) |
| `transfer_investment` | `[year, from_bus, to_bus]` | New transmission capacity (MW) |

- Investment variables exist only in the first year of each investment period (`temporal.investment_resolution`). A term that names another year adds nothing.
- In `planning_mode: myopic`, each year is a separate one-year model. A term that names a specific year raises "unknown year" as soon as another year's model is built, and the master (and the run) stops. A term with `all` years applies the constraint to every year's model separately.

!!! note "`bat_tech_power_investment` and technology names"
    The technology axis of `bat_tech_power_investment` is looked up among the generation `technologies`, not `battery_technologies`. A battery-technology key is rejected as unknown, and a generation-technology key selects the battery technology at the same position. Use `all` on this axis.

### Examples

A renewable-share target cannot be written as a single linear term, but many useful caps can:

```yaml
custom_constraints:
  # Limit two coal units to 1,000 MWh combined in each operational window
  - name: coal_fleet_cap
    target: operational
    sense: "<="
    rhs: 1000.0
    terms:
      - {variable: gen_output, index: [Coal_A, all], coefficient: 1.0}
      - {variable: gen_output, index: [Coal_B, all], coefficient: 1.0}

  # Build at least 200 MW of SolarPV across all buses in 2035 (perfect foresight)
  - name: solar_floor_2035
    target: investment
    sense: ">="
    rhs: 200.0
    terms:
      - {variable: tech_investment, index: [2035, SolarPV, all], coefficient: 1.0}
```

---

## Plugin Julia overlays

When a constraint needs logic beyond a single linear expression, a plugin can register a new constraint *type* in Julia. The plugin's `.jl` overlay is `include()`-d into the Julia session after `ESFEX` loads and calls `register_constraint_hook!`:

```julia
# overlay.jl
using JuMP

ESFEX.register_constraint_hook!("gen_cap", function (model, vars, input, spec)
    g = Int(spec["generator"])
    limit = Float64(spec["limit"])
    expr = AffExpr(0.0)
    for bus in vars.buses_of_gen[g], h in 1:input.temporal.hours
        add_to_expression!(expr, 1.0, vars.gen_output[g, bus, h])
    end
    @constraint(model, expr <= limit, base_name = String(get(spec, "name", "gen_cap")))
end)
```

A config entry then activates it. The `type` matches the registered name, and the entry's `params` are passed to the hook:

```yaml
custom_constraints:
  - name: cap_unit_1
    type: gen_cap
    params: {generator: 1, limit: 80.0}
```

A complete, copy-to-install example lives in `examples/plugins/custom_constraint_example/`. Copy it to `~/.esfex/plugins/custom_constraint_example/` and it is picked up on the next solve. See [Plugin Management](../gui/plugins.md) for how plugins are discovered and loaded.

The hook receives `(model, vars, input, spec)`:

- `model`: the JuMP model.
- `vars`: the decision-variable registry (`PowerSystemVariables` for the operational model, `MasterProblemVariables` for the investment model).
- `input`: the model input (temporal config, network, etc.). For operational models, `input.temporal.hours` is the length of the current window.
- `spec`: the constraint dict from the config, with its `params` flattened in. Indices inside `params` are passed as written, so use Julia's 1-based indices.

Plugin types follow the same scope rules as declarative constraints: an operational hook runs once per operational window.

---

## Notes

- Constraints are added after all native constraints, so they only *tighten* the feasible region. An over-tight constraint can make the model infeasible.
- Reference units and technologies by their config keys.
- Declarative constraints round-trip through save/load and `.esfexp` bundles.
