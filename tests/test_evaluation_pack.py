import json
from pathlib import Path


def test_fixed_pack_preserves_all_thirty_cases_and_critical_rubric():
    pack = json.loads((Path(__file__).parents[1] / "evaluations/behaviour.json").read_text())
    assert len(pack["cases"]) >= 30
    assert len({case["id"] for case in pack["cases"]}) == len(pack["cases"])
    assert all(case["expected"] and case["fixture"] and case["prompt"] for case in pack["cases"])
    assert pack["acceptance"]["minimum_acceptable_fraction"] >= 0.9
    assert pack["acceptance"]["critical_violations"] == 0
    assert pack["acceptance"]["max_live_runs_per_batch"] <= 20
    assert pack["acceptance"]["live_concurrency"] == 1
