# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

"""Integration test: verify Qwen prompt caching via httpx LLMClient.

Qwen prompt caching requires:
  - system message in content list format with cache_control: {"type": "ephemeral"}
  - cached prefix >= 1024 tokens
  - consecutive requests with the same prefix

The LLMClient auto-detects Qwen models by name and preserves cache_control
markers.  No litellm custom_llm_provider needed.

Env vars (either QWEN_PLUS_* or OPENAI_* as fallback):
  - QWEN_PLUS_API_KEY / OPENAI_API_KEY
  - QWEN_PLUS_BASE_URL / OPENAI_BASE_URL

Run:  QWEN_PLUS_API_KEY=... python -m pytest tests/ut/llm_manager/test_qwen_cache_reuse.py -xvs
"""

from __future__ import annotations

import os

import pytest

from dataagent.core.managers.llm_manager.llm_client import (
    LLMCallError,
    LLMClient,
)

_BASE_URL = os.getenv(
    "QWEN_PLUS_BASE_URL",
) or os.getenv(
    "OPENAI_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)
_API_KEY = os.getenv("QWEN_PLUS_API_KEY") or os.getenv("OPENAI_API_KEY")
_MODEL = os.getenv("QWEN_PLUS_MODEL", "Qwen3.7-Plus")

_LONG_SYSTEM_PROMPT = """You are an expert data analysis assistant specialized in biomedical research.
Your primary responsibilities include:
1. Parsing and extracting key entities from user queries in a biomedical research context.
2. Identifying cells, pseudoviruses, and antibodies mentioned in the query.
3. Matching these entities against reference lists provided to you.
4. Generating structured JSON output with the extracted entities.

Rules for entity extraction:
- Only match entities that appear EXACTLY in the reference lists.
- If an entity in the query does not match any reference item, treat it as unmatched.
- Set unmatched entity values to null in the output JSON.
- Always respond in the exact JSON format specified.

Reference data format:
- Cell lines will be provided as a list of strings.
- Pseudovirus names will be provided as a list of strings.
- Antibody names will be provided as a list of strings.

Output format requirements:
- The output must be a valid JSON object.
- Keys: "cell", "pseudovirus", "antibody".
- Values: matched reference string or null if unmatched.
- Do not include any explanation or extra text outside the JSON block.

Biomedical domain knowledge:
- HEK293T: Human embryonic kidney cells, commonly used in virology research.
- Vero E6: African green monkey kidney cells, standard for virus isolation.
- Huh-7: Human hepatoma cells, used in hepatitis and coronavirus studies.
- 293FT: Fast-growing HEK293 variant for protein expression.
- ACE2-expressing cells: Required for SARS-CoV-2 pseudovirus entry assays.
- BD55 series: Broadly neutralizing antibodies against SARS-CoV-2.
- S2H97: A specific antibody targeting SARS-CoV-2 spike protein.
- XBB.1.5: Omicron subvariant with enhanced immune evasion.
- XBB.1.16: Another Omicron subvariant (Arcturus).
- BA.5: Earlier Omicron subvariant.
- Pseudovirus: Safe surrogate virus particles for neutralization testing.
- IC50: Half-maximal inhibitory concentration for neutralization.
- IC80: 80% inhibitory concentration.
- Neutralization assay: Test measuring antibody blocking of virus entry.
- Cell line catalog: ATCC maintains reference cell line database.
- Pseudovirus nomenclature: WHO naming conventions for variants.

Data validation rules:
- Cross-check extracted entities against multiple reference lists.
- Verify cell line names match known catalogs (ATCC, DSMZ).
- Confirm pseudovirus names follow WHO nomenclature.
- Ensure antibody identifiers follow BD numbering convention.
- Reject entities that are partial matches or fuzzy matches.
- Flag ambiguous matches as null rather than guessing.

Safety considerations:
- Never provide medical advice or diagnostic conclusions.
- Only extract and match entities as specified.
- Flag potentially dangerous or misidentified entities.
- Maintain strict adherence to the reference data provided.

Performance optimization:
- Process queries efficiently and minimize response latency.
- Cache frequently accessed reference data where applicable.
- Prioritize accuracy over speed in entity matching.
- Validate all extracted entities against the full reference list before outputting.

Technical specifications:
- JSON output must be parseable by standard JSON parsers.
- No trailing commas or other JSON syntax errors.
- All string values must be properly quoted.
- Numeric or boolean values should not appear as strings.

Error handling:
- If the query is empty or nonsensical, return all keys as null.
- If reference lists are empty, return all keys as null.
- If the query contains only unmatched entities, return all keys as null.
- Log any parsing failures for debugging purposes.

Output formatting rules:
- Use lowercase for all keys in the JSON output.
- Maintain original case for matched entity values from reference lists.
- Include null values explicitly, do not omit keys with null values.
- Ensure the JSON is compact (no extra whitespace or formatting).
- The output must be a single JSON object, not an array.

This system prompt defines the complete behavior specification for the biomedical entity extraction assistant. Follow these instructions precisely for every query received. Do not add any content beyond what is specified here. Always produce the exact JSON format required. This specification is final and complete. End of system specification block. Repeat: Always follow all rules above. The assistant must output only valid JSON. No other text format is accepted. The JSON keys must be exactly cell, pseudovirus, antibody. Null values must be explicit. The assistant must verify all matches against reference lists. Partial or fuzzy matches are not allowed. If uncertain, set to null. The assistant handles Chinese and English queries. The assistant never provides medical advice. The assistant flags dangerous entities. The assistant maintains data integrity. The assistant prioritizes accuracy. The assistant validates before output. The assistant follows all examples. The assistant respects all formatting rules. The assistant handles errors gracefully. The assistant completes all tasks as specified. End of complete specification."""


@pytest.fixture()
def qwen_client() -> LLMClient:
    if not _API_KEY:
        pytest.skip("QWEN_PLUS_API_KEY not set — skipping integration test")
    return LLMClient(
        model=_MODEL,
        api_base=_BASE_URL,
        api_key=_API_KEY,
    )


def _invoke_or_skip(client: LLMClient, messages: list) -> object:
    try:
        return client.invoke(messages)
    except LLMCallError as e:
        if e.category in ("rate_limit", "connection", "server_error"):
            pytest.skip(f"Qwen API unavailable: {e}")
        raise


def test_qwen_cache_reuse_with_cache_control(qwen_client: LLMClient) -> None:
    system_msg = {"role": "system", "content": _LONG_SYSTEM_PROMPT}

    first = _invoke_or_skip(
        qwen_client,
        [system_msg, {"role": "user", "content": "What is 1+1?"}],
    )
    print("\n=== First call (cache creation expected) ===")
    print(f"usage: {first.usage_metadata}")
    cache_creation_1 = first.usage_metadata.get("input_cache_creation_tokens", 0)
    print(f"cache_creation_tokens: {cache_creation_1}")

    second = _invoke_or_skip(
        qwen_client,
        [system_msg, {"role": "user", "content": "What is 2+2?"}],
    )
    print("\n=== Second call (cache reuse expected) ===")
    print(f"usage: {second.usage_metadata}")
    cache_read_2 = second.usage_metadata.get("input_cache_read_tokens", 0)
    cache_creation_2 = second.usage_metadata.get("input_cache_creation_tokens", 0)
    print(f"cache_read_tokens: {cache_read_2}")
    print(f"cache_creation_tokens: {cache_creation_2}")

    assert second.content, f"Empty response from Qwen: {second}"
    cache_creation_1_val = first.usage_metadata.get("input_cache_creation_tokens", 0)
    cache_read_1_val = first.usage_metadata.get("input_cache_read_tokens", 0)
    assert cache_creation_1_val > 0 or cache_read_1_val > 0, (
        f"Expected cache_creation or cache_read > 0 on first call, "
        f"got creation={cache_creation_1_val}, read={cache_read_1_val}"
    )
    cache_read_2_val = second.usage_metadata.get("input_cache_read_tokens", 0)
    assert cache_read_2_val > 0, f"Expected cache_read > 0 on second call, got {cache_read_2_val}"
