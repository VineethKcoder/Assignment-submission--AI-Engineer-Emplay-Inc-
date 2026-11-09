"""Field specification loader (config/fields.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import yaml


@dataclass(frozen=True)
class GroupSpec:
    key: str
    agent: str
    focus: str


@dataclass(frozen=True)
class FieldSpec:
    name: str
    group: str
    description: str
    queries: tuple[str, ...] = field(default_factory=tuple)
    format: str = "text"
    addendum_sensitive: bool = False
    evidence_k: int | None = None
    generated: bool = False
    on_addendum: str = "append"  # replace | append

    @property
    def rerank_query(self) -> str:
        """Natural-language description used by the cross-encoder (better than keyword queries)."""
        return f"{self.name.replace('_', ' ')}: {self.description}"[:240]

    @property
    def is_date(self) -> bool:
        return "YYYY" in self.format


@dataclass(frozen=True)
class FieldConfig:
    fields: dict[str, FieldSpec]
    groups: dict[str, GroupSpec]

    def fields_in_group(self, group: str) -> list[FieldSpec]:
        return [f for f in self.fields.values() if f.group == group and not f.generated]

    @property
    def extractable(self) -> list[FieldSpec]:
        return [f for f in self.fields.values() if not f.generated]


_REPLACE_DEFAULT = {"Due Date", "Pre Bid Meeting", "Delivery Date", "Term of Bid", "Bid Submission Type"}


@lru_cache(maxsize=4)
def load_field_config(path: str) -> FieldConfig:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    groups = {k: GroupSpec(k, v.get("agent", k), v.get("focus", "")) for k, v in (data.get("groups") or {}).items()}
    fields: dict[str, FieldSpec] = {}
    for f in data.get("fields", []):
        name = f["name"]
        fields[name] = FieldSpec(
            name=name,
            group=f.get("group", "misc"),
            description=f.get("description", ""),
            queries=tuple(f.get("queries", []) or [name]),
            format=str(f.get("format", "text")),
            addendum_sensitive=bool(f.get("addendum_sensitive", False)),
            evidence_k=f.get("evidence_k"),
            generated=bool(f.get("generated", False)),
            on_addendum=f.get("on_addendum", "replace" if name in _REPLACE_DEFAULT else "append"),
        )
    return FieldConfig(fields=fields, groups=groups)
