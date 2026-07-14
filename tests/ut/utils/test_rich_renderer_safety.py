import pytest

from dataagent.utils.cli.rich_renderer import RICH_AVAILABLE, StreamRenderer

if RICH_AVAILABLE:
    from rich.console import Console
    from rich.tree import Tree


@pytest.mark.skipif(not RICH_AVAILABLE, reason="rich is not installed")
def test_args_tree_treats_nested_keys_as_plain_text():
    console = Console(record=True, force_terminal=False, width=80)
    renderer = StreamRenderer(console=console)
    tree = Tree("args")

    renderer._add_args_tree(tree, {"[red]danger[/red]": {"value": 1}})
    console.print(tree)

    assert "[red]danger[/red]" in console.export_text()
