"""Check incident queries against synthetic series using the running local Prometheus."""

import json
import subprocess
from pathlib import Path


def fixture(root):
    rules = {
        rule["uid"]: rule
        for group in json.loads((root / "grafana/provisioning/alerting/rules.yaml").read_text())[
            "groups"
        ]
        for rule in group["rules"]
    }
    tests = []
    for case in json.loads((root / "tests/incident-cases.json").read_text()):
        expression = rules[case["rule_uid"]]["data"][0]["model"]["expr"]
        tests.append(
            {
                "name": case["name"],
                "interval": "1m",
                "input_series": case["input_series"],
                "promql_expr_test": [
                    {"expr": expression, "eval_time": "10m", "exp_samples": case["expected"]}
                ],
            }
        )
    for case in json.loads((root / "tests/poller-cases.json").read_text()):
        expression = rules["theo-telegram-poller"]["data"][0]["model"]["expr"]
        tests.append(
            {
                "name": case["name"],
                "interval": "1m",
                "input_series": case["input_series"],
                "promql_expr_test": [
                    {
                        "expr": "(" + expression + ") > 0",
                        "eval_time": "10m",
                        "exp_samples": [
                            {
                                "labels": '{environment="'
                                + case.get("environment", "local")
                                + '"}',
                                "value": 1,
                            }
                        ]
                        if case["should_alert"]
                        else [],
                    }
                ],
            }
        )
    return {"evaluation_interval": "1m", "tests": tests}


def main():
    root = Path(__file__).resolve().parents[1] / "observability"
    value = fixture(root)
    result = subprocess.run(
        [
            "docker",
            "--context",
            "colima-theo-observability",
            "compose",
            "-f",
            str(root / "compose.yaml"),
            "exec",
            "-T",
            "prometheus",
            "promtool",
            "test",
            "rules",
            "/dev/stdin",
        ],
        input=json.dumps(value),
        text=True,
        check=False,
    )
    print(json.dumps({"behavioral_cases": len(value["tests"]), "passed": result.returncode == 0}))
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
