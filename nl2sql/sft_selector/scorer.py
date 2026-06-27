"""Step4a 逐点式 SFT 选择器的极简 vLLM 打分器。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List


NEG_INF = -1e9


@dataclass(frozen=True)
class ScoreRequest:
    question_id: int
    candidate_idx: int
    question: str
    evidence: str
    sql: str


@dataclass(frozen=True)
class ScoreResult:
    question_id: int
    candidate_idx: int
    sql: str
    yes_logprob: float
    no_logprob: float
    yes_probability: float

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "candidate_idx": self.candidate_idx,
            "sql": self.sql,
            "yes_logprob": self.yes_logprob,
            "no_logprob": self.no_logprob,
            "yes_probability": self.yes_probability,
        }


class YesNoVLLMScorer:
    def __init__(
        self,
        model_path: str,
        prompt_template_path: str | Path,
        tensor_parallel_size: int = 1,
        max_model_len: int = 4096,
        dtype: str = "bfloat16",
        gpu_memory_utilization: float = 0.85,
        topk_logprobs: int = 20,
        chunk_size: int = 2048,
        enforce_eager: bool = True,
        qwen3_empty_think_prefix: bool = True,
        trust_remote_code: bool = True,
    ):
        from transformers import AutoTokenizer
        from vllm import LLM, SamplingParams

        self.template = Path(prompt_template_path).read_text(encoding="utf-8")
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        self.yes_token_id = self._single_token_id("Yes")
        self.no_token_id = self._single_token_id("No")
        self.chunk_size = chunk_size
        self.qwen3_empty_think_prefix = qwen3_empty_think_prefix
        self.sampling_params = SamplingParams(max_tokens=1, temperature=0.0, logprobs=topk_logprobs)
        self.llm = LLM(
            model=model_path,
            tensor_parallel_size=tensor_parallel_size,
            dtype=dtype,
            max_model_len=max_model_len,
            trust_remote_code=trust_remote_code,
            enforce_eager=enforce_eager,
            gpu_memory_utilization=gpu_memory_utilization,
        )

    def score(self, requests: Iterable[ScoreRequest]) -> List[ScoreResult]:
        rows = list(requests)
        results: List[ScoreResult] = []
        for start in range(0, len(rows), self.chunk_size):
            batch = rows[start : start + self.chunk_size]
            outputs = self.llm.generate([self._prompt(row) for row in batch], self.sampling_params, use_tqdm=False)
            results.extend(self._score_output(row, output) for row, output in zip(batch, outputs))
        return results

    def _single_token_id(self, text: str) -> int:
        token_ids = self.tokenizer.encode(text, add_special_tokens=False)
        if len(token_ids) != 1:
            raise ValueError(f"Expected single-token {text!r}, got {token_ids}")
        return token_ids[0]

    def _prompt(self, row: ScoreRequest) -> str:
        content = (
            self.template.replace("{question}", row.question or "")
            .replace("{external_knowledge}", row.evidence or "")
            .replace("{solution_text}", row.sql or "")
        )
        prompt = self.tokenizer.apply_chat_template(
            [{"role": "user", "content": content}],
            add_generation_prompt=True,
            tokenize=False,
        )
        if self.qwen3_empty_think_prefix:
            prompt += "<think>\n\n</think>\n\n"
        return prompt

    def _score_output(self, row: ScoreRequest, output) -> ScoreResult:
        logprobs = output.outputs[0].logprobs[0] if output.outputs[0].logprobs else {}
        yes = logprobs.get(self.yes_token_id)
        no = logprobs.get(self.no_token_id)
        yes_logprob = float(yes.logprob) if yes is not None else NEG_INF
        no_logprob = float(no.logprob) if no is not None else NEG_INF
        yes_probability = 0.0 if yes_logprob <= NEG_INF + 1 else math.exp(yes_logprob)
        return ScoreResult(row.question_id, row.candidate_idx, row.sql, yes_logprob, no_logprob, yes_probability)
