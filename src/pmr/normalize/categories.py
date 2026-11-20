"""Venue category and tag labels to the project taxonomy (D-08, DQ-15)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path("config/categories.yaml")
IGNORED_LABELS = frozenset({"all", "featured", "trending", "new", "hide from new", "recurring"})


@dataclass(frozen=True)
class CategoryMap:
    """Exact per-venue matches plus ordered keyword rules."""

    taxonomy: tuple[str, ...]
    default: str
    exact: Mapping[str, Mapping[str, str]]
    keywords: tuple[tuple[str, tuple[str, ...]], ...]

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> CategoryMap:
        """Build from the parsed YAML; every target must be in the taxonomy."""
        taxonomy = tuple(d["taxonomy"])
        exact = {
            venue: {str(k).lower(): str(v) for k, v in (m or {}).items()}
            for venue, m in (d.get("exact") or {}).items()
        }
        keywords = tuple(
            (str(cat), tuple(str(w).lower() for w in words))
            for cat, words in (d.get("keywords") or {}).items()
        )
        targets = {v for m in exact.values() for v in m.values()} | {c for c, _ in keywords}
        bad = targets - set(taxonomy)
        if bad:
            raise ValueError(f"categories not in the taxonomy: {sorted(bad)}")
        return cls(taxonomy, str(d.get("default", "other")), exact, keywords)

    def map(self, venue: str, labels: Iterable[str | None]) -> str:
        """Project category for a market's raw labels (``default`` when nothing matches)."""
        exact = self.exact.get(venue, {})
        for raw in labels:
            if not raw:
                continue
            label = raw.strip().lower()
            if not label or label in IGNORED_LABELS:
                continue
            if label in exact:
                return exact[label]
            for cat, words in self.keywords:
                if any(_contains(label, w) for w in words):
                    return cat
        return self.default


def _contains(label: str, word: str) -> bool:
    """Whole-word containment for short words, substring for longer ones."""
    if len(word) <= 3:
        tokens = label.replace("-", " ").replace("_", " ").replace("/", " ").split()
        return word in tokens
    return word in label


@lru_cache(maxsize=4)
def load(path: str = str(DEFAULT_PATH)) -> CategoryMap:
    """Load and cache ``config/categories.yaml``."""
    with Path(path).open(encoding="utf-8") as fh:
        return CategoryMap.from_dict(yaml.safe_load(fh))
