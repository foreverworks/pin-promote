"""Which checks passed: the evidence a promotion needs before any pin moves.

Evidence comes from the caller, in either form or both:

* names given one by one (`--passed smoke`), for a check the calling job ran itself (a name the
  job results also hold, with any result but `success`, is an error: a hand cannot overrule a CI run);
* a CI system's job results as JSON. GitHub Actions' `toJSON(needs)` is the shape read here:
  `{"<job>": {"result": "success", "outputs": {...}}, ...}`. Only `success` counts as passed.

`missing` is pure: the required checks, the evidence, and what is still missing.
"""

import json
from typing import cast


class EvidenceError(ValueError):
    """The job results could not be read, or they contradict a name passed by hand."""


def from_needs(text: str) -> dict[str, str]:
    """{job: result} from GitHub Actions' `toJSON(needs)`."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise EvidenceError(f"the job results are not JSON: {error}") from error
    if not isinstance(raw, dict):
        raise EvidenceError("the job results must be a JSON object of job -> {result: ...}")
    results: dict[str, str] = {}
    for job, body in cast(dict[str, object], raw).items():
        result = cast(dict[str, object], body).get("result") if isinstance(body, dict) else None
        if not isinstance(result, str):
            raise EvidenceError(f"the job results: {job} has no result")
        results[job] = result
    return results


def evidence(passed: list[str], needs: dict[str, str]) -> dict[str, str]:
    """One {check: result} map; a name passed by hand is a success, unless the job results say otherwise."""
    out = dict(needs)
    for name in passed:
        if name in needs and needs[name] != "success":
            raise EvidenceError(f"{name}: the job results say {needs[name]}; --passed cannot override it")
        out[name] = "success"
    return out


def missing(required: list[str], results: dict[str, str]) -> list[str]:
    """The required checks that did not pass, each with what was seen ("not run" when absent)."""
    return [f"{name} ({results.get(name, 'not run')})" for name in required if results.get(name) != "success"]
