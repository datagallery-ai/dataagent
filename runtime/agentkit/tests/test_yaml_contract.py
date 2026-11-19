import json
from pathlib import Path

import pytest

from dataagent.strict_yaml import parse_yaml

CASES = json.loads(
    (Path(__file__).resolve().parents[3] / "tests/fixtures/yaml-contract.json").read_text()
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_yaml_contract(case):
    if case.get("reject"):
        with pytest.raises(ValueError) as caught:
            parse_yaml(case["text"], "fixture.yaml")
        assert "fixture.yaml" in str(caught.value)
        assert "secret" not in str(caught.value)
    else:
        assert parse_yaml(case["text"]) == case["value"]
