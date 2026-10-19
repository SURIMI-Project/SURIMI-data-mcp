"""The registry of data sources and the tools that read them, and the check that the registry matches the registered tools."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


#: The reasons a question cannot be answered. `not_served` means the data exists but no tool reads it,
#: `no_mechanism` means no engine can compute it, and `not_collected` means nobody gathered the data.
_WHY = ("not_served", "no_mechanism", "not_collected")


@dataclass(frozen=True)
class CannotAnswer:
    """One class of question this server cannot answer, why not, and where the answer lives.

    `why` must be one of `_WHY`, otherwise construction raises ValueError. `lives_in` is empty when the answer is held nowhere.
    """

    question_class: str
    why: str
    detail: str
    lives_in: str = ""

    def __post_init__(self) -> None:
        if self.why not in _WHY:
            raise ValueError(
                f"why must be one of {_WHY}, got {self.why!r}")


@dataclass(frozen=True)
class DataSourceCapability:
    """One data source: its id, title, domain, description, coverage, the tools that read it and what it cannot answer."""

    source_id: str
    title: str
    domain: str
    description: str
    coverage: dict[str, Any]
    tools: tuple[str, ...]
    cannot_answer: tuple[CannotAnswer, ...] = ()


_REGISTRY: list[DataSourceCapability] = []


def register(spec: DataSourceCapability) -> None:
    """Add `spec` to the registry, replacing any entry with the same `source_id`."""
    global _REGISTRY
    _REGISTRY = [c for c in _REGISTRY if c.source_id != spec.source_id]
    _REGISTRY.append(spec)


def all_capabilities() -> list[DataSourceCapability]:
    """Return a copy of the registered data sources."""
    return list(_REGISTRY)


def claimed_tools() -> set[str]:
    """Return the names of all tools that some registered source lists."""
    return {t for cap in _REGISTRY for t in cap.tools}


def describe(tool_names: list[str]) -> dict[str, Any]:
    """Return the registered sources, a tool inventory grouped by layer and by domain, and a usage hint.

    `tool_names` is the list of tools the server has registered.
    """
    return {
        "data_sources": [asdict(c) for c in _REGISTRY],
        "tool_inventory": {
            "total": len(tool_names),
            "layers": _bucket_by_layer(tool_names),
            "by_domain": _bucket_by_domain(tool_names),
        },
        "usage_hint": (
            "Call list_datasets, then list_columns. For any number call query_metric "
            "(names from list_metrics) or aggregate; use query_data for row lookups. "
            "analyze_trend, compare_countries, breakdown and generate_report run the "
            "analysis on the server and return a finding with its calculation records."
        ),
    }


def drift_check(registered_tool_names: set[str]) -> dict[str, list[str]]:
    """Compare the registry with the registered tools and return the names missing on each side.

    `missing_from_registry` lists tools no source claims, and `missing_from_fastmcp` lists claimed tools that are not registered.
    """
    claimed = claimed_tools()
    return {
        "missing_from_registry": sorted(registered_tool_names - claimed),
        "missing_from_fastmcp": sorted(claimed - registered_tool_names),
    }


def _classify(name: str) -> str:
    if name == "describe_data_capabilities":
        return "meta"
    if name.startswith("list_"):
        return "lookup"
    if name.startswith("query_") or name.startswith("describe_"):
        return "fetcher"
    if name.startswith("search_") or name == "explain_indicator":
        return "search"
    return "recipe"


def _bucket_by_layer(tool_names: list[str]) -> dict[str, list[str]]:
    layers: dict[str, list[str]] = {
        "meta": [], "lookup": [], "fetcher": [], "search": [], "recipe": [],
    }
    for name in tool_names:
        layers.setdefault(_classify(name), []).append(name)
    return layers


def _bucket_by_domain(tool_names: list[str]) -> dict[str, list[str]]:
    by_domain: dict[str, list[str]] = {}
    for cap in _REGISTRY:
        for t in cap.tools:
            by_domain.setdefault(cap.domain, []).append(t)
    unclaimed = [t for t in tool_names if t not in claimed_tools()]
    if unclaimed:
        by_domain["_unclaimed"] = unclaimed
    return by_domain
