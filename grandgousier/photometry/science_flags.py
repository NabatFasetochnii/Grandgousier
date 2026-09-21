from __future__ import annotations

from typing import Any, Mapping


def merge_effective_science_flags(
    base_flags: Mapping[str, Any] | None,
    science_flags: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the public science flag view as immutable BASE || SCIENCE.

    Legacy ``science_measurements.flags`` was intentionally compact for materialized
    0.6 products.  Detector/catalog/per-epoch provenance remains on the immutable
    BASE measurement referenced by ``base_measurement_id``.  Science-specific
    keys override BASE keys, matching PostgreSQL JSONB ``||`` semantics for
    top-level objects.
    """

    merged = dict(base_flags or {})
    merged.update(dict(science_flags or {}))
    return merged


def compact_science_flags_against_base(
    base_flags: Mapping[str, Any] | None,
    effective_flags: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the minimal SCIENCE overlay that preserves ``BASE || SCIENCE``.

    Python reconstruction historically copied the complete accumulated effective
    flag payload into legacy ``science_measurements.flags``.  That duplicates immutable
    BASE detector/catalog/per-epoch provenance for every retained science row.
    The public reader already reconstructs effective flags as BASE followed by
    SCIENCE, so keys whose value is unchanged from BASE are redundant.

    This helper removes only exact top-level duplicates.  Changed values, new
    science keys, explicit ``None`` overrides, nested dictionaries, and lists are
    retained verbatim.  The reconstruction contract requires the accumulated
    effective mapping to retain every BASE key; missing BASE keys are rejected
    rather than silently changing public flag semantics.  Under that invariant,
    ``merge_effective_science_flags(base, compact)`` is exactly equivalent to the
    original effective mapping for JSON-compatible values.
    """

    base = dict(base_flags or {})
    effective = dict(effective_flags or {})
    missing_base_keys = set(base) - set(effective)
    if missing_base_keys:
        missing_preview = ", ".join(sorted(str(key) for key in missing_base_keys)[:8])
        raise ValueError(
            "effective science flags must retain every BASE key before delta compaction; "
            f"missing={missing_preview}"
        )
    return {
        key: value
        for key, value in effective.items()
        if key not in base or base[key] != value
    }


def exact_top_level_flag_patch(
    reconstructed_flags: Mapping[str, Any] | None,
    historical_flags: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Return the minimal top-level patch from reconstructed to historical flags.

    The returned tuple is ``(override_flags, removed_keys)`` and satisfies::

        apply_top_level_flag_patch(reconstructed, override_flags, removed_keys)
        == historical

    for ordinary JSON-compatible mappings.  Keys absent from the historical
    mapping are represented explicitly in ``removed_keys``; changed/new values
    live in ``override_flags``.
    """

    reconstructed = dict(reconstructed_flags or {})
    historical = dict(historical_flags or {})
    removed = sorted(str(key) for key in (set(reconstructed) - set(historical)))
    overrides = {
        str(key): value
        for key, value in historical.items()
        if key not in reconstructed or reconstructed.get(key) != value
    }
    return overrides, removed


def apply_top_level_flag_patch(
    reconstructed_flags: Mapping[str, Any] | None,
    override_flags: Mapping[str, Any] | None,
    removed_keys: list[str] | tuple[str, ...] | set[str] | None,
) -> dict[str, Any]:
    """Apply one exact historical top-level flag patch."""

    restored = dict(reconstructed_flags or {})
    for key in removed_keys or ():
        restored.pop(str(key), None)
    restored.update(dict(override_flags or {}))
    return restored
