from deepagents_runtime import (
    extra_backend,
    extra_interrupt_on,
    extra_middleware,
    extra_skills,
    extra_subagents,
    extra_system_prompt,
    extra_tools,
)


def test_reserved_hooks_are_empty() -> None:
    assert tuple(extra_tools()) == ()
    assert tuple(extra_middleware()) == ()
    assert extra_subagents() is None
    assert extra_skills() is None
    assert extra_backend() is None
    assert extra_interrupt_on() is None
    assert extra_system_prompt("base") == "base"
