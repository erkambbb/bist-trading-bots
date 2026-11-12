"""YAML konfigürasyon yükleme.

`config/<ad>.example.yaml` varsayılanları okunur; varsa `config/<ad>.local.yaml`
(gitignore'da) üzerine yazılır; en son çağıran koddan gelen `overrides` uygulanır.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"


def deep_merge(base: dict, extra: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(name: str, overrides: dict | None = None, config_dir: Path | None = None) -> dict:
    d = config_dir or CONFIG_DIR
    cfg = load_yaml(d / f"{name}.example.yaml")
    local = d / f"{name}.local.yaml"
    if local.exists():
        cfg = deep_merge(cfg, load_yaml(local))
    return deep_merge(cfg, overrides)


def get(cfg: dict, path: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_path(cfg: dict, path: str, value: Any) -> dict:
    """`a.b.c` yoluna değer yazar (yeni kopya döner)."""
    out = copy.deepcopy(cfg)
    cur = out
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value
    return out
