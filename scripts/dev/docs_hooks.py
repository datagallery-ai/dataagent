"""Keep repository-relative source links usable on the built documentation site."""

import re
from pathlib import Path


def on_page_markdown(markdown, page, config, **kwargs):
    docs = Path(config["docs_dir"]).resolve()
    root = docs.parent
    source = Path(page.file.abs_src_path)
    source_tree_url = config["extra"]["source_tree_url"].rstrip("/")

    def replace(match):
        target, separator, anchor = match[1].partition("#")
        if not target or ":" in target or target.startswith("/"):
            return match[0]
        destination = (source.parent / target).resolve()
        if destination.is_relative_to(docs) or not destination.is_relative_to(root):
            return match[0]
        url = source_tree_url + "/" + destination.relative_to(root).as_posix()
        return "](" + url + separator + anchor + ")"

    return re.sub(r"\]\(([^)]+)\)", replace, markdown)
