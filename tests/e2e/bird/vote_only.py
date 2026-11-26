"""Experiment boundary: forbid model fallback when deterministic vote has no valid group."""

from functools import wraps


class NoValidVote(RuntimeError):
    pass


def install(cls):
    old = cls._aprocess
    if getattr(old, "_vote_only", False):
        return

    @wraps(old)
    async def guarded(self, state, *args, **kwargs):
        assert self.shortcut == 1
        best, _ = self._vote(state["execution_results"])
        if best is None:
            state.update(sql="", confidence=0.0, proceed=False, error="NO_VALID_VOTE")
            raise NoValidVote("NO_VALID_VOTE")
        return await old(self, state, *args, **kwargs)

    guarded._vote_only = True
    cls._aprocess = guarded
