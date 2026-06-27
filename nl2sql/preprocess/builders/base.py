from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

from ..core.context import Step1BuildContext
from ..core.progress import ProgressStore
from ..models.step1_models import StepResult

logger = logging.getLogger(__name__)


class StepBuilder(ABC):
    step_name: str = "base"

    def __init__(self, context: Step1BuildContext):
        self.context = context
        self.progress = ProgressStore(self.context.artifacts.step_progress_path(self.step_name))

    def run(self) -> StepResult:
        start = time.time()
        self.progress.mark_running()
        try:
            result = self._run_impl()
            result.elapsed_seconds = time.time() - start
            self.progress.mark_success(result=result.to_dict())
            return result
        except Exception as exc:
            logger.exception("Step %s failed", self.step_name)
            self.progress.mark_failed(str(exc))
            raise

    @abstractmethod
    def _run_impl(self) -> StepResult:
        raise NotImplementedError()
