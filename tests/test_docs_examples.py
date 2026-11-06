"""Keep the YAML examples in the documentation loadable.

Every fenced ``yaml`` block in ``docs/`` and ``README.md`` that looks like an
ESFEX configuration is checked against what the loader actually consumes:

* every key must be one the loader reads — the pydantic models ignore unknown
  keys and the unit converters in ``config/loader.py`` pick fields from an
  explicit list, so a misspelled or obsolete key is otherwise dropped in
  silence;
* a block with top-level ``meta_network:`` and inline ``systems:`` is a
  complete configuration and must also pass
  :func:`esfex.config.loader.load_config`.

A block whose top-level keys are not configuration keys at all (mkdocs, CI,
plugin manifests, ...) is ignored. To exempt a block on purpose — e.g. one that
shows a mistake — put ``# docs-test: skip`` on its first line.
"""

from __future__ import annotations

import ast
import inspect
import re
import types
import typing
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from esfex.config import loader, schema

ROOT = Path(__file__).resolve().parent.parent
SKIP_MARK = "docs-test: skip"

# Pages whose examples predate the current schema. They are being rewritten;
# remove a page from this set once its examples pass (strict xfail enforces it).
KNOWN_STALE: set[str] = set()


# ── What the loader reads ───────────────────────────────────────────────


def _keys_read(func, names: set[str]) -> set[str]:
    """String keys *func* reads from the dicts bound to *names*.

    Covers ``d.get("k", ...)``, ``d["k"]`` and string items of list/tuple
    literals, which is every explicit access pattern the converters use.
    """
    tree = ast.parse(inspect.getsource(func))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
                and node.func.value.id in names and node.args
                and isinstance(node.args[0], ast.Constant)):
            keys.add(node.args[0].value)
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id in names and isinstance(node.slice, ast.Constant)):
            keys.add(node.slice.value)
        elif isinstance(node, (ast.List, ast.Tuple)):
            keys.update(e.value for e in node.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return {k for k in keys if isinstance(k, str)}


def _field_names(model: type[BaseModel]) -> set[str]:
    out: set[str] = set()
    for name, f in model.model_fields.items():
        out.add(name)
        if f.alias:
            out.add(f.alias)
    return out


# Units also take any schema field by name or alias (loader._pass_through).
GENERATOR_KEYS = (_keys_read(loader._convert_generator, {"data"})
                  | _field_names(schema.GeneratorConfig))
# `type` is read by _convert_system to route `bat_*` units to the battery converter.
BATTERY_KEYS = (_keys_read(loader._convert_battery, {"data"})
                | _field_names(schema.BatteryConfig) | {"type"})
FUEL_KEYS = _keys_read(loader._convert_fuels, {"data"})
PE_SOURCE_KEYS = (_keys_read(loader._convert_primary_energy_source, {"data"})
                  | _field_names(schema.PrimaryEnergySourceConfig))
DC_FLOW_KEYS = _keys_read(loader._convert_dc_power_flow, {"nested"})
PENALTY_KEYS = _keys_read(loader._convert_system, {"pen_data"})  # compared lower-case


def _model_of(annotation) -> tuple[str, type[BaseModel]] | None:
    """('one'|'list'|'dict', Model) if *annotation* holds a pydantic model."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        for arg in typing.get_args(annotation):
            if arg is not type(None) and (hit := _model_of(arg)):
                return hit
        return None
    if origin in (list, tuple):
        args = typing.get_args(annotation)
        inner = _model_of(args[0]) if args else None
        return ("list", inner[1]) if inner and inner[0] == "one" else None
    if origin is dict:
        args = typing.get_args(annotation)
        inner = _model_of(args[1]) if len(args) == 2 else None
        return ("dict", inner[1]) if inner and inner[0] == "one" else None
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return ("one", annotation)
    return None


def _check_flat(d, allowed: set[str], path: str, errors: list[str], lower=False):
    if not isinstance(d, dict):
        return
    for k in d:
        key = str(k).lower() if lower else k
        if key not in allowed:
            errors.append(f"{path}.{k}")


def _check_units(d, allowed: set[str], path: str, errors: list[str]):
    if isinstance(d, dict):
        for name, unit in d.items():
            _check_flat(unit, allowed, f"{path}.{name}", errors)


def _check_model(d, model: type[BaseModel], path: str, errors: list[str]):
    if not isinstance(d, dict):
        return
    allowed = _field_names(model)
    fields = {}
    for name, f in model.model_fields.items():
        fields[name] = f
        if f.alias:
            fields[f.alias] = f
    for k, v in d.items():
        if k not in allowed:
            errors.append(f"{path}.{k}")
            continue
        hit = _model_of(fields[k].annotation)
        if hit is None:
            continue
        kind, sub = hit
        if kind == "one":
            _check_model(v, sub, f"{path}.{k}", errors)
        elif kind == "list" and isinstance(v, list):
            for i, item in enumerate(v):
                _check_model(item, sub, f"{path}.{k}[{i}]", errors)
        elif kind == "list":
            errors.append(f"{path}.{k} (must be a list)")
        elif kind == "dict" and isinstance(v, dict):
            for name, item in v.items():
                _check_model(item, sub, f"{path}.{k}.{name}", errors)


# Keys of a system mapping the loader converts by hand before SystemConfig.
SYSTEM_SPECIAL = {"generators", "batteries", "fuels", "primary_energy_sources",
                  "penalties", "dc_power_flow"}
SYSTEM_KEYS = _field_names(schema.SystemConfig) | SYSTEM_SPECIAL
ROOT_KEYS = _field_names(schema.ESFEXConfig)


def _check_system(d, path: str, errors: list[str]):
    if not isinstance(d, dict):
        return
    for k, v in d.items():
        p = f"{path}.{k}"
        if k.startswith("unit_") or k.startswith("bat_"):
            is_bat = isinstance(v, dict) and v.get("type") == "Storage"
            _check_flat(v, BATTERY_KEYS if is_bat else GENERATOR_KEYS, p, errors)
        elif k == "generators":
            _check_units(v, GENERATOR_KEYS, p, errors)
        elif k == "batteries":
            _check_units(v, BATTERY_KEYS, p, errors)
        elif k == "fuels":
            _check_units(v, FUEL_KEYS, p, errors)
        elif k == "primary_energy_sources":
            _check_units(v, PE_SOURCE_KEYS, p, errors)
        elif k == "penalties":
            _check_flat(v, PENALTY_KEYS, p, errors, lower=True)
        elif k == "dc_power_flow":
            _check_flat(v, DC_FLOW_KEYS, p, errors)
        else:
            _check_model({k: v}, schema.SystemConfig, path, errors)


def _check_root(d, errors: list[str]):
    for k, v in d.items():
        if k == "systems" and isinstance(v, dict):
            for name, sysd in v.items():
                _check_system(sysd, f"systems.{name}", errors)
        else:
            _check_model({k: v}, schema.ESFEXConfig, "", errors)


def unknown_keys(block: dict) -> list[str] | None:
    """Keys the loader would drop, or None if *block* is not a config block."""
    top = set(block)
    errors: list[str] = []
    if top & ROOT_KEYS and len(top & ROOT_KEYS) >= len(top & SYSTEM_KEYS):
        _check_root(block, errors)
    elif top & SYSTEM_KEYS or any(str(k).startswith(("unit_", "bat_")) for k in top):
        _check_system(block, "<system>", errors)
    else:
        return None
    return [e.lstrip(".") for e in errors]


# ── Collect the blocks ──────────────────────────────────────────────────

FENCE = re.compile(r"^(?P<indent>[ \t]*)```ya?ml[^\n]*\n(?P<body>.*?)^(?P=indent)```",
                   re.MULTILINE | re.DOTALL)


def _doc_files() -> list[Path]:
    return sorted(ROOT.glob("docs/**/*.md")) + [ROOT / "README.md"]


def _blocks(path: Path):
    text = path.read_text(encoding="utf-8")
    for m in FENCE.finditer(text):
        indent = len(m.group("indent"))
        body = "\n".join(line[indent:] for line in m.group("body").splitlines())
        line = text.count("\n", 0, m.start()) + 1
        yield line, body


def _check_page(path: Path, tmp_path: Path) -> list[str]:
    problems: list[str] = []
    for line, body in _blocks(path):
        if SKIP_MARK in body.splitlines()[0] if body.strip() else True:
            continue
        try:
            block = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            # Only a config block has to parse; CI/mkdocs snippets may template.
            top = set(re.findall(r"^([A-Za-z_]\w*):", body, re.MULTILINE))
            if (top - {"name"}) & (ROOT_KEYS | SYSTEM_KEYS):
                problems.append(f"line {line}: invalid YAML ({exc.__class__.__name__})")
            continue
        if not isinstance(block, dict):
            continue
        bad = unknown_keys(block)
        if bad is None:
            continue
        if bad:
            problems.append(f"line {line}: keys the loader ignores: {', '.join(bad)}")
        systems = block.get("systems")
        if ("meta_network" in block and isinstance(systems, dict)
                and all(isinstance(s, dict) for s in systems.values())):
            cfg = tmp_path / f"block_{line}.yaml"
            cfg.write_text(body, encoding="utf-8")
            try:
                loader.load_config(cfg)
            except Exception as exc:  # noqa: BLE001 — report any load failure
                problems.append(f"line {line}: load_config failed: {exc}")
    return problems


PAGES = [p for p in _doc_files() if any(True for _ in _blocks(p))]


@pytest.mark.parametrize(
    "page",
    [pytest.param(p, id=str(p.relative_to(ROOT)),
                  marks=pytest.mark.xfail(strict=True, reason="examples not yet updated")
                  if str(p.relative_to(ROOT)) in KNOWN_STALE else ())
     for p in PAGES],
)
def test_doc_yaml_examples(page: Path, tmp_path: Path):
    problems = _check_page(page, tmp_path)
    assert not problems, f"{page.relative_to(ROOT)}:\n  " + "\n  ".join(problems)


def test_checker_flags_unknown_keys():
    """The checker itself must catch the typical drift."""
    assert unknown_keys({"master_problem": {"representative_days_per_year": 5}}) == [
        "master_problem.representative_days_per_year"]
    assert unknown_keys({"generators": {"g": {"min_up_time": 1}}}) == [
        "<system>.generators.g.min_up_time"]
    assert unknown_keys({"master_problem": {"representative_days": 5}}) == []
    assert unknown_keys({"systems": {"s": {"stochastic_scenarios": [
        {"name": "a", "probability": 1.0, "multipliers": {"demand_growth_multiplier": 1.5}}]}}}) == [
        "systems.s.stochastic_scenarios[0].multipliers.demand_growth_multiplier"]
    assert unknown_keys({"site_name": "x", "nav": []}) is None
