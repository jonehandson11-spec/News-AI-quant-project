"""Quality companions supplied by a trusted GVS data source, separate from market factors."""

from collections.abc import Mapping
from math import isfinite


def _score(item, default_reason, *, metadata=(), require=()):
    if not isinstance(item, Mapping):
        return {"value": None, "missing_reason": default_reason}
    result = {key: item[key] for key in metadata
              if isinstance(item.get(key), str) and item[key].strip()}
    score = item.get("value")
    valid = (type(score) in (int, float) and 0 <= score <= 1 and isfinite(score)
             and all(key in result for key in require))
    reason = item.get("missing_reason")
    result.update(value=score if valid else None,
                  missing_reason=None if valid else (reason if isinstance(reason, str) and reason else default_reason))
    return result


def read_quality(source, asset, *, as_of, profile_id):
    """Invoke a read-only adapter with the same cutoff as its market-state query."""
    if source is None:
        return {"evidence_reliability": _score(None, "ESTIMATOR_NOT_READY"),
                "data_coverage": _score(None, "REFERENCE_MISSING")}
    try:
        supplied = source(asset=asset, as_of=as_of, profile_id=profile_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return {key: _score(None, "SOURCE_UNAVAILABLE")
                for key in ("evidence_reliability", "data_coverage")}

    supplied = supplied if isinstance(supplied, Mapping) else {}
    evidence = supplied.get("evidence_reliability")
    coverage = supplied.get("data_coverage")
    evidence_metadata = ("method_version", "source_ref")
    reliability = _score(evidence, "NOT_OBSERVED", metadata=evidence_metadata,
                         require=("method_version",))
    per_variable = evidence.get("by_variable") if isinstance(evidence, Mapping) else None
    if isinstance(per_variable, Mapping):
        reliability["by_variable"] = {
            variable: _score(item, "NOT_OBSERVED", metadata=evidence_metadata,
                             require=("method_version",))
            for variable, item in per_variable.items() if isinstance(variable, str)
        }
    return {"evidence_reliability": reliability,
            "data_coverage": _score(coverage, "REFERENCE_MISSING",
                                    metadata=("scope_id", "method_version", "source_ref"),
                                    require=("scope_id", "method_version"))}
