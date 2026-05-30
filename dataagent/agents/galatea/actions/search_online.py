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
from __future__ import annotations

from langchain_core.messages import HumanMessage
from tavily import TavilyClient

from dataagent.core.managers.llm_manager.galatea_llm import LLM


def search_online(
    query: str,
    tavily_config: str | None = None,
    llm_config: dict | None = None,
) -> str:
    """
    Search the web for information based on the given query.

    Use this tool when:
    - Cannot find the information you need in the local workspace
    - You need search-engine-style discovery across multiple sites
    - You want a concise summary of relevant search results

    Do not use this tool when:
    - You need the raw content of a specific webpage
    - You need to inspect a page exactly as published, including its full body text
    - You already know the exact page and should fetch only a bounded excerpt with a CLI command instead

    Args:
        query: The search query string to look up on the web.

    Returns:
        A summarized, search-engine-like overview of the most relevant results.
    """
    if not tavily_config:
        raise ValueError("Tavily API key is required for the search_online tool.")

    client = TavilyClient(api_key=tavily_config)
    response = client.search(query)
    return _summarize_search_results(response, query, llm_config)


def _summarize_search_results(response: dict, query: str, llm_config: dict | None = None) -> str:
    if not response.get("results"):
        return f"No results were found matching '{query}'."

    contents = []
    for result in response.get("results", []):
        if isinstance(result, dict):
            content = result.get("content", "")
        elif isinstance(result, str):
            content = result
        else:
            continue
        if content:
            contents.append(content)

    results_content = "\n".join(contents)
    prompt = (
        f'Please summarize the following web search results for the query: "{query}"\n\n'
        f"Search Results:\n{results_content}\n\n"
        "Provide a concise summary of the most relevant information from these search results."
    )

    if not llm_config:
        raise ValueError("LLM is required for the search_online tool.")

    llm = LLM(llm_config)
    message = llm.invoke([HumanMessage(content=prompt)])
    return message.content if hasattr(message, "content") else str(message)
