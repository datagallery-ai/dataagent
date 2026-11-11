"""Prepare a history SQL snapshot using AgentKit Home/Agent configuration."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from dataagent.bootstrap import LaunchOptions, prepare_runtime
from dataagent.diagnostics import safe_error
from dataagent.history_sql.config import corpus_path, enabled
from dataagent.history_sql.downloader import (
    CorpusDownloadError,
    compare_corpora,
    fetch_all_entities,
    write_corpus,
)
from dataagent.semantic_client import SemanticServiceClient


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="AgentKit Agent JSON configuration")
    parser.add_argument("--output-dir", help="Absolute corpus directory override")
    parser.add_argument("--compare-dir")
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--if-enabled", action="store_true", help="Skip when history_sql is disabled")
    parser.add_argument("--no-lock", action="store_true", help="Do not chmod the prepared snapshot read-only")
    args = parser.parse_args(argv)
    try:
        runtime = prepare_runtime(LaunchOptions(config=args.config))
        options = runtime.settings.plugins.config.get("de-agent", {}).get("history_sql", {})
        if args.if_enabled and not enabled(options):
            print(json.dumps({"status": "disabled", "written": 0}))
            return 0
        if args.output_dir:
            # An explicit command-line location wins over the snapshot environment override.
            destination = Path(args.output_dir).expanduser()
            if not destination.is_absolute():
                raise ValueError("--output-dir must be absolute")
            destination = destination.resolve()
        else:
            destination = corpus_path(options, runtime.paths.home)
        if runtime.settings.semantic is None:
            raise ValueError("semantic configuration is required for downloading history SQL")
    except (ValueError, OSError) as exc:
        print(f"History SQL configuration error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    try:
        with SemanticServiceClient.from_settings(runtime.settings.semantic) as client:
            result = fetch_all_entities(client, page_size=args.page_size)
            stats = write_corpus(
                result.entities, destination, lock=not args.no_lock, source_base_url=client.base_url
            )
        stats.total_fetched = len(result.entities)
        stats.pages = result.pages
        stats.duplicates_skipped = result.duplicates_skipped
        report = {"status": "ready", **asdict(stats)}
        if args.compare_dir:
            report["comparison"] = asdict(compare_corpora(destination, args.compare_dir))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (CorpusDownloadError, OSError) as exc:
        details = safe_error(exc, runtime.redaction_secrets)
        print(f"History SQL download failed [{details['code']}]: {details['message']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
