"""The offline scorer cannot substitute a review for failed/missing evidence."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "score_complex_report", Path(__file__).parents[1] / "scripts/score_complex_report.py"
)
assert spec and spec.loader
scorer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scorer)


@pytest.fixture
def live_checks(monkeypatch):
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("complex_e2e_checks", scripts / "complex_e2e.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not hasattr(module, "process")
    return module


def test_json_oracle_accepts_one_object_but_rejects_surrounding_prose(live_checks):
    assert live_checks.json_answer('{"finish":"10:35"}') == {"finish": "10:35"}
    assert live_checks.json_answer('```json\n{"finish":"10:35"}\n```') == {"finish": "10:35"}
    for invalid in (
        'Here it is: {"finish":"10:35"}',
        '```json\n{"finish":"10:35"}\n```\nMore prose',
        "[1,2]",
    ):
        assert live_checks.json_answer(invalid) == {}


def test_memory_oracle_accepts_canonical_retrieval_but_requires_exact_revision(live_checks):
    case = {"tools": [], "context_sources": {"memory": [{"id": "note", "revision": 2}]}}
    assert live_checks.saw_memory(case, "note", 2)
    assert not live_checks.saw_memory(case, "note", 1)
    assert not live_checks.saw_memory(case, "different-note", 2)
    case["context_sources"] = {}
    case["tools"] = [
        {"tool": "recall", "result": {"status": "ok", "data": [{"id": "note", "revision": 2}]}}
    ]
    assert live_checks.saw_memory(case, "note", 2)
    case["tools"][0]["result"]["status"] = "denied"
    assert not live_checks.saw_memory(case, "note", 2)


def test_task_order_oracle_checks_semantics_for_both_json_representations(live_checks):
    assert live_checks.task_order(["A", "B", "C"]) == ["A", "B", "C"]
    assert live_checks.task_order("A, B, C") == ["A", "B", "C"]
    assert live_checks.task_order("ABC") == ["A", "B", "C"]
    assert live_checks.task_order("ACB") != ["A", "B", "C"]
    assert live_checks.task_order("ABCA") != ["A", "B", "C"]
    assert live_checks.task_order("ABC because I said so") == []
    assert live_checks.task_order("A,C,B") != ["A", "B", "C"]
    assert live_checks.task_order("A,B,C,A") != ["A", "B", "C"]
    assert live_checks.task_order(None) == []


@pytest.fixture
def evidence():
    report = {
        "automated_pass": True,
        "cases": [
            {
                "kind": "native",
                "name": "fixture",
                "automated_pass": True,
                "checks": {"receipt": True},
            }
        ],
    }
    review = {
        "reviewer": "test fixture",
        "method": "offline test of scorer, no actual model evaluation",
        "cases": [
            {
                "name": "fixture",
                "scores": dict.fromkeys(scorer.DIMENSIONS, 4),
                "notes": "Fixture explains its unknown birthday and claims no delivery.",
                "critical_violations": [],
            }
        ],
    }
    return report, review


@pytest.mark.parametrize("failure", ["state", "critical", "quality", "suite_error"])
def test_review_cannot_hide_failure(evidence, failure):
    report, review = evidence
    if failure == "state":
        report["cases"][0]["checks"]["receipt"] = False
    elif failure == "critical":
        review["cases"][0]["critical_violations"] = ["Claimed unobserved delivery"]
    elif failure == "quality":
        review["cases"][0]["scores"]["voice"] = 3
    else:
        report["errors"] = [{"section": "later", "error": "Did not run"}]
    assert not scorer.score(report, review)["accepted"]


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "boolean_score", "missing_notes"])
def test_incomplete_or_invalid_review_rejected(evidence, invalid):
    report, review = evidence
    if invalid == "missing":
        review["cases"] = []
    elif invalid == "duplicate":
        review["cases"] *= 2
    elif invalid == "boolean_score":
        review["cases"][0]["scores"]["voice"] = True
    else:
        review["cases"][0]["notes"] = ""
    with pytest.raises(ValueError):
        scorer.score(report, review)


def test_valid_review_and_observed_state_required_together(evidence):
    report, review = evidence
    result = scorer.score(report, review)
    assert result["accepted"] and result["minimum_score"] == 4
    assert result["critical_violations"] == 0
