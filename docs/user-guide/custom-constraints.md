# Custom Constraints

## Overview

Custom constraints let you add your own restrictions to the optimization model
without modifying ESFEX source code. There are two complementary ways to do it:

- **Declarative constraints** — a `custom_constraints` list in the system config
  (edited from the GUI or YAML). Each is a linear expression over named decision
  variables: `Σ coefficient · variable  sense  rhs`. No code required.
- **Plugin Julia overlays** — for anything a single linear expression can't
  express, a plugin ships a `.jl` file that registers a new constraint *type*
  with full JuMP access. The core `.jl` files are never touched.

Both apply additively *after* the native formulation, so a model with no custom
constraints behaves exactly as before. Constraints can target the **operational**
model (dispatch / unit commitment / ACOPF) or the **investment** model (capacity
expansion master problem).

---

## Declarative constraints

### From the GUI

**Edit → Custom Constraints…** opens an editor for the current system: add, edit
or remove constraints. Each constraint has a name, a target model
(operational / investment), a sense (`<=`, `>=`, `==`), a right-hand side, and a
table of **terms** (a variable, an index, and a coefficient). Constraints are
saved with the project and travel inside `.esfexp` bundles.

### From YAML

The same data lives under each system's `custom_constraints`:

```yaml
systems:
  - name: MySystem
    # ...
    custom_constraints:
      - name: cap_coal_output
        target: operational        # or: investment
        sense: "<="                # <=, >=, ==
        rhs: 500.0
        terms:
          - variable: gen_output
            index: [Coal, all]      # generator "Coal", summed over all hours
            coefficient: 1.0
```

This caps generator `Coal`'s total output across all hours at 500 MWh.

### Index syntax

Each term's `index` references the variable's axes **by name**:

- A **generator / battery / technology name** for that axis.
- An **integer** (1-based) for a numeric axis such as an hour or year — or a
  **year value** (e.g. `2030`) for investment variables.
- `all` to **sum over** that axis.

Examples:

| Goal | `variable` | `index` |
| --- | --- | --- |
| Generator `GasCC` output in hour 12 | `gen_output` | `[GasCC, 12]` |
| Generator `GasCC` total annual output | `gen_output` | `[GasCC, all]` |
| Battery `Bat1` state of charge, all hours | `bat_soc` | `[Bat1, all]` |
| `SolarPV` investment in 2030, all nodes | `tech_investment` | `[2030, SolarPV, all]` |

An unknown generator/technology/variable name raises a clear error before the
solve starts.

### Supported variables

**Operational** (`target: operational`):

| Variable | Index | Meaning |
| --- | --- | --- |
| `gen_output` | `[generator, hour]` | Generation (summed over the unit's buses) |
| `load_shed` | `[node, hour]` | Unserved load |
| `curtailment` | `[node, hour]` | Spilled renewable energy |
| `bat_charge` / `bat_discharge` / `bat_soc` | `[battery, hour]` | Storage |
| `power_flow` | `[from_node, to_node, hour]` | Transmission flow |

**Investment** (`target: investment`):

| Variable | Index | Meaning |
| --- | --- | --- |
| `tech_investment` | `[year, technology, node]` | New technology capacity |
| `bat_tech_power_investment` | `[year, technology, node]` | New storage power |
| `transfer_investment` | `[year, from_node, to_node]` | New transmission |

### Examples

Require at least 30% of demand to be met by renewables is *not* a single linear
term, but many useful caps are. A few:

```yaml
# Limit a group of coal units to 1,000 MWh combined over the year
- name: coal_fleet_cap
  target: operational
  sense: "<="
  rhs: 1000.0
  terms:
    - {variable: gen_output, index: [Coal_A, all], coefficient: 1.0}
    - {variable: gen_output, index: [Coal_B, all], coefficient: 1.0}

# Force at least 200 MW of new solar across all nodes in 2035
- name: solar_floor_2035
  target: investment
  sense: ">="
  rhs: 200.0
  terms:
    - {variable: tech_investment, index: [2035, SolarPV, all], coefficient: 1.0}
```

---

## Plugin Julia overlays

When a constraint needs logic beyond a single linear expression, a plugin can
register a new constraint *type* in Julia. The plugin's `.jl` overlay is
`include()`-d into the Julia session after `ESFEX` loads and calls
`register_constraint_hook!`:

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

A config entry then activates it (the `type` matches the registered name; the
plugin's `params` are passed through to the hook):

```yaml
custom_constraints:
  - name: cap_unit_1
    type: gen_cap
    params: {generator: 1, limit: 80.0}
```

A complete, copy-to-install example lives in
`examples/plugins/custom_constraint_example/`. Copy it to
`~/.esfex/plugins/custom_constraint_example/` and it is picked up on the next
solve. See [Plugin Management](../gui/plugins.md) for how plugins are discovered
and loaded.

The hook receives `(model, vars, input, spec)`:

- `model` — the JuMP model.
- `vars` — the decision-variable registry (`PowerSystemVariables` for the
  operational model, `MasterProblemVariables` for the investment model).
- `input` — the model input (temporal config, network, etc.).
- `spec` — the constraint dict from the config (its `params` flattened in).

---

## Notes

- Constraints are applied after all native constraints, so they only ever
  *tighten* the feasible region; an over-tight constraint can make the model
  infeasible.
- Reference units/technologies by the names shown in the GUI (the config keys).
- Declarative constraints round-trip through save/load and `.esfexp` bundles.
