"""Paper variants: their strategy grid and their frozen rule version."""

from __future__ import annotations

import hashlib
import json

from flowlevels.config import AppConfig, PaperVariant
from flowlevels.strategies import method5 as m5


def grid_of(v: PaperVariant) -> m5.Grid:
    return m5.Grid(v.trigger, v.k, v.exit2, v.stop)


def key_of(v: PaperVariant) -> str:
    return grid_of(v).key


def rule_inputs(cfg: AppConfig, v: PaperVariant) -> dict[str, object]:
    """Everything that can change a variant's trades (and nothing operational)."""
    syms = cfg.iteration5.symbols
    return {
        "variant": v.model_dump(mode="json", exclude={"id"}),
        "iteration5": cfg.iteration5.model_dump(mode="json"),
        "levels": cfg.levels.model_dump(mode="json"),
        "symbols": {s: cfg.symbol(s).model_dump(mode="json") for s in syms},
        "costs": cfg.costs.model_dump(mode="json"),
    }


def rules_hash(cfg: AppConfig, v: PaperVariant) -> str:
    canon = json.dumps(rule_inputs(cfg, v), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:12]


def variant_grids(cfg: AppConfig) -> list[m5.Grid]:
    return [grid_of(v) for v in cfg.paper.variants]
