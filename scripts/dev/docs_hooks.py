"""Preserve published routes and repository source links in MkDocs."""

import re
from html import escape
from pathlib import Path

from mkdocs.structure.files import File
from mkdocs.utils import get_relative_url

PUBLISHED_PREFIXES = {
    "development/architecture/": "architecture/",
    "development/reference/": "reference/",
}

LEGACY_PREFIXES = {
    "guides/integrate/extensions/": "extensions/",
    "guides/compatibility/": "guides/",
}

LEGACY_PATHS = {
    "guides/integrate/index.md": "python-guide.md",
    "guides/integrate/sdk.md": "guides/sdk.md",
    "guides/configuration.md": "guides/configuration.md",
    "guides/sessions-and-outputs.md": "guides/sessions-and-outputs.md",
    "guides/troubleshooting.md": "guides/troubleshooting.md",
    **{
        f"reference/{name}.md": f"reference/{name}.md"
        for name in (
            "a2a",
            "cli-and-environment",
            "configuration",
            "events",
            "extension-schema",
            "mcp-configuration",
            "python-api",
        )
    },
}

PUBLISHED_PATHS = {}
for language in ("en", "zh"):
    PUBLISHED_PREFIXES[f"{language}/concepts/"] = f"{language}/"
    PUBLISHED_PATHS[f"{language}/concepts/architecture.md"] = f"{language}/architecture/overview.md"
    for name, published_name in {
        "deploy-mirrors": "mirror-deployment",
        "deploy-packages": "dataagent-package",
        "deploy-source": "source-deployment",
        "inputs": "data-sources",
        "web": "web-workbench",
    }.items():
        PUBLISHED_PATHS[f"{language}/guides/{name}.md"] = f"{language}/guides/{published_name}.md"


def mapped_path(source, paths, prefixes):
    if source in paths:
        return paths[source]
    for prefix, replacement in prefixes.items():
        if source.startswith(prefix):
            return replacement + source.removeprefix(prefix)
    return None


def redirect_html(target):
    target = escape(target, quote=True)
    return f"""<!doctype html>
<html lang="zh"><meta charset="utf-8">
<title>文档已移动</title>
<link rel="canonical" href="{target}">
<a id="document-link" href="{target}">打开文档</a>
<script>
  const page = new URL(document.getElementById("document-link").href);
  page.search = location.search;
  page.hash = location.hash;
  location.replace(page);
</script></html>
"""


def on_files(files, config, **kwargs):
    """Publish paired locale URLs and forward former shared URLs to the Chinese pages."""

    def output_for(path):
        return File(path, config["docs_dir"], config["site_dir"], config["use_directory_urls"])

    for file in list(files.documentation_pages()):
        source = file.src_uri
        if published := mapped_path(source, PUBLISHED_PATHS, PUBLISHED_PREFIXES):
            file.dest_uri = output_for(published).dest_uri
        if source.startswith("zh/"):
            relative_path = source.removeprefix("zh/")
            if legacy := mapped_path(relative_path, LEGACY_PATHS, LEGACY_PREFIXES):
                old = output_for(legacy)
                target = get_relative_url(file.url, old.url)
                files.append(File.generated(config, old.dest_uri, content=redirect_html(target)))
    return files


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
