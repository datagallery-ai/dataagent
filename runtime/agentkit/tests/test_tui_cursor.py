"""PTY probe for the locked Ink cursor contract; uses only the standard library.

The tracker only implements cursor sequences emitted by this fixture. It is not
an IME emulator. The unpatched test must fail its *correct-position* assertion
until Ink fixes the full-height suffix; that is the signal to remove our patch.
"""
import codecs
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CSI = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")


class Cursor:
    def __init__(self, columns=80, rows=24):
        self.columns, self.rows = columns, rows
        self.x = self.y = 0
        self.pending = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")()

    def feed(self, data):
        text = self.pending + self.decoder.decode(data)
        self.pending = ""
        while text:
            if text.startswith("\x1b"):
                match = CSI.match(text)
                if not match:
                    self.pending = text
                    break
                params, _, code = match.groups()
                text = text[match.end():]
                if params.startswith(("?", ">", "<")):
                    continue
                values = [int(value or "1") for value in params.split(";")]
                n = values[0]
                if code == "A": self.y = max(0, self.y - n)
                elif code == "B": self.y = min(self.rows - 1, self.y + n)
                elif code == "C": self.x = min(self.columns - 1, self.x + n)
                elif code == "D": self.x = max(0, self.x - n)
                elif code == "G": self.x = n - 1
                elif code == "E": self.y, self.x = min(self.rows - 1, self.y + n), 0
                elif code in ("H", "f"): self.y, self.x = n - 1, (values[1] if len(values) > 1 else 1) - 1
            else:
                char, text = text[0], text[1:]
                if char == "\r": self.x = 0
                elif char == "\n": self.y = min(self.rows - 1, self.y + 1)
                elif ord(char) >= 32 and not unicodedata.combining(char):
                    if self.x >= self.columns:
                        self.x, self.y = 0, min(self.rows - 1, self.y + 1)
                    self.x += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


@contextmanager
def fixture(*arguments, optimize=False, expected_exit=0):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    environment = {**os.environ, "TERM": "xterm-256color", "NO_COLOR": "1",
                   "DATAFOUNDRY_TUI_OPTIMIZE_ERASE_LINES": "1" if optimize else "0"}
    environment.pop("CI", None)
    environment.pop("FORCE_COLOR", None)
    process = subprocess.Popen(["node", str(REPO / "apps/tui/dist/ui/cursor-pty.fixture.js"), *arguments],
                               stdin=slave, stdout=slave, stderr=slave, cwd=REPO,
                               start_new_session=True, env=environment)
    os.close(slave)
    cursor = Cursor()

    def drain():
        output = b""
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 0.15)[0]:
                if output: break
                continue
            try: chunk = os.read(master, 65536)
            except OSError: break
            if not chunk: break
            output += chunk
            cursor.feed(chunk)
        return output

    def send(data):
        os.write(master, data)
        return drain()

    try:
        output = drain()
        assert b"test-model" in output
        assert not re.search(rb"\x1b\[(?:38|48);", output), "NO_COLOR must suppress palette escapes"
        yield process, master, cursor, send, drain
        if process.poll() is None:
            output = send(b"\x03")
            assert b"WRITER_RESTORED" in output
        assert process.wait(timeout=3) == expected_exit
        assert termios.tcgetattr(master)[3] & termios.ICANON
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)


def test_unpatched_ink_full_height_cursor_probe():
    with fixture("--unpatched") as (_, _, cursor, send, _):
        assert cursor.y == 20, "Ink cursor behavior changed: run the patched tests and remove the workaround if upstream is fixed"
        send(b"abc")
        assert cursor.y != 21  # Input is on zero-based row 21.


@pytest.mark.parametrize("standard,optimize", [(False, False), (True, False), (True, True)])
def test_fullscreen_cursor_frames_movement_resize_and_remount(standard, optimize):
    with fixture(*(["--standard"] if standard else []), optimize=optimize) as (process, master, cursor, send, drain):
        assert (cursor.x, cursor.y) == (5, 21)
        send(b"abcdef")
        assert (cursor.x, cursor.y) == (11, 21)
        send(b"\x1b[D")
        output = send(b"\x1b[D")
        assert b"abcdef" not in output, "Second left-arrow should be a cursor-only frame"
        assert (cursor.x, cursor.y) == (9, 21)
        send(b"\x1b[13;2u")
        assert (cursor.x, cursor.y) == (5, 21)
        for columns, rows in [(60, 24), (160, 40), (80, 24)]:
            cursor.columns, cursor.rows = columns, rows
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            os.killpg(process.pid, signal.SIGWINCH)
            drain()
            assert (cursor.x, cursor.y) == (5, rows - 3)
        for _ in range(2):
            send(b"\x12")  # Remount composer; must never install two active corrections.
            assert (cursor.x, cursor.y) == (5, 21)
            send("中文，e\u0301".encode())
            assert (cursor.x, cursor.y) == (12, 21)
        send(b"\x12")
        send(("a" * 70 + "中👨‍👩‍👧‍👦e\u0301").encode())
        assert (cursor.x, cursor.y) == (10, 21), "Wide glyphs must wrap as intact graphemes"
        for expected in [(9, 21), (7, 21), (5, 21), (74, 20)]:
            send(b"\x1b[D")
            assert (cursor.x, cursor.y) == expected
        send(b"\x1b[C")
        assert (cursor.x, cursor.y) == (5, 21)


def test_render_exception_restores_raw_mode_screen_and_writer():
    with fixture(expected_exit=1, optimize=True) as (process, _, _, send, drain):
        output = send(b"\x18")
        process.wait(timeout=3)
        output += drain()
        assert b"Intentional cursor fixture failure" in output
        assert b"WRITER_RESTORED" in output
        assert b"\x1b[?1049l" in output
        assert b"\x1b[?25h" in output
