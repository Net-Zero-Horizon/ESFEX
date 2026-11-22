# Overview
Multi-step analysis tools in the **Workflows** menu. Each one guides you through a study with configurable parameters, progress reporting and, where applicable, map-based visualization.

The Solar PV, Wind, Rooftop Solar and EV & V2G wizards each have four steps. A step stacks several panels (for example domain and configuration) that you work through from top to bottom. The Grid Builder has three steps, the Financial Analysis wizard eight, and OTEC Studio is a tabbed window organized around scenarios rather than steps.

Resource computations are powered by standalone Python libraries that can also be used independently for scripting and batch analysis:

| Library | Scope | PyPI |
|---------|-------|------|
| [**solarex**](https://pypi.org/project/solarex/) | Ground-mounted solar PV resource assessment and MCDA | `pip install solarex` |
| [**windrex**](https://pypi.org/project/windrex/) | Wind resource assessment and wake modeling | `pip install windrex` |
| [**rooftex**](https://pypi.org/project/rooftex/) | Rooftop solar potential and building footprint analysis | `pip install rooftex` |
| [**evrex**](https://pypi.org/project/evrex/) | EV fleet adoption, charging demand, and V2G analysis | `pip install evrex` |
| [**otex**](https://pypi.org/project/otex/) | Ocean thermal energy conversion (OTEC) assessment | `pip install otex` |


---


## Workflow Catalog

| Workflow (menu entry) | Steps | Description | Library |
|----------|-------|-------------|---------|
| [Grid Builder](grid-auto-mapping.md) (**Grid Builder...**) | 3 | Power grid construction from open geospatial databases (OpenStreetMap, WRI, GEM, GridFinder) with node placement by clustering, network simplification and per-node demand forecast | — |
| [Rooftop Solar](rooftop-solar.md) (**Solar Rooftop Analysis...**) | 4 | Distributed rooftop PV potential from building footprints, adoption curve modeling (logistic, techno-economic, Bass, agent-based), and scenario-based grid integration | rooftex |
| [OTEC Studio](otec.md) (**OTEC Studio**) | 7 tabs | Ocean Thermal Energy Conversion site assessment with thermodynamic cycle modeling, design optimization, off-design operation, economics, uncertainty and sensitivity analysis, and regional assessment | otex |
| [Wind Assessment](wind.md) (**Wind Resource Assessment...**) | 4 | Wind resource assessment with Weibull fitting, MCDA site ranking, financial analysis, Jensen/Park wake modeling, and hourly availability profile generation | windrex |
| [Solar PV Assessment](solar-pv.md) (**Solar PV Assessment...**) | 4 | Ground-mounted PV potential via Multi-Criteria Decision Analysis (MCDA), financial analysis, GHI characterization, array layout analysis, and hourly availability profile generation | solarex |
| [EV & V2G Assessment](ev-v2g.md) (**EV & V2G Assessment...**) | 4 | Electric vehicle fleet adoption modeling, charging demand scenario generation, vehicle-to-grid potential assessment, battery degradation analysis, and grid impact evaluation | evrex |
| [Financial Analysis](financial-analysis.md) (**Financial Analysis...**) | 8 (4+4) | Post-optimization financial assessment: NPV decomposition, IRR/MIRR, LCOE/VALCOE, pro-forma cash flows, sensitivity analysis, and Monte Carlo risk simulation | — |

[Demand Forecast & Bus Distribution](demand-distribution.md) has no menu entry of its own: it is Step 3 of the Grid Builder and is also opened for a single node by the **Forecast** button of the node attributes panel.


---


## Study area selector

The area-based workflows (Grid Builder, Solar PV, Wind, Rooftop Solar, EV & V2G) define their study area with the same two-column control:

- **Draw domain**: click **Draw domain**, then click on the map to place vertices and click the first vertex to close the polygon. Press Esc to cancel. The wizard window minimizes while you draw and comes back when the polygon is closed.
- **Use imported area as domain**: select an imported GeoAsset (Shapefile, GeoJSON or KML) and click **Apply as domain** to use its boundary. Multi-part boundaries, such as an archipelago, are kept.

The most recent action replaces the previous domain. The status line reads `Polygon: n vertices · ~A km²` (or `GeoAsset 'name': ...`), where A is the area of the domain's bounding box, so it overstates the area of irregular shapes. Data downloads use the bounding box; how the polygon itself is used (for example to clip analysis cells or buildings) is described on each workflow page.
