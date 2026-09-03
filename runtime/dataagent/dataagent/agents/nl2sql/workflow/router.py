"""Pure routing helpers for the native NL2SQL LangGraph."""

from dataclasses import dataclass

from langgraph.graph import END

from dataagent.agents.nl2sql.workflow.state import NL2SQLState


@dataclass(frozen=True)
class NL2SQLRouter:
    """Route between enabled NL2SQL nodes without the retired BaseRouter layer."""

    enabled_nodes: tuple[str, ...]

    @property
    def entry_point(self) -> str:
        """Return the first enabled pipeline node."""
        return self.enabled_nodes[0]

    def route_from_perceptor(self, state: NL2SQLState) -> str:
        """Route after perceptor to the next enabled node."""
        return self._next("perceptor")

    def route_from_generator(self, state: NL2SQLState) -> str:
        """Route after generator to the next enabled node."""
        return self._next("generator")

    def route_from_validator(self, state: NL2SQLState) -> str:
        """Route after validator to the next enabled node."""
        return self._next("validator")

    def route_from_reflector(self, state: NL2SQLState) -> str:
        """Route a failed validation back through validation after reflection."""
        return self._next("reflector") if state.get("proceed", False) else "validator"

    def route_from_executor(self, state: NL2SQLState) -> str:
        """Route after executor to the next enabled node."""
        return self._next("executor")

    def route_from_selector(self, state: NL2SQLState) -> str:
        """Finish a selected result or return it to the reflector."""
        return END if state.get("proceed", False) else "reflector"

    def route(self, node_name: str, state: NL2SQLState) -> str:
        """Dispatch routing for one configured node."""
        route_method = getattr(self, f"route_from_{node_name}")
        return route_method(state)

    def _next(self, current: str) -> str:
        index = self.enabled_nodes.index(current)
        if index + 1 >= len(self.enabled_nodes):
            return END
        return self.enabled_nodes[index + 1]
