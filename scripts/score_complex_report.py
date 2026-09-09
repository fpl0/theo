"""Validate a separately authored transcript review; never invokes a model.

Scores are subjective reviewer judgments, not an automated proof of personality.
The review must name its reviewer, cover every native transcript, and explain
each judgment. The original evidence report is retained and hashed in the score.
"""

import argparse
import hashlib
import json
from pathlib import Path

DIMENSIONS = ("correctness", "evidence", "completion", "voice", "judgment")


def score(report, review):
    cases = report.get("cases", [])
    native = [case for case in cases if case.get("kind") == "native"]
    if not native or not review.get("reviewer") or not review.get("method"):
        raise ValueError("Native evidence and an identified review method are required")
    records = review.get("cases", [])
    names = [record.get("name") for record in records]
    if len(names) != len(set(names)) or set(names) != {case["name"] for case in native}:
        raise ValueError("Review must cover each native transcript exactly once")
    for record in records:
        scores = record.get("scores", {})
        if set(scores) != set(DIMENSIONS) or any(
            type(value) is not int or not 1 <= value <= 5 for value in scores.values()
        ):
            raise ValueError("Each dimension requires an integer score from 1 to 5")
        if not isinstance(record.get("notes"), str) or not record["notes"].strip():
            raise ValueError("Each transcript needs an evidence-based review note")
        if not isinstance(record.get("critical_violations"), list):
            raise ValueError("Each review must explicitly list any critical violations")
    automatic = (
        report.get("automated_pass") is True
        and not report.get("errors")
        and bool(cases)
        and all(
            case.get("automated_pass") is True
            and bool(case.get("checks"))
            and all(value is True for value in case["checks"].values())
            for case in cases
        )
    )
    quality = all(
        min(record["scores"].values()) >= 4 and not record["critical_violations"]
        for record in records
    )
    return {
        "accepted": automatic and quality,
        "automated_pass": automatic,
        "quality_pass": quality,
        "native_transcripts": len(native),
        "minimum_score": min(min(record["scores"].values()) for record in records),
        "dimension_means": {
            dimension: round(
                sum(record["scores"][dimension] for record in records) / len(records), 2
            )
            for dimension in DIMENSIONS
        },
        "critical_violations": sum(len(record["critical_violations"]) for record in records),
        "review": review,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.report.read_bytes()
    result = score(json.loads(raw), json.loads(args.review.read_text()))
    result["report_sha256"] = hashlib.sha256(raw).hexdigest()
    result["report_file"] = args.report.name
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "review"}))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
