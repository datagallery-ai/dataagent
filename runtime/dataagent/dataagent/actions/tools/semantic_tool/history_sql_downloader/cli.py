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
"""CLI entry for the history-SQL corpus downloader.

Usage (any cwd, as long as the dataagent package is importable)::

    python -m dataagent.actions.tools.semantic_tool.history_sql_downloader \
        [--config PATH] [--output-dir PATH] [--compare-dir PATH] \
        [--page-size N] [--no-lock] [--if-enabled]

Exit codes: 0 success (incl. --if-enabled skip) / 1 network or write failure / 2 config error.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dataagent.actions.tools.semantic_tool.history_sql_downloader import downloader
from dataagent.utils.runtime_paths import dataagent_package_path

DEFAULT_CONFIG = ("core", "flex", "examples", "deagent_config.yaml")
_SUMMARY_LIST_LIMIT = 20
_COMPARE_LIST_LIMIT = 50


def main(argv: list[str] | None = None) -> int:
    """Run the downloader CLI; returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    page_size = max(1, min(args.page_size, downloader.MAX_PAGE_SIZE))
    try:
        config = downloader.load_agent_config(args.config)
        if args.if_enabled and not downloader.history_sql_enabled_in_config(config):
            print("HISTORY_SQL.enabled=false：历史SQL功能未启用，跳过语料下载（不下载、不删除）")
            return 0
        stats = downloader.downloader_full_corpus(
            config,
            output_dir=args.output_dir,
            page_size=page_size,
            lock=not args.no_lock,
        )
    except downloader.CorpusConfigError as err:
        print(f"配置错误：{err}")
        return 2
    except downloader.CorpusDownloadError as err:
        print(f"下载/写入失败：{err}")
        return 1
    _print_summary(stats)
    if args.compare_dir:
        report = downloader.compare_corpora(Path(stats.output_dir), Path(args.compare_dir))
        _print_compare(report)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dataagent.actions.tools.semantic_tool.history_sql_downloader",
        description=(
            "从语义引擎全量下载历史SQL语料（文件名=大写输出表名.sql；同一表多条记录时全组 _HASH12 后缀；"
            "内容=服务端 expression 原文）。下载后目录锁定只读（目录 555 / 文件 444，--no-lock跳过）"
        ),
    )
    parser.add_argument(
        "--config",
        default=str(dataagent_package_path(*DEFAULT_CONFIG)),
        help="agent config 路径（yaml.safe_load 直读，值须为字面量；缺省用包内 deagent.yaml 示例）",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="语料输出目录；缺省 = env DATAAGENT_HISTORY_SQL_CORPUS > dataagent_home()/history_sql",
    )
    parser.add_argument(
        "--compare-dir",
        default=None,
        help="下载后与该目录做名字级 diff（如手维护语料 DA/history_sql），只读不写",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=downloader.MAX_PAGE_SIZE,
        help=f"分页大小，自动 clamp 到 [1, {downloader.MAX_PAGE_SIZE}]（服务端上限 200，缺省 200）",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="跳过 555/444 只读锁定（scratch 测试用）",
    )
    parser.add_argument(
        "--if-enabled",
        action="store_true",
        help="读 config 的 HISTORY_SQL.enabled：false 时跳过下载直接退出 0（评测钩子等价此旗标）；"
        "默认不读开关、无条件下载（下载是准备工作，与开关解耦）",
    )
    return parser


def _print_summary(stats: downloader.CorpusWriteStats) -> None:
    print(f"拉取记录：{stats.total_fetched} 条（{stats.pages} 页，分页重复跳过 {stats.duplicates_skipped}）")
    print(
        f"落盘文件：{stats.written}（裸名 {stats.bare_names} / 碰撞组后缀 {stats.group_suffixed}"
        f" / 含回退解析 {stats.fallback_named}）"
    )
    print(f"skip：{len(stats.skipped)} 条")
    for guid, reason in stats.skipped[:_SUMMARY_LIST_LIMIT]:
        print(f"  - {reason}（{guid}）")
    if len(stats.skipped) > _SUMMARY_LIST_LIMIT:
        print(f"  ... 其余 {len(stats.skipped) - _SUMMARY_LIST_LIMIT} 条略")
    if stats.pruned:
        print(f"pruned（上次 manifest 中不在本次集合，已删除）：{len(stats.pruned)} 个")
        for name in stats.pruned[:_SUMMARY_LIST_LIMIT]:
            print(f"  - {name}")
    print(f"语料目录：{stats.output_dir}")


def _print_compare(report: downloader.CompareReport) -> None:
    print(
        f"compare：shared {len(report.shared)}（其中内容差异 {len(report.shared_content_diff)}）"
        f" / server-only {len(report.server_only)} / corpus-only {len(report.corpus_only)}"
    )
    for name in report.server_only[:_COMPARE_LIST_LIMIT]:
        print(f"  [server-only] {name}")
    if len(report.server_only) > _COMPARE_LIST_LIMIT:
        print(f"  ... 其余 {len(report.server_only) - _COMPARE_LIST_LIMIT} 个略")
    for name in report.corpus_only[:_COMPARE_LIST_LIMIT]:
        print(f"  [corpus-only] {name}")
    if len(report.corpus_only) > _COMPARE_LIST_LIMIT:
        print(f"  ... 其余 {len(report.corpus_only) - _COMPARE_LIST_LIMIT} 个略")
    for name in report.shared_content_diff[:_COMPARE_LIST_LIMIT]:
        print(f"  [content-diff] {name}")
    if len(report.shared_content_diff) > _COMPARE_LIST_LIMIT:
        print(f"  ... 其余 {len(report.shared_content_diff) - _COMPARE_LIST_LIMIT} 个略")
