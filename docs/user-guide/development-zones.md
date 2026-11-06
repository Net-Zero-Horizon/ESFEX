# Development Zones

## What Are Development Zones

A development zone is a geographic area (a polygon) where candidate technologies can be built away from the existing nodes. Before the model is built, ESFEX turns each zone into a **virtual node and bus**, connected to the existing network by a candidate interconnection whose cost combines a line cost per MW·km and a transformer cost per MW.

A zone captures:

- **Location**: the polygon, reduced to the mean of its vertices.
- **Potential**: a maximum capacity (`max_capacity_mw`) and optional per-technology limits.
- **Interconnection cost**: line + transformer cost per MW, from the zone to the bus it connects to.
- **Technology scope**: which generators, technologies and storage the zone applies to.

Zones work in single-node, multi-node and multi-system runs. When several systems are merged, a zone's technology and unit keys become `<system>__<key>` like the rest of the merged configuration, and its `target_bus` follows the merged bus numbering.

---


## How Zones Work Internally

`expand_config_with_zones()` (in `esfex/zones.py`) runs once at the start of a run, before the master problem:

1. **Virtual node**: a node is added for each zone at the polygon's mean vertex position, named `zone_<name>`. With N original nodes, zone *z* (0-based, in list order) becomes node N + z.
2. **Virtual bus**: a bus `zone_bus_<z>` is added at that node with `demand_fraction: 0.0` (zone nodes have no demand).
3. **Connection bus**: `target_bus` if given; otherwise the existing bus whose parent node is closest to the zone (haversine distance between the zone position and the node coordinates). Without node coordinates, bus 0 is used at distance 0.
4. **Interconnection**: a candidate line `zone_link_<name>` joins the zone bus to the connection bus. It starts at 0 MW, and its length is the zone-to-node distance. The master expands it through transmission investment: the zone node's `transference_invest_cost` is set to the interconnection cost, and its `transference_invest_max` to `max_capacity_mw` (1,000,000 MW when `max_capacity_mw` is null). If the connection node has its own, higher `transference_invest_cost`, that cost applies instead (a corridor costs the higher of its two ends). The capacity the master builds is added to this line in the dispatch. The adjacency matrix also grows from N×N to (N+Z)×(N+Z).
5. **Per-node arrays**: every generator, battery, technology, battery technology and electrolyzer gets one more entry per zone, in every per-node list. Matched units get values copied from the connection node (see [Matching](#matching)); unmatched units get zero investment there; installed capacity (rated power, storage capacity, reservoir data, electrolyzer rating) is 0 in the zone.

In a multi-node run the master works on a node-level network and the dispatch on the bus-level network. The zone is added to both with the same virtual node index, so the interconnection the master builds lands on the zone line of the dispatch network.
6. **Demand and profiles**: demand gets a zero column per zone node, and every cached availability profile gets a column copied from the connection node.


---


## Zone Configuration in YAML

```yaml
systems:
  island:
    development_zones:
      - name: southern_plain
        technology: solar
        polygon:
          - latitude: 22.0
            longitude: -82.5
          - latitude: 22.0
            longitude: -82.0
          - latitude: 21.8
            longitude: -82.0
          - latitude: 21.8
            longitude: -82.5
        max_capacity_mw: 500.0
        line_cost_per_mw_km: 1500.0
        transformer_cost_per_mw: 50000.0
        allowed_technologies:
          tech_solar: 300.0
        exclusive: false
        notes: "Flat land south of the main substation"
```

### Field Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | (required) | Zone name; the virtual node is named `zone_<name>` |
| `technology` | string | (required) | Matching text (see [Matching](#matching)). `battery`, `storage`, `bess` or `ess` make it a storage zone |
| `layer` | `electrical` / `primary_energy` | `electrical` | Stored with the zone; not used by the expansion |
| `polygon` | list of coordinates | (required) | Vertices (`latitude`, `longitude`) |
| `max_capacity_mw` | float or null | null | Investment limit for matched technologies, and the interconnection cap (see [Limits](#investment-limits)) |
| `line_cost_per_mw_km` | float | 1500.0 | Line cost ($/MW/km) |
| `transformer_cost_per_mw` | float | 50000.0 | Transformer cost ($/MW) |
| `target_bus` | int or null | null | 0-based index in the system's bus list to connect to. Ignored if out of range |
| `allowed_generators` | list or null | null | Generator keys to match instead of matching by text |
| `allowed_technologies` | map or null | null | Technology or battery-technology keys → MW limit at this zone. Replaces matching by text |
| `exclusive` | bool | false | Matched technologies can only be built in this zone |
| `notes` | string or null | null | Free text |

### Polygon Coordinates

Each vertex is a WGS84 coordinate (latitude −90 to 90, longitude −180 to 180). The zone position is the **mean of the vertices**, not the area centroid. If you close the ring by repeating the first vertex, that vertex is counted twice in the mean.


---


## Matching

The zone's `technology` text is compared case-insensitively as a substring:

| Unit | Matched when | Override |
|------|--------------|----------|
| `generators` | The text is in the generator's key, its `fuel`, or its `technology` field | `allowed_generators` |
| `technologies` | The text is in the technology's key, `name` or `fuel` | `allowed_technologies` (only listed keys) |
| `batteries` | Storage zone only: every battery | none |
| `battery_technologies` | Storage zone only: every battery technology | `allowed_technologies` (only listed keys) |

Investment in the master problem comes only from `technologies` and `battery_technologies` (see [Master Problem](master-problem.md#investment-decisions)). Matched generators and batteries get a zone entry with zero rated power, but this adds no buildable capacity.

For a matched technology, the zone entry copies `invest_cost` and the other per-node values from the connection node. For a matched battery technology it copies `invest_cost_power`, `invest_cost_energy` and `invest_max_capacity` from the connection node. Unmatched units get 0.


---


## Investment Limits

For a matched technology or battery technology, the investment limit (`invest_max_power`) at the zone node is:

1. its value in `allowed_technologies`, if the key is listed with a value > 0;
2. otherwise `max_capacity_mw`;
3. otherwise (null) 1,000,000 MW.

`max_capacity_mw` is **not** a combined cap over all technologies in the zone. It also sets the zone's interconnection cap (`transference_invest_max`). A battery technology's energy limit (`invest_max_capacity`) is copied from the connection node, not derived from the zone.

### Interconnection Cost

```
interconnection_cost = line_cost_per_mw_km × distance_km + transformer_cost_per_mw
```

`distance_km` is the haversine distance from the zone position to the parent node of the connection bus. Example, 30 km away with the defaults:

```
cost = 1500 × 30 + 50000 = 95,000 $/MW
```

The master charges the larger of this value and the connection node's `transference_invest_cost` per MW of interconnection built.

### Exclusive Zones

With `exclusive: true`, `invest_max_power` of every matched technology is set to 0 at all original nodes, so it can only be built in the zone. For matched battery technologies, both `invest_max_power` and `invest_max_capacity` are zeroed at the original nodes. Generators are not affected.

### Target Bus

`target_bus` connects the zone to a given bus instead of the nearest one. The distance in the cost formula is still measured to that bus's parent node.


---


## Zone Mapping Log

At the start of a run, ESFEX logs one line per zone:

```
Zone 'southern_plain' (solar): virtual node 1, bus 1 → nearest bus 0 (node 0, 25.3 km), interconnection cost $87950/MW, matched gens: ['solar'], matched bats: (none), matched techs: ['tech_solar'], matched bat_techs: (none)
```

Use it to check which units each zone matched and where it connects.


---


## Zone Results

- Zone nodes have indices N to N+Z−1 in per-node result arrays.
- Technology capacity built in a zone appears in the `inv_<technology key>` units at the zone node.
- Transmission investment between the zone node and its connection node is reported with the other transmission investments.

See [Results Export](results-export.md) for the file layout.
