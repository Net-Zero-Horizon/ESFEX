"""One term per concept across the Studio (Spanish glossary).

- Curtailment and spillage (energy that could have been produced or stored
  and was wasted): "vertimiento".
- Load shedding / loss of load as a quantity or cost: "deslastre de carga".
  The named reliability indices keep their standard names (LOLP, LOLH, LOLF:
  "pérdida de carga"; VOLL: "valor de la carga perdida"), and so does the ENS
  index ("energía no suministrada").
- $B is "miles de M$", never "billón" (10^12 in Spanish).
- Converters are "AC/DC", not "CA/CC".
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_TDIR = Path(__file__).resolve().parent.parent / "src/esfex/visualization/translations"

# Standard reliability-index names that keep "pérdida de carga".
_INDEX = re.compile(r"loss of load (probability|hours|frequency)|value of lost load", re.I)


def _flat(lang: str) -> dict[str, str]:
    out: dict[str, str] = {}

    def walk(node, prefix=""):
        for k, v in node.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                walk(v, key)
            else:
                out[key] = str(v)
    walk(json.loads((_TDIR / f"{lang}.json").read_text(encoding="utf-8")))
    return out


EN, ES = _flat("en"), _flat("es")


def _bad(pattern: str, must: str, forbid: str = r"(?!x)x", skip=None) -> list[str]:
    out = []
    for key, en in EN.items():
        if not re.search(pattern, en, re.I) or key not in ES:
            continue
        if skip is not None and skip.search(en):
            continue
        es = ES[key]
        if not re.search(must, es, re.I) or re.search(forbid, es, re.I):
            out.append(f"{key}: {en!r} -> {es!r}")
    return out


def test_curtailment_and_spillage_are_vertimiento():
    assert not _bad(r"curtail|spill", r"vertimiento",
                    r"curtailment|derrame|vertido|recorte")


def test_load_shedding_is_deslastre_de_carga():
    assert not _bad(r"load[ -]?shed|\bshed\b|loss of load", r"deslastre de carga",
                    r"pérdida de carga|desconexi", skip=_INDEX)


def test_reliability_indices_keep_their_standard_names():
    assert not _bad(r"loss of load (probability|hours|frequency)", r"pérdida de carga")


def test_billion_is_never_billon():
    assert not [k for k, v in ES.items() if re.search(r"bill[oó]n", v, re.I)]
    assert not _bad(r"\$B\b", r"miles de M\$")


def test_converters_are_ac_dc():
    assert not [k for k, v in ES.items() if "CA/CC" in v]
