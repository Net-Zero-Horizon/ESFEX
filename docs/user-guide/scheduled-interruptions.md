# Scheduled Interruptions

## Overview

Scheduled interruptions take an infrastructure element out of service, fully or as a partial derate, over explicit time windows that the dispatch must respect. Typical uses are **planned maintenance**, a **known forced outage**, or a **fuel-supply disruption**.

Each interruption is one record, an *outage window*, that applies an **availability multiplier** to one element over an hour range. `0.0` is a full cut, `0.5` a 50 % derate, and `1.0` has no effect. Before each operational window is solved, the schedule is compiled into per-element, per-timestep capacity factors. The formulation itself does not change, and a system with an empty schedule behaves exactly as before.

!!! note "Different from N-1 contingency analysis"
    Scheduled interruptions are **deterministic** events that *you* place on the calendar. They are not the automatic [N-1 security](../gui/global-settings.md) screening, which tests whether the system survives the loss of any single element. Use interruptions for known maintenance and outages; use N-1 for reliability against unplanned failures.

!!! note "Generators and batteries: unit key and node"
    A generator or battery unit can have capacity at several nodes. A window with `element_id: <unit key>` cuts the unit at every node; add `node: <n>` to cut only its capacity at node *n*. The calendar works per node (one entry per unit and node) and saves windows as unit key plus `node`. Projects saved by earlier versions, which stored the Studio instance id (`<unit key>_n<node>`, for example `diesel_n1`), are read as that unit and node. Battery windows always apply to the whole battery; a warning is logged when a single-node window targets a battery with capacity at several nodes.

---

## How it works

- **Hours within the year.** `start_hour` and `end_hour` count from the first hour of a simulated year (hour 0). A window covers `[start_hour, end_hour)`: start inclusive, end exclusive. A one-week maintenance from the start of week 20 is `start_hour = 20 * 168 = 3360`, `end_hour = 3360 + 168 = 3528`.
- **Every year.** The same window applies in **every** simulated year. A `start_hour` of 8760 or more never takes effect.
- **Availability multiplier.** During the window the element's capacity is multiplied by `availability` ∈ [0, 1]. Outside the window the element is fully available.
- **Per element.** Each window targets exactly one element.
- **Overlaps multiply.** Overlapping windows on the same element compound: two windows of 0.5 in the same hour give 0.25.
- **Coarse time steps.** With `temporal.resolution_hours > 1`, a timestep that is only partly inside a window gets `(1 - f) + f × availability`, where `f` is the covered fraction.
- **Operational dispatch only.** The master problem's representative days do not see the interruptions.
- **Persisted.** The schedule is saved with the project and travels inside `.esfexp` bundles.

---

## From the Studio

### The interruptions calendar

**Edit → Interruptions…** opens the **Service Interruptions** calendar for the current system. It is a Gantt-style timeline over one year (8,760 hours), with one row per element, grouped by category:

- Generators
- Batteries
- Transmission lines
- Transformers
- AC/DC converters
- Frequency converters

**Drag on an element's row** to create an interruption over that span; **click** an existing window to edit it. Each window has:

| Field | Meaning |
| --- | --- |
| **Start** / **End** | The window's hour range within the year |
| **Availability** | Capacity fraction during the window (0 = full cut, 1 = no effect) |
| **Note** | Optional free-text label shown on the calendar |

Use **Add** / **Delete** to manage windows directly. Windows with `end <= start` are dropped when you apply.

### From the element tree

Right-click a generator, battery, line, transformer or converter in the element tree and choose **Schedule interruption…** to open the calendar on that element's row.

---

## From YAML

The schedule lives under each system's `outage_schedule`, as a list of outage windows:

```yaml
systems:
  MySystem:
    outage_schedule:
      # Two-week full maintenance outage of generator "nuclear_1" (unit key)
      - element_type: generator
        element_id: nuclear_1
        start_hour: 3360          # start of week 20
        end_hour: 3696            # + 2 weeks (2 * 168)
        availability: 0.0         # fully offline
        label: "Annual refuelling"

      # Summer 50 % derate of a transmission line (by line_id)
      - element_type: line
        element_id: L_north_tie
        start_hour: 4344          # ~1 July
        end_hour: 5088
        availability: 0.5
        label: "Conductor thermal derate"
```

### Element types and `element_id`

| `element_type` | In calendar | `element_id` | Effect |
| --- | --- | --- | --- |
| `generator` | yes | Generator unit key (optionally with `node`) | Multiplies the generator's availability at the window's node, or at every node, which caps its output. Works for dispatchable units too |
| `battery` | yes | Battery unit key | Scales the charge and discharge power limits of the whole battery |
| `line` | yes | `line_id` of the transmission line | Scales the line's flow limit in both directions |
| `transformer` | yes | Transformer name | Scales the transformer's flow limit |
| `acdc_converter` | yes | Converter name | Scales the rated power |
| `freq_converter` | yes | Converter name | Scales the rated power |
| `fuel_source` | no | Fuel name | Primary-energy supply disruption (see below) |
| `electrolyzer` | no | Electrolyzer key | Accepted, but currently has no effect |
| `fuel_storage` | no | — | Accepted, but currently has no effect |
| `fuel_route` | no | — | Accepted, but currently has no effect |

!!! note "`fuel_source` windows"
    A `fuel_source` window is passed to the primary-energy model's built-in supply-disruption window, so it only matters when primary-energy modelling is active (`enable_primary_energy` and `primary_energy_sources`). The primary-energy model supports **one** window per fuel: only the first is used, and a warning is logged if more are given. That disruption window is measured in hours from the start of each **operational window**, not from the start of the year. With the default rolling horizon it therefore does not fall on the hours you entered. It only lines up with hours of the year when `temporal.use_rolling_horizon: false`.

### Window fields

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `element_type` | yes | — | One of the types above |
| `element_id` | yes | — | Target element key or id (see table) |
| `node` | no | none | Generators and batteries: node of the unit to cut (none = every node) |
| `start_hour` | yes | — | Hour within the year, inclusive (≥ 0) |
| `end_hour` | yes | — | Hour within the year, exclusive (> `start_hour`) |
| `availability` | no | `0.0` | Capacity fraction in the window (0 = full cut, 1 = none) |
| `label` | no | `""` | Optional note shown on the calendar |

---

## Examples

```yaml
outage_schedule:
  # Battery offline for a firmware upgrade (3 days)
  - {element_type: battery, element_id: bat_a, start_hour: 1200, end_hour: 1272,
     availability: 0.0, label: "BMS upgrade"}

  # AC/DC converter running at 70 % for a month
  - {element_type: acdc_converter, element_id: Conv_1, start_hour: 2000,
     end_hour: 2744, availability: 0.7, label: "Cooling maintenance"}

  # Two overlapping 50 % derates on one line: 25 % available in hours 100-123
  - {element_type: line, element_id: L_north_tie, start_hour: 0, end_hour: 124,
     availability: 0.5}
  - {element_type: line, element_id: L_north_tie, start_hour: 100, end_hour: 200,
     availability: 0.5}
```

---

## Notes

- An empty `outage_schedule` leaves the model unchanged.
- `availability = 0.0` removes the element for the window, and a value in `(0, 1)` derates it. The reduced capacity is a hard cap that dispatch cannot exceed.
- Reference elements by their config keys, `line_id` or names. An unknown generator or battery `element_id` is reported with a warning at load time and ignored; for other element types an unknown id is ignored without a warning, so check names when a scheduled outage seems to have no effect.
- Windows round-trip through save/load and `.esfexp` bundles.
- Maintenance driven by operating hours (every *X* hours of operation) is not modelled; place the windows manually.
