"""Remove personal data (input values, explanations quoting them) from stored results."""

from __future__ import annotations

import json
from typing import Any


def redact_result(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    out["hits"] = [
        {**h, "input_value": None, "explanation": _strip_quoted(h.get("explanation", ""))}
        for h in data.get("hits", [])
    ]
    out["geo_findings"] = [
        {**g, "input_value": None, "explanation": _strip_quoted(g.get("explanation", ""))}
        for g in data.get("geo_findings", [])
    ]
    out["summary"] = ""
    out["stateless"] = True
    return out


def _strip_quoted(text: str) -> str:
    marker = " value '"
    while marker in text:
        start = text.index(marker)
        end = text.find("'", start + len(marker))
        if end == -1:
            break
        text = text[:start] + text[end + 1 :]
    return text


def redact_result_json(result_json: str) -> str:
    return json.dumps(redact_result(json.loads(result_json)))
