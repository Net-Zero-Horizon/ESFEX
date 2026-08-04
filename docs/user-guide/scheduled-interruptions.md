# Scheduled Interruptions

## Overview

Scheduled interruptions let you take an infrastructure element out of service —
fully or as a partial derate — over explicit time windows that the optimizer
must respect. Typical uses are **planned maintenance**, a **forced outage**, or a
seasonal **fuel-supply disruption**.

Each interruption is one record — an *outage window* — that applies an
**availability multiplier** to a single element over an absolute hour range of
the simulation horizon. `0.0` is a full cut; `0.5` a 50 % derate; `1.0` has no
effect. The whole schedule is compiled into per-element, per-timestep capacity
masks before the solve, so the operational model simply sees reduced capacity in
those hours — the formulation itself is unchanged, and a system with an empty
schedule behaves exactly as before.

!!! note "Different from N-1 contingency analysis"
    Scheduled interruptions are **deterministic** events *you* place on the
    calendar. They are not the automatic [N-1 security](../gui/global-settings.md)
    screening, which tests the *survivability* of losing any single element. Use
    interruptions for known maintenance/outages; use N-1 for reliability against
    unplanned single-element failures.

---

## How it works

- **Absolute hours.** Hour `0` is 00:00 on 1 January of the system's
  `base_year`. A window covers `[start_hour, end_hour)` — start inclusive, end
  exclusive. A one-week maintenance from the start of week 20 is
  `start_hour = 20 * 168 = 3360`, `end_hour = 3360 + 168 = 3528`.
- **Availability multiplier.** During the window the element's capacity is
  scaled by `availability` ∈ [0, 1] (`0` = offline, `1` = unaffected). Outside
  the window the element is fully available.
- **Per element.** Each window targets exactly one element. Overlapping windows
  on the same element combine (the tighter derate wins for that hour).
- **Persisted.** The schedule is saved with the project and travels inside
  `.esfexp` bundles.

---

## From the GUI

### The interruptions calendar

**Edit → Interruptions…** opens the **Service Interruptions** calendar for the
current system — a Gantt-style timeline with one row per element, grouped by
category:

- Generators
- Batteries
- Transmission lines
- Transformers
- AC/DC converters
- Frequency converters

**Drag on an element's row** to create an interruption over that span; **click** an
existing window to edit it. Each window exposes:

| Field | Meaning |
| --- | --- |
| **Start** / **End** | The window's absolute hour range on the horizon |
| **Availability** | Capacity fraction during the window (0 = full cut, 1 = no effect) |
| **Note** | Optional free-text label shown on the calendar |

Use **Add** / **Delete** to manage windows directly.

### From the element tree

Right-click any generator, battery, line, transformer, or converter in the
element tree and choose **Schedule interruption…** to open the calendar already
focused on that element's row.

---

## From YAML

The same data lives under each system's `outage_schedule` as a list of outage
windows:

```yaml
systems:
  - name: MySystem
    # ...
    outage_schedule:
      # Two-week full maintenance outage of generator "Nuclear_1"
      - element_type: generator
        element_id: Nuclear_1
        start_hour: 3360          # start of week 20
        end_hour: 3696            # + 2 weeks (2 * 168)
        availability: 0.0         # fully offline
        label: "Annual refuelling"

      # Summer 50% derate of a transmission line (by line id)
      - element_type: line
        element_id: L_north_tie
        start_hour: 4344          # ~1 July
        end_hour: 5088
        availability: 0.5
        label: "Conductor thermal derate"
```

### Element types and `element_id`

| `element_type` | GUI | `element_id` refers to |
| --- | --- | --- |
| `generator` | ✅ | Generator unit key (its name in the config) |
| `battery` | ✅ | Battery unit key |
| `line` | ✅ | Transmission line `line_id` (or its list index as a string) |
| `transformer` | ✅ | Transformer name (or list index as a string) |
| `acdc_converter` | ✅ | Converter name (or list index as a string) |
| `freq_converter` | ✅ | Frequency-converter name (or list index as a string) |
| `fuel_source` | — | Fuel name (edited via YAML; applies a supply-disruption window) |

Elements marked ✅ are editable in the interruptions calendar; `fuel_source`
outages are configured in YAML.

### Window fields

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `element_type` | ✅ | — | One of the types above |
| `element_id` | ✅ | — | Target element key/id (see table) |
| `start_hour` | ✅ | — | Absolute horizon hour, inclusive (≥ 0) |
| `end_hour` | ✅ | — | Absolute horizon hour, exclusive (> `start_hour`) |
| `availability` | — | `0.0` | Capacity fraction in the window (0 = full cut, 1 = none) |
| `label` | — | `""` | Optional note shown on the calendar |

---

## Examples

```yaml
outage_schedule:
  # Battery offline for a firmware upgrade (3 days)
  - {element_type: battery, element_id: Bat_A, start_hour: 1200, end_hour: 1272,
     availability: 0.0, label: "BMS upgrade"}

  # AC/DC converter running at 70% for a month
  - {element_type: acdc_converter, element_id: Conv_1, start_hour: 2000,
     end_hour: 2744, availability: 0.7, label: "Cooling maintenance"}

  # Winter gas-supply disruption (config-only)
  - {element_type: fuel_source, element_id: NaturalGas, start_hour: 0,
     end_hour: 720, availability: 0.3, label: "Pipeline curtailment"}
```

---

## Notes

- An empty `outage_schedule` leaves the model unchanged.
- `availability = 0.0` removes the element for the window; a value in `(0, 1)`
  derates it. The reduced capacity is a hard cap the dispatch cannot exceed.
- Reference elements by the names/ids shown in the GUI element tree (the config
  keys). An unknown `element_id` is simply ignored (no mask applied), so
  double-check names when a scheduled outage seems to have no effect.
- Windows round-trip through save/load and `.esfexp` bundles.
- For usage-driven maintenance (every *X* operating hours) you currently place
  windows manually; automatic maintenance scheduling is not yet available.
```
