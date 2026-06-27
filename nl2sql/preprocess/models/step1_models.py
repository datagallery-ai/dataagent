from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class StepResult:
    step_name: str
    status: str
    processed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    outputs: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_name": self.step_name,
            "status": self.status,
            "processed_count": self.processed_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "outputs": list(self.outputs),
            "details": dict(self.details),
            "elapsed_seconds": self.elapsed_seconds,
        }
