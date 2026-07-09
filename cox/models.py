"""Resolve a pinned model for every agent invocation (T-03, salvage: relay
py/relay_models.py).

The token-burn fix. Workers must NOT take the harness's implicit expensive
default: an unpinned `claude -p` falls onto the 1M-context path that gates
behind usage credits (this is what killed relay's smartocrprocess-12 worker)
and burns the Opus-tier rate. This resolves a concrete model + effort for
every spawned worker so the claude lane is launched with --model pinned.

Resolution order (first wins):
  1. env override    (COX_MODEL_IMPL / COX_MODEL_REVIEW as "<model>[:<effort>]")
  2. repo policy      (<project>/.cox/repo.yml -> models: {implementer, reviewer})
  3. global policy    (COX_MODELS_FILE, else ~/.config/cox/models.yml)
  4. built-in defaults (implementer = sonnet/medium, reviewer = opus/medium)

Model routing is an OPERATOR property, not per-codebase — so the default lives
globally. Per-repo override stays supported as the occasional exception.

Unlike relay, a config file that EXISTS but cannot be parsed (bad YAML, or
PyYAML missing while a real config is present) is a hard error, not a silent
fallback: silent degradation is how relay shipped Opus when it meant Sonnet.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .model import ModelSpec

# Role -> built-in default. Aliases (sonnet/opus) resolve to the latest model
# the CLI knows, so defaults rarely go stale across launches, and they stay off
# the credit-gated 1M-context path.
_ROLE_DEFAULT: dict[str, ModelSpec] = {
    "implementer": ModelSpec(provider="anthropic", model="claude-sonnet-4-6", effort="medium"),
    "reviewer": ModelSpec(provider="anthropic", model="opus", effort="medium"),
}

# Per-lane implementer default: the model family follows the lane. claude → a
# cost-effective Sonnet, codex → gpt-5.4. Reviewer stays Opus for every lane
# (judgment is where the strong model earns its keep); route genuinely hard
# implement tasks to Opus explicitly via `cox dispatch --model opus:high`.
_LANE_IMPL_DEFAULT: dict[str, ModelSpec] = {
    "claude": ModelSpec(provider="anthropic", model="claude-sonnet-4-6", effort="medium"),
    "codex": ModelSpec(provider="openai", model="gpt-5.4", effort="medium"),
    "stub": ModelSpec(provider="anthropic", model="sonnet", effort="medium"),
}

_ENV_KEY = {"implementer": "COX_MODEL_IMPL", "reviewer": "COX_MODEL_REVIEW"}

# UI catalog: the models the dispatch picker offers per lane, with the effort
# levels each is worth running at. This is an OPERATOR list for the dropdown —
# edit it here as models ship/retire; a repo/global policy can still pin anything
# outside it. `default: True` marks the lane's pre-selected model. (Codex model
# ids follow the existing gpt-5.4 pattern — adjust if the CLI names differ.)
CATALOG: dict[str, list[dict[str, Any]]] = {
    "claude": [
        {"model": "claude-opus-4-8", "label": "opus 4.8", "efforts": ["medium", "high"]},
        {"model": "claude-sonnet-5", "label": "sonnet 5", "efforts": ["low", "medium", "high"]},
        {"model": "claude-sonnet-4-6", "label": "sonnet 4.6",
         "efforts": ["low", "medium", "high"], "default": True},
        {"model": "claude-haiku-4-5", "label": "haiku 4.5", "efforts": ["low", "medium"]},
    ],
    "codex": [
        {"model": "gpt-5.6", "label": "gpt 5.6", "efforts": ["low", "medium", "high"]},
        {"model": "gpt-5.5", "label": "gpt 5.5", "efforts": ["low", "medium", "high"]},
        {"model": "gpt-5.5-mini", "label": "gpt 5.5 mini", "efforts": ["low", "medium", "high"]},
        {"model": "gpt-5.4", "label": "gpt 5.4",
         "efforts": ["low", "medium", "high"], "default": True},
        {"model": "gpt-5.4-mini", "label": "gpt 5.4 mini", "efforts": ["low", "medium", "high"]},
    ],
}


def _catalog_overlay_path() -> Path:
    raw = os.environ.get("COX_MODELS_CATALOG")
    return Path(raw).expanduser() if raw else Path.home() / ".config" / "cox" / "models.json"


def catalog() -> dict[str, list[dict[str, Any]]]:
    """CATALOG merged with an optional ~/.config/cox/models.json overlay.

    Stdlib JSON only (no yaml dep). An overlay entry with a new `model` id is
    appended to that lane; one matching an existing id overrides it (change
    efforts/default). This is how future models are added without a code change.
    A malformed overlay falls back to the built-in catalog — it is a UI dropdown
    list, not the pinning path (models.resolve stays strict), so a typo must not
    break the dashboard.
    """
    merged: dict[str, list[dict[str, Any]]] = {
        lane: [dict(m) for m in items] for lane, items in CATALOG.items()
    }
    path = _catalog_overlay_path()
    if not path.exists():
        return merged
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return merged
    if not isinstance(data, dict):
        return merged
    for lane, items in data.items():
        if not isinstance(items, list):
            continue
        base = merged.setdefault(lane, [])
        by_id = {m.get("model"): i for i, m in enumerate(base)}
        for entry in items:
            if not (isinstance(entry, dict) and entry.get("model")):
                continue
            mid = entry["model"]
            if mid in by_id:
                base[by_id[mid]] = entry
            else:
                by_id[mid] = len(base)
                base.append(entry)
    return merged


class BosunConfigError(RuntimeError):
    """A config file exists but is unusable — crash rather than silently default."""


def _parse_spec(raw: str, provider: str = "anthropic") -> ModelSpec:
    model, _, effort = raw.partition(":")
    return ModelSpec(provider=provider, model=model.strip(), effort=(effort or "medium").strip())


def parse_spec(raw: str, provider: str = "anthropic") -> ModelSpec:
    """Public: parse a '<model>[:<effort>]' string into a ModelSpec (for --model)."""
    return _parse_spec(raw, provider)


def _spec_from_mapping(m: Any) -> ModelSpec | None:
    if isinstance(m, str):
        return _parse_spec(m)
    if isinstance(m, dict) and m.get("model"):
        return ModelSpec(
            provider=str(m.get("provider", "anthropic")),
            model=str(m["model"]),
            effort=str(m.get("effort", "medium")),
        )
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a config file. Present-but-unparseable is a hard error (unlike relay)."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as e:
        raise BosunConfigError(
            f"{path} exists but PyYAML is not installed — refusing to silently use "
            f"defaults (relay's silent-degrade bug). Install pyyaml or remove the file."
        ) from e
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - any parse failure is fatal here
        raise BosunConfigError(f"{path} is not valid YAML: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise BosunConfigError(f"{path} must be a mapping at top level")
    return data


def _global_path() -> Path:
    raw = os.environ.get("COX_MODELS_FILE")
    return Path(raw).expanduser() if raw else Path.home() / ".config" / "cox" / "models.yml"


def resolve(role: str, repo_path: Path | None = None, lane: str = "claude") -> ModelSpec:
    """Return a pinned ModelSpec for a role + lane. Never unpinned (DESIGN P8)."""
    if role not in _ROLE_DEFAULT:
        raise ValueError(f"unknown role {role!r}")

    # 1. env
    env = os.environ.get(_ENV_KEY[role])
    if env:
        return _parse_spec(env)

    # 2. repo policy
    if repo_path is not None:
        repo_cfg = repo_path / ".cox" / "repo.yml"
        if repo_cfg.exists():
            models = _load_yaml(repo_cfg).get("models", {})
            spec = _spec_from_mapping(models.get(role)) if isinstance(models, dict) else None
            if spec:
                return spec

    # 3. global policy
    gpath = _global_path()
    if gpath.exists():
        spec = _spec_from_mapping(_load_yaml(gpath).get(role))
        if spec:
            return spec

    # 4. built-in default — implementer follows the lane; reviewer is role-fixed
    if role == "implementer" and lane in _LANE_IMPL_DEFAULT:
        return _LANE_IMPL_DEFAULT[lane]
    return _ROLE_DEFAULT[role]
