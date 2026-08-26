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
"""Raw-mode multiline input for terminal REPL. Zero external dependencies.

Unix: uses termios/tty for raw mode.
  - Enter (\\r)       → submit
  - Ctrl+J (\\n)      → insert newline (portable, works in all terminals)
  - Alt+Enter         → insert newline (terminal-dependent)
  - Backspace / Del   → delete previous character
  - Ctrl+C            → KeyboardInterrupt

Windows: falls back to plain input() with blank-line submit.
"""

import sys


def multiline_input(prompt: str = "> ") -> str:
    """Read multiline input from the terminal."""
    if sys.platform == "win32":
        return _windows_multiline(prompt)
    return _unix_multiline(prompt)


def _windows_multiline(prompt: str) -> str:
    lines: list[str] = []
    first = True
    while True:
        line = input(prompt if first else "")
        first = False
        if line == "":
            break
        lines.append(line)
    return "\n".join(lines)


# ── Unix raw-mode implementation ──────────────────────────────────────────


def _redraw(buf: list[str]) -> None:
    """Clear and redraw buffer content from the saved cursor position.

    Relies on DECSC/DECRC (\\x1b7/\\x1b8) set by _unix_multiline.
    """
    # Raw mode: \n only moves cursor down, not to column 0 — must use \r\n
    content = "".join(buf).replace("\n", "\r\n")
    sys.stdout.write("\x1b8\x1b[0J" + content)  # Restore cursor, clear to end, redraw
    sys.stdout.flush()


def _unix_multiline(prompt: str) -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write(prompt + "\x1b7")  # DECSC: save cursor position right after prompt
        sys.stdout.flush()
        buf: list[str] = []
        while True:
            ch = sys.stdin.read(1)
            if not ch:
                continue
            if ch == "\x1b":
                nxt = _read_with_timeout(fd, timeout=0.05)
                if nxt in ("\r", "\n"):
                    # Alt+Enter → insert newline (terminal-dependent)
                    buf.append("\n")
                    sys.stdout.write("\r\n")
                elif nxt == "[":
                    _drain_csi(fd)
            elif ch == "\n":
                # Ctrl+J → insert newline (portable)
                buf.append("\n")
                sys.stdout.write("\r\n")
            elif ch == "\r":
                # Enter → submit
                sys.stdout.write("\r\n")
                break
            elif ch == "\x03":
                sys.stdout.write("\r\n")
                raise KeyboardInterrupt
            elif ch in ("\x7f", "\x08"):
                if buf:
                    buf.pop()
                    _redraw(buf)
            elif ch.isprintable():
                buf.append(ch)
                sys.stdout.write(ch)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return "".join(buf)


def _read_with_timeout(fd: int, timeout: float) -> str:
    """Read available bytes after ESC with a short timeout."""
    import select

    result: list[str] = []
    while True:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            break
        ch = sys.stdin.read(1)
        if not ch:
            break
        result.append(ch)
        timeout = 0.01
    return "".join(result)


def _drain_csi(fd: int) -> None:
    """Drain a CSI sequence (e.g. arrow keys) from stdin."""
    while True:
        ch = _read_with_timeout(fd, 0.01)
        if not ch:
            break
        if ch.isalpha() or ch in ("~",):
            break
