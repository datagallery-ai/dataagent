import json
from pathlib import Path


def inject_evidence(value, runtime, *, params):
    item = json.loads(Path(params["case_input"]).read_text())
    assert value["question"].strip() == item["question"].strip()
    return {"evidence": item.get("evidence", "")}
