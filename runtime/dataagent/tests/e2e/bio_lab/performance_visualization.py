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

"""HTML cache visualization helpers for bio_lab performance test artifacts."""

import re
from pathlib import Path
from typing import Any


def _parse_round_dump(file_path: Path) -> list[dict[str, Any]]:
    """Parse a round_X.txt dump file into a list of message dicts.

    Each message dict contains: idx, role, content (truncated), chars, bp_tag,
    cache_control markers, and whether it's an anchor.
    """
    content = file_path.read_text(encoding="utf-8")
    messages = []
    current_msg = None
    in_content = False

    for line in content.splitlines():
        header_match = re.match(r"^--- \[(\d+)\] (\w+)(.*?)---", line)
        if header_match:
            if current_msg:
                messages.append(current_msg)
            idx = int(header_match.group(1))
            role = header_match.group(2)
            header_extra = header_match.group(3).strip()

            current_msg = {
                "idx": idx,
                "role": role,
                "content_preview": "",
                "chars": 0,
                "bp_tag": "",
                "is_anchor": "[anchor]" in header_extra,
                "has_explicit_cc": "[explicit cc]" in header_extra or "[bp" in header_extra,
                "bp_candidate": "[bp" in header_extra,
                "content_lines": [],
            }
            # Extract bp candidate number
            bp_m = re.search(r"\[bp (\d) candidate\]", header_extra)
            if bp_m:
                current_msg["bp_candidate_num"] = int(bp_m.group(1))
            in_content = True
        elif in_content and current_msg is not None:
            if line.startswith("--- ["):
                messages.append(current_msg)
                current_msg = None
                in_content = False
                # Re-parse this line as a new header
                header_match = re.match(r"^--- \[(\d+)\] (\w+)(.*?)---", line)
                if header_match:
                    idx = int(header_match.group(1))
                    role = header_match.group(2)
                    header_extra = header_match.group(3).strip()
                    current_msg = {
                        "idx": idx,
                        "role": role,
                        "content_preview": "",
                        "chars": 0,
                        "bp_tag": "",
                        "is_anchor": "[anchor]" in header_extra,
                        "has_explicit_cc": "[explicit cc]" in header_extra or "[bp" in header_extra,
                        "bp_candidate": "[bp" in header_extra,
                        "content_lines": [],
                    }
                    in_content = True
            else:
                # Check for cache_control marker
                if "⭐ cache_control" in line:
                    current_msg["has_explicit_cc"] = True
                current_msg["content_lines"].append(line)

    if current_msg:
        messages.append(current_msg)

    # Calculate chars and preview
    for msg in messages:
        full_content = "\n".join(msg.get("content_lines", []))
        msg["chars"] = len(full_content)
        # Truncate content for display
        if len(full_content) > 500:
            msg["content_preview"] = full_content[:500] + "\n... (truncated)"
        else:
            msg["content_preview"] = full_content

    return messages


def generate_cache_visualization(session_root: Path) -> Path:
    """Generate a self-contained HTML visualization of cache_control markers and context evolution.

    The HTML file includes:
    1. Per-run, per-round breakpoint allocation with cache_control markers highlighted
    2. Compression detection (message count drops between rounds)
    3. Side-by-side diff of context before/after compression events

    Args:
        session_root: Path to the session root directory.

    Returns:
        Path to the generated HTML file.
    """
    import html as html_lib

    dump_dir = session_root / "workspace" / ".memory" / "context_dump"

    # Collect all runs and rounds
    runs_data: list[dict[str, Any]] = []
    if dump_dir.exists():
        for run_dir in sorted(
            dump_dir.iterdir(),
            key=lambda x: int(x.name.split("_")[1]) if x.is_dir() and x.name.startswith("run_") else 999,
        ):
            if not run_dir.is_dir():
                continue

            run_num = int(run_dir.name.split("_")[1])
            rounds_data: list[dict[str, Any]] = []

            round_files = sorted(
                run_dir.glob("round_*.txt"),
                key=lambda x: int(x.stem.split("_")[1]),
            )

            prev_msg_count = None
            for rf in round_files:
                round_num = int(rf.stem.split("_")[1])

                round_info: dict[str, Any] = {
                    "round": round_num,
                    "file": str(rf.name),
                    "messages": _parse_round_dump(rf),
                    "is_compression": False,
                    "prev_msg_count": prev_msg_count,
                }

                msg_count = len(round_info["messages"])
                if prev_msg_count is not None and msg_count < prev_msg_count:
                    round_info["is_compression"] = True

                prev_msg_count = msg_count
                rounds_data.append(round_info)

            runs_data.append({"run": run_num, "rounds": rounds_data})

    # Build HTML
    html_parts: list[str] = []
    html_parts.append("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cache Control Visualization</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif; background: #1a1a2e; color: #e0e0e0; padding: 20px; }
h1 { color: #00d4ff; margin-bottom: 20px; font-size: 24px; }
h2 { color: #00d4ff; margin: 20px 0 10px; font-size: 18px; border-bottom: 1px solid #333; padding-bottom: 5px; }
h3 { color: #ffb74d; margin: 15px 0 8px; font-size: 15px; }
.summary-card { background: #16213e; border-radius: 8px; padding: 15px; margin-bottom: 15px; border: 1px solid #333; }
.run-tabs { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 15px; }
.run-tab { background: #16213e; border: 1px solid #333; border-radius: 6px; padding: 6px 16px; cursor: pointer; color: #aaa; font-size: 13px; transition: all 0.2s; }
.run-tab:hover { border-color: #00d4ff; color: #00d4ff; }
.run-tab.active { background: #0f3460; border-color: #00d4ff; color: #00d4ff; }
.round-list { max-height: 300px; overflow-y: auto; border: 1px solid #333; border-radius: 6px; margin-bottom: 15px; }
.round-item { padding: 6px 12px; cursor: pointer; border-bottom: 1px solid #222; font-size: 13px; transition: background 0.15s; }
.round-item:hover { background: #1a1a3e; }
.round-item.active { background: #0f3460; }
.round-item.compression { color: #ff5252; }
.round-item.compression::after { content: " ⚡压缩"; font-size: 11px; }
.message-list { max-height: 600px; overflow-y: auto; border: 1px solid #333; border-radius: 6px; }
.message-card { border-bottom: 1px solid #222; padding: 10px 12px; font-size: 13px; }
.message-card.bp { border-left: 4px solid #00e676; }
.message-card.anchor { border-left: 4px solid #ffb74d; }
.message-card.no-bp { border-left: 4px solid #555; }
.message-card.compression-removed { border-left: 4px solid #ff5252; background: rgba(255,82,82,0.05); }
.msg-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px; }
.msg-idx { color: #888; font-size: 12px; }
.msg-role { font-weight: bold; font-size: 12px; padding: 2px 8px; border-radius: 4px; }
.msg-role.SYSTEM { background: #4fc3f7; color: #000; }
.msg-role.HUMAN, .msg-role.USER { background: #81c784; color: #000; }
.msg-role.AI, .msg-role.ASSISTANT { background: #ce93d8; color: #000; }
.msg-role.TOOL { background: #ffcc80; color: #000; }
.msg-bp-tag { font-size: 11px; padding: 2px 6px; border-radius: 4px; }
.msg-bp-tag.bp { background: #00e676; color: #000; }
.msg-bp-tag.no-bp { background: #444; color: #aaa; }
.msg-bp-tag.anchor { background: #ffb74d; color: #000; }
.msg-cc-badge { background: #e91e63; color: #fff; font-size: 10px; padding: 1px 5px; border-radius: 3px; margin-left: 5px; }
.msg-chars { color: #888; font-size: 11px; }
.msg-content { background: #0d1117; border-radius: 4px; padding: 8px; margin-top: 5px; font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 11px; white-space: pre-wrap; word-break: break-all; max-height: 200px; overflow-y: auto; color: #c9d1d9; }
.bp-coverage-bar { background: #222; border-radius: 4px; height: 20px; overflow: hidden; margin: 5px 0; position: relative; }
.bp-coverage-fill { height: 100%; background: linear-gradient(90deg, #00e676, #00d4ff); transition: width 0.3s; }
.bp-coverage-text { position: absolute; top: 0; left: 50%; transform: translateX(-50%); font-size: 11px; line-height: 20px; color: #fff; text-shadow: 0 0 3px #000; }
.archive-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.archive-table th { background: #0f3460; padding: 8px; text-align: left; color: #00d4ff; }
.archive-table td { padding: 6px 8px; border-bottom: 1px solid #222; }
.archive-table tr:hover { background: #1a1a3e; }
.diff-view { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }
.diff-panel { border: 1px solid #333; border-radius: 6px; overflow: hidden; }
.diff-panel-title { background: #0f3460; padding: 6px 10px; font-size: 12px; color: #00d4ff; }
.diff-panel-body { max-height: 400px; overflow-y: auto; padding: 8px; font-family: monospace; font-size: 11px; white-space: pre-wrap; word-break: break-all; }
.diff-removed { color: #ff5252; text-decoration: line-through; }
.diff-added { color: #00e676; }
.grid-2col { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
</style>
</head>
<body>
<h1>Cache Control Breakpoint Visualization</h1>
""")

    # Summary card
    total_runs = len(runs_data)
    total_rounds = sum(len(r["rounds"]) for r in runs_data)
    compression_events = sum(1 for r in runs_data for rd in r["rounds"] if rd.get("is_compression"))

    html_parts.append(f"""
<div class="summary-card">
  <h2>Overview</h2>
  <p>Runs: <b>{total_runs}</b> | Rounds: <b>{total_rounds}</b> | Compression events: <b style="color:#ff5252">{compression_events}</b></p>
</div>
""")

    # Per-run visualization
    html_parts.append("<h2>Breakpoint Allocation per Run/Round</h2>")
    html_parts.append('<div class="run-tabs">')
    for i, run in enumerate(runs_data):
        active = "active" if i == 0 else ""
        html_parts.append(f'<div class="run-tab {active}" onclick="showRun({i})">Run {run["run"]}</div>')
    html_parts.append("</div>")

    for i, run in enumerate(runs_data):
        display = "" if i == 0 else "none"
        html_parts.append(f'<div class="run-content" id="run_{i}" style="display:{display}">')

        # Round list
        html_parts.append("<h3>Rounds (click to view messages)</h3>")
        html_parts.append('<div class="round-list">')
        for j, rd in enumerate(run["rounds"]):
            active_cls = "active" if j == 0 else ""
            comp_cls = "compression" if rd.get("is_compression") else ""
            html_parts.append(
                f'<div class="round-item {active_cls} {comp_cls}" '
                f'onclick="showRound({i},{j})" id="round_tab_{i}_{j}">'
                f"Round {rd['round']} | {len(rd['messages'])} msgs"
                f"</div>"
            )
        html_parts.append("</div>")

        # Round detail containers
        for j, rd in enumerate(run["rounds"]):
            display = "" if j == 0 else "none"
            html_parts.append(f'<div class="round-detail" id="round_{i}_{j}" style="display:{display}">')

            if rd.get("is_compression"):
                html_parts.append(
                    '<div style="color:#ff5252; margin:5px 0;">⚡ Compression detected: message count dropped</div>'
                )

            # Message list
            html_parts.append('<div class="message-list">')
            for msg in rd["messages"]:
                role = msg["role"]
                idx = msg["idx"]
                chars = msg["chars"]
                is_anchor = msg.get("is_anchor", False)
                has_cc = msg.get("has_explicit_cc", False)
                bp_candidate = msg.get("bp_candidate", False)

                # Determine card class
                card_cls = "message-card "
                if bp_candidate:
                    card_cls += "bp"
                elif is_anchor:
                    card_cls += "anchor"
                else:
                    card_cls += "no-bp"

                # BP tag
                bp_tag_text = ""
                bp_tag_cls = "msg-bp-tag no-bp"
                if bp_candidate:
                    bp_num = msg.get("bp_candidate_num", "?")
                    bp_tag_text = f"bp {bp_num} candidate"
                    bp_tag_cls = "msg-bp-tag bp"
                elif is_anchor:
                    bp_tag_text = "anchor"
                    bp_tag_cls = "msg-bp-tag anchor"
                elif has_cc:
                    bp_tag_text = "explicit cc"
                    bp_tag_cls = "msg-bp-tag bp"
                else:
                    bp_tag_text = "no bp"

                # Role badge class
                role_cls = f"msg-role {role}"

                # CC badge
                cc_badge = '<span class="msg-cc-badge">cache_control</span>' if has_cc else ""

                # Content (escaped, truncated for display)
                content_preview = html_lib.escape(msg.get("content_preview", ""))

                html_parts.append(
                    f'<div class="{card_cls}">'
                    f'<div class="msg-header">'
                    f'<span><span class="msg-idx">[{idx}]</span> '
                    f'<span class="{role_cls}">{role}</span> '
                    f'<span class="{bp_tag_cls}">{bp_tag_text}</span>'
                    f"{cc_badge}</span>"
                    f'<span class="msg-chars">{chars:,} chars</span>'
                    f"</div>"
                    f'<div class="msg-content">{content_preview}</div>'
                    f"</div>"
                )
            html_parts.append("</div>")  # message-list
            html_parts.append("</div>")  # round-detail

        html_parts.append("</div>")  # run-content

    html_parts.append("""
<script>
function showRun(idx) {
    document.querySelectorAll('.run-content').forEach(el => el.style.display = 'none');
    document.querySelectorAll('.run-tab').forEach(el => el.classList.remove('active'));
    document.getElementById('run_' + idx).style.display = '';
    event.target.classList.add('active');
}
function showRound(runIdx, roundIdx) {
    document.querySelectorAll('#run_' + runIdx + ' .round-detail').forEach(el => el.style.display = 'none');
    document.querySelectorAll('#run_' + runIdx + ' .round-item').forEach(el => el.classList.remove('active'));
    document.getElementById('round_' + runIdx + '_' + roundIdx).style.display = '';
    document.getElementById('round_tab_' + runIdx + '_' + roundIdx).classList.add('active');
}
</script>
</body>
</html>
""")

    html_content = "\n".join(html_parts)
    html_path = session_root / "workspace" / ".memory" / "cache_visualization.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_content, encoding="utf-8")
    return html_path
