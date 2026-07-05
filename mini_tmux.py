#!/usr/bin/env python3
"""A tiny tmux-like multiplexer for Linux hosts without tmux installed."""

from __future__ import annotations

import argparse
import dataclasses
import errno
import json
import os
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


APP = "mini-tmux"
PREFIX_KEY = "\x02"  # Ctrl-b
MAX_HISTORY = 2000


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip())
    return cleaned.strip("-") or "default"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    path = Path(base) / APP if base else Path("/tmp") / f"{APP}-{os.getuid()}"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def session_paths(name: str) -> tuple[Path, Path, Path]:
    slug = safe_name(name)
    root = runtime_dir()
    return root / f"{slug}.sock", root / f"{slug}.json", root / f"{slug}.log"


def send_json(sock: socket.socket, msg: dict[str, Any]) -> None:
    payload = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"
    sock.sendall(payload)


def recv_json_lines(buffer: bytes, chunk: bytes) -> tuple[bytes, list[dict[str, Any]]]:
    buffer += chunk
    messages: list[dict[str, Any]] = []
    while b"\n" in buffer:
        raw, buffer = buffer.split(b"\n", 1)
        if not raw:
            continue
        messages.append(json.loads(raw.decode("utf-8")))
    return buffer, messages


def leaf(pane_id: int) -> dict[str, Any]:
    return {"pane": pane_id}


def is_leaf(node: dict[str, Any]) -> bool:
    return "pane" in node


def split_layout(node: dict[str, Any], target: int, orientation: str, new_pane: int) -> tuple[dict[str, Any], bool]:
    if is_leaf(node):
        if node["pane"] != target:
            return node, False
        return {"split": orientation, "children": [dict(node), leaf(new_pane)]}, True

    changed_children = []
    changed = False
    for child in node["children"]:
        new_child, child_changed = split_layout(child, target, orientation, new_pane)
        changed_children.append(new_child)
        changed = changed or child_changed
    if not changed:
        return node, False
    return {"split": node["split"], "children": changed_children}, True


def remove_from_layout(node: dict[str, Any], pane_id: int) -> tuple[dict[str, Any] | None, bool]:
    if is_leaf(node):
        return (None, True) if node["pane"] == pane_id else (node, False)

    changed = False
    children: list[dict[str, Any]] = []
    for child in node["children"]:
        new_child, removed = remove_from_layout(child, pane_id)
        changed = changed or removed
        if new_child is not None:
            children.append(new_child)

    if not changed:
        return node, False
    if not children:
        return None, True
    if len(children) == 1:
        return children[0], True
    return {"split": node["split"], "children": children}, True


def pane_order(node: dict[str, Any]) -> list[int]:
    if is_leaf(node):
        return [node["pane"]]
    order: list[int] = []
    for child in node["children"]:
        order.extend(pane_order(child))
    return order


def compute_rects(node: dict[str, Any], x: int, y: int, width: int, height: int) -> dict[int, tuple[int, int, int, int]]:
    if is_leaf(node):
        return {node["pane"]: (x, y, max(1, width), max(1, height))}

    children = node["children"]
    rects: dict[int, tuple[int, int, int, int]] = {}
    if node["split"] == "h":
        used = 0
        for index, child in enumerate(children):
            remaining = width - used
            child_width = remaining if index == len(children) - 1 else max(1, width // len(children))
            rects.update(compute_rects(child, x + used, y, child_width, height))
            used += child_width
    else:
        used = 0
        for index, child in enumerate(children):
            remaining = height - used
            child_height = remaining if index == len(children) - 1 else max(1, height // len(children))
            rects.update(compute_rects(child, x, y + used, width, child_height))
            used += child_height
    return rects


def choose_neighbor(rects: dict[int, tuple[int, int, int, int]], current: int, direction: str) -> int | None:
    if current not in rects:
        return None
    cx, cy, cw, ch = rects[current]
    center_x = cx + cw / 2
    center_y = cy + ch / 2
    candidates: list[tuple[float, int]] = []
    for pane_id, (x, y, width, height) in rects.items():
        if pane_id == current:
            continue
        other_x = x + width / 2
        other_y = y + height / 2
        if direction == "left" and other_x < center_x:
            candidates.append((center_x - other_x + abs(center_y - other_y) / 100, pane_id))
        elif direction == "right" and other_x > center_x:
            candidates.append((other_x - center_x + abs(center_y - other_y) / 100, pane_id))
        elif direction == "up" and other_y < center_y:
            candidates.append((center_y - other_y + abs(center_x - other_x) / 100, pane_id))
        elif direction == "down" and other_y > center_y:
            candidates.append((other_y - center_y + abs(center_x - other_x) / 100, pane_id))
    return min(candidates)[1] if candidates else None


def pane_frame(
    rect: tuple[int, int, int, int], total_cols: int, total_rows: int, framed: bool = True
) -> tuple[int, int, int, int, int, int, int, int]:
    x, y, width, height = rect
    total_cols = max(1, total_cols)
    total_rows = max(1, total_rows)
    left = max(0, min(total_cols - 1, x))
    top = max(0, min(total_rows - 1, y))
    right = max(left, min(total_cols - 1, x + width - 1))
    bottom = max(top, min(total_rows - 1, y + height - 1))
    if not framed:
        content_width = max(1, right - left + 1)
        content_height = max(1, bottom - top + 1)
        return left, top, right, bottom, left, top, content_width, content_height
    content_x = min(total_cols - 1, left + (1 if left > 0 else 0))
    content_y = min(total_rows - 1, top + (1 if top > 0 else 0))
    content_width = max(1, right - content_x + 1)
    content_height = max(1, bottom - content_y + 1)
    return left, top, right, bottom, content_x, content_y, content_width, content_height


class TerminalScreen:
    def __init__(self, rows: int = 24, cols: int = 80) -> None:
        self.rows = max(1, rows)
        self.cols = max(1, cols)
        self.primary = self._blank_grid()
        self.alternate = self._blank_grid()
        self.use_alternate = False
        self.cursor_x = 0
        self.cursor_y = 0
        self.saved_cursor = (0, 0)
        self.alternate_saved_cursor = (0, 0)
        self.state = "normal"
        self.escape_buffer = ""

    def _blank_grid(self) -> list[list[str]]:
        return [[" "] * self.cols for _ in range(self.rows)]

    @property
    def grid(self) -> list[list[str]]:
        return self.alternate if self.use_alternate else self.primary

    def resize(self, rows: int, cols: int) -> None:
        rows = max(1, rows)
        cols = max(1, cols)
        if rows == self.rows and cols == self.cols:
            return
        self.primary = self._resize_grid(self.primary, rows, cols)
        self.alternate = self._resize_grid(self.alternate, rows, cols)
        self.rows = rows
        self.cols = cols
        self.cursor_y = min(self.cursor_y, self.rows - 1)
        self.cursor_x = min(self.cursor_x, self.cols - 1)

    def _resize_grid(self, grid: list[list[str]], rows: int, cols: int) -> list[list[str]]:
        resized = []
        for row in grid[:rows]:
            resized.append((row + [" "] * cols)[:cols])
        while len(resized) < rows:
            resized.append([" "] * cols)
        return resized

    def feed(self, data: bytes) -> None:
        text = data.decode("utf-8", errors="replace")
        for char in text:
            self._feed_char(char)

    def _feed_char(self, char: str) -> None:
        if self.state == "normal":
            self._normal(char)
        elif self.state == "esc":
            self._escape(char)
        elif self.state == "csi":
            self.escape_buffer += char
            if "@" <= char <= "~":
                self._csi(self.escape_buffer[:-1], char)
                self.state = "normal"
                self.escape_buffer = ""
        elif self.state == "osc":
            if char == "\x07":
                self.state = "normal"
            elif char == "\x1b":
                self.state = "osc_esc"
        elif self.state == "osc_esc":
            self.state = "normal" if char == "\\" else "osc"
        elif self.state == "charset":
            self.state = "normal"

    def _normal(self, char: str) -> None:
        if char == "\x1b":
            self.state = "esc"
        elif char == "\r":
            self.cursor_x = 0
        elif char == "\n":
            self._linefeed()
        elif char == "\b":
            self.cursor_x = max(0, self.cursor_x - 1)
        elif char == "\t":
            target = min(self.cols - 1, ((self.cursor_x // 8) + 1) * 8)
            while self.cursor_x < target:
                self._put(" ")
        elif ord(char) >= 32 and char != "\x7f":
            self._put(char)

    def _escape(self, char: str) -> None:
        if char == "[":
            self.state = "csi"
            self.escape_buffer = ""
        elif char == "]":
            self.state = "osc"
        elif char in {"(", ")", "*", "+", "-"}:
            self.state = "charset"
        elif char == "7":
            self.saved_cursor = (self.cursor_x, self.cursor_y)
            self.state = "normal"
        elif char == "8":
            self.cursor_x, self.cursor_y = self.saved_cursor
            self._clamp_cursor()
            self.state = "normal"
        elif char == "c":
            self.reset()
            self.state = "normal"
        else:
            self.state = "normal"

    def _put(self, char: str) -> None:
        self.grid[self.cursor_y][self.cursor_x] = char
        if self.cursor_x >= self.cols - 1:
            self.cursor_x = 0
            self._linefeed()
        else:
            self.cursor_x += 1

    def _linefeed(self) -> None:
        if self.cursor_y >= self.rows - 1:
            self.grid.pop(0)
            self.grid.append([" "] * self.cols)
        else:
            self.cursor_y += 1

    def _csi(self, raw: str, final: str) -> None:
        private = raw.startswith("?")
        params = self._params(raw[1:] if private else raw)
        first = params[0] if params else 0
        if final in {"H", "f"}:
            row = (params[0] if len(params) >= 1 and params[0] else 1) - 1
            col = (params[1] if len(params) >= 2 and params[1] else 1) - 1
            self.cursor_y = max(0, min(self.rows - 1, row))
            self.cursor_x = max(0, min(self.cols - 1, col))
        elif final == "A":
            self.cursor_y = max(0, self.cursor_y - max(1, first))
        elif final == "B":
            self.cursor_y = min(self.rows - 1, self.cursor_y + max(1, first))
        elif final == "C":
            self.cursor_x = min(self.cols - 1, self.cursor_x + max(1, first))
        elif final == "D":
            self.cursor_x = max(0, self.cursor_x - max(1, first))
        elif final == "G":
            self.cursor_x = max(0, min(self.cols - 1, max(1, first) - 1))
        elif final == "d":
            self.cursor_y = max(0, min(self.rows - 1, max(1, first) - 1))
        elif final == "J":
            self._erase_display(first)
        elif final == "K":
            self._erase_line(first)
        elif final == "X":
            for offset in range(max(1, first)):
                x = self.cursor_x + offset
                if x < self.cols:
                    self.grid[self.cursor_y][x] = " "
        elif final == "P":
            count = max(1, first)
            row = self.grid[self.cursor_y]
            del row[self.cursor_x : self.cursor_x + count]
            row.extend([" "] * count)
        elif final == "@":
            count = max(1, first)
            row = self.grid[self.cursor_y]
            row[self.cursor_x : self.cursor_x] = [" "] * count
            del row[self.cols :]
        elif private and final in {"h", "l"}:
            self._private_mode(params, final == "h")
        elif final in {"m", "r", "s", "u", "h", "l"}:
            return

    def _params(self, raw: str) -> list[int]:
        values = []
        for part in raw.split(";"):
            if not part:
                values.append(0)
                continue
            digits = re.sub(r"[^0-9]", "", part)
            values.append(int(digits) if digits else 0)
        return values or [0]

    def _erase_display(self, mode: int) -> None:
        if mode == 2:
            self.grid[:] = self._blank_grid()
        elif mode == 1:
            for y in range(0, self.cursor_y + 1):
                end = self.cursor_x + 1 if y == self.cursor_y else self.cols
                for x in range(0, end):
                    self.grid[y][x] = " "
        else:
            for y in range(self.cursor_y, self.rows):
                start = self.cursor_x if y == self.cursor_y else 0
                for x in range(start, self.cols):
                    self.grid[y][x] = " "

    def _erase_line(self, mode: int) -> None:
        if mode == 2:
            start, end = 0, self.cols
        elif mode == 1:
            start, end = 0, self.cursor_x + 1
        else:
            start, end = self.cursor_x, self.cols
        for x in range(start, end):
            self.grid[self.cursor_y][x] = " "

    def _private_mode(self, params: list[int], enabled: bool) -> None:
        if any(param in {47, 1047, 1049} for param in params):
            if enabled:
                self.alternate_saved_cursor = (self.cursor_x, self.cursor_y)
                self.use_alternate = True
                self.alternate = self._blank_grid()
                self.cursor_x = 0
                self.cursor_y = 0
            else:
                self.use_alternate = False
                self.cursor_x, self.cursor_y = self.alternate_saved_cursor
                self._clamp_cursor()

    def reset(self) -> None:
        self.primary = self._blank_grid()
        self.alternate = self._blank_grid()
        self.use_alternate = False
        self.cursor_x = 0
        self.cursor_y = 0
        self.alternate_saved_cursor = (0, 0)

    def _clamp_cursor(self) -> None:
        self.cursor_x = max(0, min(self.cols - 1, self.cursor_x))
        self.cursor_y = max(0, min(self.rows - 1, self.cursor_y))

    def lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.grid]


@dataclasses.dataclass
class Pane:
    pane_id: int
    master_fd: int
    pid: int
    title: str
    screen: TerminalScreen = dataclasses.field(default_factory=TerminalScreen)

    def feed(self, data: bytes) -> None:
        self.screen.feed(data)

    def view(self, height: int, width: int) -> tuple[list[str], tuple[int, int]]:
        self.screen.resize(height, width)
        return self.screen.lines(), (self.screen.cursor_x, self.screen.cursor_y)


@dataclasses.dataclass
class Window:
    window_id: int
    name: str
    layout: dict[str, Any]
    panes: dict[int, Pane]
    focus: int


class SessionServer:
    def __init__(self, name: str) -> None:
        self.name = safe_name(name)
        self.socket_path, self.meta_path, self.log_path = session_paths(self.name)
        self.selector = selectors.DefaultSelector()
        self.clients: dict[socket.socket, dict[str, Any]] = {}
        self.windows: list[Window] = []
        self.active_window = 0
        self.next_pane_id = 1
        self.next_window_id = 1
        self.running = True

    def run(self) -> int:
        self._install_signals()
        self._create_window()
        if self.socket_path.exists():
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        listener.listen()
        listener.setblocking(False)
        self.selector.register(listener, selectors.EVENT_READ, ("listen", None))
        self._write_meta()
        try:
            while self.running:
                for key, _ in self.selector.select(timeout=0.1):
                    kind, extra = key.data
                    if kind == "listen":
                        self._accept(listener)
                    elif kind == "client":
                        self._read_client(key.fileobj)
                    elif kind == "pty":
                        self._read_pty(extra)
        finally:
            self._shutdown(listener)
        return 0

    def _install_signals(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stop())

    def _spawn_pane(self) -> Pane:
        import fcntl
        import pty

        shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
        pid, master = pty.fork()
        if pid == 0:
            os.environ.setdefault("TERM", "xterm-256color")
            try:
                os.execlp(shell, Path(shell).name, "-i")
            except OSError:
                os.execlp("/bin/sh", "sh", "-i")
        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        pane = Pane(self.next_pane_id, master, pid, Path(shell).name)
        self.next_pane_id += 1
        self.selector.register(master, selectors.EVENT_READ, ("pty", pane))
        return pane

    def _create_window(self) -> Window:
        pane = self._spawn_pane()
        window = Window(self.next_window_id, str(self.next_window_id), leaf(pane.pane_id), {pane.pane_id: pane}, pane.pane_id)
        self.next_window_id += 1
        self.windows.append(window)
        self.active_window = len(self.windows) - 1
        return window

    def _accept(self, listener: socket.socket) -> None:
        client, _ = listener.accept()
        client.setblocking(False)
        self.clients[client] = {"buffer": b"", "rows": 24, "cols": 80}
        self.selector.register(client, selectors.EVENT_READ, ("client", None))
        self._send_state(client)
        self._write_meta()

    def _read_client(self, client: socket.socket) -> None:
        try:
            chunk = client.recv(65536)
        except OSError:
            self._drop_client(client)
            return
        if not chunk:
            self._drop_client(client)
            return
        state = self.clients[client]
        try:
            state["buffer"], messages = recv_json_lines(state["buffer"], chunk)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._drop_client(client)
            return
        for msg in messages:
            self._handle_client_msg(client, msg)

    def _handle_client_msg(self, client: socket.socket, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "hello" or msg_type == "resize":
            self.clients[client]["rows"] = int(msg.get("rows", 24))
            self.clients[client]["cols"] = int(msg.get("cols", 80))
            self._resize_active_panes(client)
            self._send_state(client)
        elif msg_type == "input":
            self._write_focus(bytes(msg.get("data", [])))
        elif msg_type == "command":
            self._command(str(msg.get("cmd", "")), msg)
        elif msg_type == "detach":
            self._drop_client(client)
        elif msg_type == "kill_session":
            self.stop()

    def _window(self) -> Window | None:
        return self.windows[self.active_window] if self.windows else None

    def _write_focus(self, data: bytes) -> None:
        window = self._window()
        if not window or window.focus not in window.panes:
            return
        try:
            os.write(window.panes[window.focus].master_fd, data)
        except OSError:
            self._kill_pane(window.focus)

    def _command(self, cmd: str, msg: dict[str, Any]) -> None:
        window = self._window()
        if cmd == "split" and window:
            pane = self._spawn_pane()
            window.panes[pane.pane_id] = pane
            window.layout, _ = split_layout(window.layout, window.focus, str(msg.get("orientation", "h")), pane.pane_id)
            window.focus = pane.pane_id
        elif cmd == "new_window":
            self._create_window()
        elif cmd == "next_window" and self.windows:
            self.active_window = (self.active_window + 1) % len(self.windows)
        elif cmd == "prev_window" and self.windows:
            self.active_window = (self.active_window - 1) % len(self.windows)
        elif cmd == "select_window" and self.windows:
            index = int(msg.get("index", 0))
            if 0 <= index < len(self.windows):
                self.active_window = index
        elif cmd == "focus" and window:
            self._focus(window, str(msg.get("direction", "next")))
        elif cmd == "kill_pane" and window:
            self._kill_pane(window.focus)
        elif cmd == "kill_window":
            self._kill_window(self.active_window)
        elif cmd == "kill_session":
            self.stop()
        self._resize_all_clients()
        self._broadcast_state()
        self._write_meta()

    def _focus(self, window: Window, direction: str) -> None:
        order = pane_order(window.layout)
        if not order:
            return
        if direction in {"next", "tab"}:
            window.focus = order[(order.index(window.focus) + 1) % len(order)]
            return
        if direction == "prev":
            window.focus = order[(order.index(window.focus) - 1) % len(order)]
            return
        rects = compute_rects(window.layout, 0, 0, 80, 24)
        neighbor = choose_neighbor(rects, window.focus, direction)
        if neighbor is not None:
            window.focus = neighbor

    def _kill_pane(self, pane_id: int) -> None:
        window = self._window()
        if not window or pane_id not in window.panes:
            return
        pane = window.panes.pop(pane_id)
        self._terminate_pane(pane)
        new_layout, _ = remove_from_layout(window.layout, pane_id)
        if new_layout is None or not window.panes:
            self._kill_window(self.active_window)
            return
        window.layout = new_layout
        order = pane_order(window.layout)
        window.focus = order[0]

    def _kill_window(self, index: int) -> None:
        if not (0 <= index < len(self.windows)):
            return
        window = self.windows.pop(index)
        for pane in list(window.panes.values()):
            self._terminate_pane(pane)
        if not self.windows:
            self.stop()
            return
        self.active_window = min(index, len(self.windows) - 1)

    def _terminate_pane(self, pane: Pane) -> None:
        try:
            self.selector.unregister(pane.master_fd)
        except Exception:
            pass
        try:
            os.close(pane.master_fd)
        except OSError:
            pass
        try:
            os.killpg(pane.pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pane.pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            os.waitpid(pane.pid, os.WNOHANG)
        except ChildProcessError:
            pass

    def _read_pty(self, pane: Pane) -> None:
        try:
            data = os.read(pane.master_fd, 65536)
        except OSError as exc:
            if exc.errno not in {errno.EIO, errno.EBADF}:
                return
            data = b""
        if not data:
            self._kill_pane(pane.pane_id)
            self._broadcast_state()
            return
        pane.feed(data)
        self._broadcast_state()

    def _resize_active_panes(self, client: socket.socket) -> None:
        import fcntl
        import struct
        import termios

        window = self._window()
        if not window:
            return
        rows = max(3, int(self.clients[client]["rows"]) - 1)
        cols = max(10, int(self.clients[client]["cols"]))
        rects = compute_rects(window.layout, 0, 0, cols, rows)
        framed = len(rects) > 1
        for pane_id, (x, y, width, height) in rects.items():
            pane = window.panes.get(pane_id)
            if not pane:
                continue
            _, _, _, _, _, _, inner_cols, inner_rows = pane_frame((x, y, width, height), cols, rows, framed)
            pane.screen.resize(inner_rows, inner_cols)
            packed = struct.pack("HHHH", inner_rows, inner_cols, 0, 0)
            try:
                fcntl.ioctl(pane.master_fd, termios.TIOCSWINSZ, packed)
            except OSError:
                pass

    def _resize_all_clients(self) -> None:
        for client in list(self.clients):
            self._resize_active_panes(client)

    def _state_for(self, client: socket.socket) -> dict[str, Any]:
        rows = max(3, int(self.clients[client]["rows"]))
        cols = max(10, int(self.clients[client]["cols"]))
        window = self._window()
        windows = [
            {"index": index, "id": win.window_id, "name": win.name, "active": index == self.active_window}
            for index, win in enumerate(self.windows)
        ]
        if not window:
            return {"type": "state", "session": self.name, "windows": windows, "panes": [], "rects": {}, "focus": None}
        rects = compute_rects(window.layout, 0, 0, cols, rows - 1)
        framed = len(rects) > 1
        panes = []
        for pane_id, rect in rects.items():
            pane = window.panes.get(pane_id)
            if not pane:
                continue
            _, _, _, _, _, _, width, height = pane_frame(rect, cols, rows - 1, framed)
            lines, cursor = pane.view(height, width)
            panes.append(
                {
                    "id": pane_id,
                    "title": pane.title,
                    "focused": pane_id == window.focus,
                    "lines": lines,
                    "cursor": cursor,
                }
            )
        return {
            "type": "state",
            "session": self.name,
            "windows": windows,
            "active_window": self.active_window,
            "panes": panes,
            "rects": {str(k): v for k, v in rects.items()},
            "focus": window.focus,
        }

    def _send_state(self, client: socket.socket) -> None:
        try:
            send_json(client, self._state_for(client))
        except OSError:
            self._drop_client(client)

    def _broadcast_state(self) -> None:
        for client in list(self.clients):
            self._send_state(client)

    def _drop_client(self, client: socket.socket) -> None:
        try:
            self.selector.unregister(client)
        except Exception:
            pass
        self.clients.pop(client, None)
        try:
            client.close()
        except OSError:
            pass
        self._write_meta()

    def _write_meta(self) -> None:
        data = {
            "name": self.name,
            "pid": os.getpid(),
            "socket": str(self.socket_path),
            "created": int(time.time()),
            "windows": len(self.windows),
            "panes": sum(len(window.panes) for window in self.windows),
            "clients": len(self.clients),
        }
        self.meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def stop(self) -> None:
        self.running = False

    def _shutdown(self, listener: socket.socket) -> None:
        for client in list(self.clients):
            self._drop_client(client)
        for window in list(self.windows):
            for pane in list(window.panes.values()):
                self._terminate_pane(pane)
        try:
            self.selector.unregister(listener)
        except Exception:
            pass
        listener.close()
        for path in (self.socket_path, self.meta_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def client_attach(name: str) -> int:
    import curses

    sock_path, _, _ = session_paths(name)
    if not sock_path.exists():
        print(f"session not found: {name}", file=sys.stderr)
        return 1

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(sock_path))
    sock.setblocking(False)

    def run(stdscr: Any) -> int:
        curses.curs_set(1)
        curses.noecho()
        curses.raw()
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_GREEN)
        except Exception:
            pass
        stdscr.keypad(True)
        stdscr.nodelay(True)
        buffer = b""
        state: dict[str, Any] | None = None
        prefixed = False
        rows, cols = stdscr.getmaxyx()
        send_json(sock, {"type": "hello", "rows": rows, "cols": cols})
        last_size = (rows, cols)

        while True:
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer, messages = recv_json_lines(buffer, chunk)
                for msg in messages:
                    if msg.get("type") == "state":
                        state = msg
            except BlockingIOError:
                pass
            except OSError:
                break

            new_size = stdscr.getmaxyx()
            if new_size != last_size:
                last_size = new_size
                send_json(sock, {"type": "resize", "rows": new_size[0], "cols": new_size[1]})

            try:
                key = stdscr.get_wch()
            except curses.error:
                key = None
            if key is not None:
                if prefixed:
                    if handle_prefix(sock, key):
                        return 0
                    prefixed = False
                elif key == PREFIX_KEY:
                    prefixed = True
                else:
                    data = key_to_bytes(key, curses)
                    if data:
                        send_json(sock, {"type": "input", "data": list(data)})

            if state:
                draw(stdscr, state, prefixed)
            time.sleep(0.02)
        return 0

    try:
        return curses.wrapper(run)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def key_to_bytes(key: Any, curses_mod: Any) -> bytes:
    special = {
        curses_mod.KEY_UP: b"\x1b[A",
        curses_mod.KEY_DOWN: b"\x1b[B",
        curses_mod.KEY_RIGHT: b"\x1b[C",
        curses_mod.KEY_LEFT: b"\x1b[D",
        curses_mod.KEY_BACKSPACE: b"\x7f",
        curses_mod.KEY_DC: b"\x1b[3~",
        curses_mod.KEY_HOME: b"\x1b[H",
        curses_mod.KEY_END: b"\x1b[F",
    }
    if isinstance(key, int):
        return special.get(key, b"")
    if key == "\n":
        return b"\r"
    return key.encode("utf-8", errors="ignore")


def handle_prefix(sock: socket.socket, key: Any) -> bool:
    mapping = {
        "%": {"cmd": "split", "orientation": "h"},
        '"': {"cmd": "split", "orientation": "v"},
        "c": {"cmd": "new_window"},
        "n": {"cmd": "next_window"},
        "p": {"cmd": "prev_window"},
        "\t": {"cmd": "focus", "direction": "next"},
        "x": {"cmd": "kill_pane"},
        "&": {"cmd": "kill_window"},
        "q": {"cmd": "kill_session"},
        "h": {"cmd": "focus", "direction": "left"},
        "j": {"cmd": "focus", "direction": "down"},
        "k": {"cmd": "focus", "direction": "up"},
        "l": {"cmd": "focus", "direction": "right"},
    }
    if key == "d":
        send_json(sock, {"type": "detach"})
        return True
    if isinstance(key, int):
        direction = {260: "left", 261: "right", 259: "up", 258: "down"}.get(key)
        if direction:
            send_json(sock, {"type": "command", "cmd": "focus", "direction": direction})
        return False
    if isinstance(key, str) and key.isdigit():
        send_json(sock, {"type": "command", "cmd": "select_window", "index": int(key)})
        return False
    command = mapping.get(key)
    if command:
        send_json(sock, {"type": "command", **command})
    return False


def draw(stdscr: Any, state: dict[str, Any], prefixed: bool) -> None:
    import curses

    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    pane_rows = max(1, rows - 1)
    pane_by_id = {pane["id"]: pane for pane in state.get("panes", [])}
    framed = len(state.get("rects", {})) > 1
    for pane_id_text, rect in state.get("rects", {}).items():
        pane_id = int(pane_id_text)
        pane = pane_by_id.get(pane_id)
        if not pane:
            continue
        left, top, right, bottom, content_x, content_y, content_width, content_height = pane_frame(
            tuple(rect), cols, pane_rows, framed
        )
        if content_width <= 0 or content_height <= 0:
            continue
        attr = pane_attr(curses, bool(pane.get("focused")))
        if framed:
            draw_internal_borders(stdscr, tuple(rect), cols, pane_rows, attr)
        for line_index, line in enumerate(pane.get("lines", [])[-content_height:]):
            safe_addstr(stdscr, content_y + line_index, content_x, line[:content_width])
        if pane.get("focused"):
            cursor_x, cursor_y = pane.get("cursor", [0, 0])
            cursor_y = max(0, min(content_height - 1, int(cursor_y))) if content_height else 0
            cursor_x = max(0, min(content_width - 1, int(cursor_x))) if content_width else 0
            try:
                stdscr.move(content_y + cursor_y, content_x + cursor_x)
            except Exception:
                pass

    draw_status(stdscr, state, prefixed, rows, cols)
    stdscr.refresh()


def pane_attr(curses_mod: Any, focused: bool) -> int:
    if focused:
        return curses_mod.A_BOLD
    return curses_mod.A_DIM


def draw_status(stdscr: Any, state: dict[str, Any], prefixed: bool, rows: int, cols: int) -> None:
    import curses

    windows = []
    for window in state.get("windows", []):
        label = f"{window['index']}:{window['name']}"
        windows.append(f" {label}* " if window.get("active") else f" {label} ")
    left = f"[{state.get('session')}] {''.join(windows)}"
    right = "PREFIX" if prefixed else "C-b"
    if state.get("focus") is not None:
        right = f"pane {state.get('focus')} | {right}"
    gap = max(1, cols - len(left) - len(right))
    status = (left + " " * gap + right)[:cols]
    safe_addstr(stdscr, rows - 1, 0, status.ljust(cols), status_attr(curses))


def status_attr(curses_mod: Any) -> int:
    try:
        if curses_mod.has_colors():
            return curses_mod.color_pair(1) | curses_mod.A_BOLD
    except Exception:
        pass
    return curses_mod.A_REVERSE


def draw_internal_borders(
    stdscr: Any, rect: tuple[int, int, int, int], total_cols: int, total_rows: int, attr: int
) -> None:
    import curses

    x, y, width, height = rect
    right = x + width
    bottom = y + height
    if 0 < x < total_cols:
        for row in range(max(0, y), min(total_rows, y + height)):
            safe_addch(stdscr, row, x, curses.ACS_VLINE, attr)
    if 0 < right < total_cols:
        for row in range(max(0, y), min(total_rows, y + height)):
            safe_addch(stdscr, row, right, curses.ACS_VLINE, attr)
    if 0 < y < total_rows:
        for col in range(max(0, x), min(total_cols, x + width)):
            safe_addch(stdscr, y, col, curses.ACS_HLINE, attr)
    if 0 < bottom < total_rows:
        for col in range(max(0, x), min(total_cols, x + width)):
            safe_addch(stdscr, bottom, col, curses.ACS_HLINE, attr)


def safe_addch(stdscr: Any, y: int, x: int, char: Any, attr: int = 0) -> None:
    try:
        stdscr.addch(y, x, char, attr)
    except Exception:
        pass


def safe_addstr(stdscr: Any, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        if text:
            stdscr.addstr(y, x, text, attr)
    except Exception:
        pass


def list_sessions() -> int:
    root = runtime_dir()
    entries = sorted(root.glob("*.json"))
    if not entries:
        print("no sessions")
        return 0
    for meta_file in entries:
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        alive = process_alive(int(meta.get("pid", -1))) and Path(meta.get("socket", "")).exists()
        mark = "running" if alive else "stale"
        print(
            f"{meta.get('name')} ({mark}) "
            f"windows={meta.get('windows', 0)} panes={meta.get('panes', 0)} clients={meta.get('clients', 0)}"
        )
    return 0


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def start_server(name: str) -> int:
    sock_path, _, log_path = session_paths(name)
    if sock_path.exists():
        return 0
    with log_path.open("ab", buffering=0) as log:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_server", name],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            start_new_session=True,
        )
    deadline = time.time() + 5
    while time.time() < deadline:
        if sock_path.exists():
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.connect(str(sock_path))
                probe.close()
                return 0
            except OSError:
                pass
        time.sleep(0.05)
    print(f"server did not start; see {log_path}", file=sys.stderr)
    return 1


def attach_or_error(name: str) -> int:
    return client_attach(safe_name(name))


def kill_session(name: str) -> int:
    sock_path, meta_path, _ = session_paths(name)
    if sock_path.exists():
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(sock_path))
            send_json(sock, {"type": "kill_session"})
            sock.close()
            return 0
        except OSError:
            pass
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            os.kill(int(meta["pid"]), signal.SIGTERM)
            return 0
        except Exception:
            pass
    print(f"session not found: {name}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mini-tmux")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="create a session and attach")
    new.add_argument("-s", "--session", default="default")
    new.add_argument("-d", "--detached", action="store_true")

    attach = sub.add_parser("attach", help="attach to a session")
    attach.add_argument("-t", "--target", default="default")

    sub.add_parser("ls", help="list sessions")

    kill = sub.add_parser("kill-session", help="kill a session")
    kill.add_argument("-t", "--target", default="default")

    server = sub.add_parser("_server")
    server.add_argument("session")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "_server":
        return SessionServer(args.session).run()
    if args.command == "new":
        rc = start_server(args.session)
        if rc or args.detached:
            return rc
        return attach_or_error(args.session)
    if args.command == "attach":
        return attach_or_error(args.target)
    if args.command == "ls":
        return list_sessions()
    if args.command == "kill-session":
        return kill_session(args.target)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
