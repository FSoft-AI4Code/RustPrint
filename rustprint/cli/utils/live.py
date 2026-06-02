import shutil
import sys
import threading

import click

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_CRAB = [
    r"   __     __",
    r"  /  \~~~/  \ ",
    r" ( () . . () )",
    r"  \___'-'___/",
]

_RUST = [
    r" ____   _   _  ____   _____ ",
    r"|  _ \ | | | |/ ___| |_   _|",
    r"| |_) || | | |\___ \   | |  ",
    r"|  _ < | |_| | ___) |  | |  ",
    r"|_| \_\ \___/ |____/   |_|  ",
]

_PRINT = [
    r" ____   ____   ___  _   _  _____ ",
    r"|  _ \ |  _ \ |_ _|| \ | ||_   _|",
    r"| |_) || |_) | | | |  \| |  | |  ",
    r"|  __/ |  _ < | | || |\  |  | |  ",
    r"|_|    |_| \_\|___||_| \_|  |_|  ",
]


def banner() -> str:
    crab_w = max(len(c) for c in _CRAB)
    blank = " " * crab_w
    rows_n = len(_RUST)
    top_pad = max(0, (rows_n - len(_CRAB)) // 2)
    crab_col = [blank] * rows_n
    for i, c in enumerate(_CRAB):
        if top_pad + i < rows_n:
            crab_col[top_pad + i] = c.ljust(crab_w)

    rust_w = max(len(r) for r in _RUST)
    print_w = max(len(p) for p in _PRINT)
    sep = "   "

    rows: list[tuple[str, str]] = []
    for i in range(5):
        left = _RUST[i].ljust(rust_w)
        right = _PRINT[i].ljust(print_w)
        plain = crab_col[i] + sep + left + right
        colored = (
            click.style(crab_col[i], fg="bright_red", bold=True)
            + sep
            + click.style(left, fg="bright_red", bold=True)
            + click.style(right, fg="bright_yellow", bold=True)
        )
        rows.append((plain, colored))

    logo_w = max(len(plain) for plain, _ in rows)
    tagline = "Documentation as Blueprint for Rust Translation"
    left = max(0, (logo_w - len(tagline)) // 2)
    rows.append(("", ""))
    rows.append((" " * left + tagline, " " * left + click.style(tagline, fg="bright_black")))

    inner = max(len(plain) for plain, _ in rows)
    bar = lambda text: click.style(text, fg="bright_red", bold=True)
    width = inner + 2

    top = bar("╔" + "═" * width + "╗")
    rule = bar("╟" + "─" * width + "╢")
    bottom = bar("╚" + "═" * width + "╝")

    lines = ["", top]
    for idx, (plain, colored) in enumerate(rows):
        if plain == "":
            lines.append(rule)
            continue
        pad = " " * (inner - len(plain))
        lines.append(bar("║") + " " + colored + pad + " " + bar("║"))
    lines.append(bottom)
    lines.append("")
    return "\n".join(lines)


_ICONS = {
    "pending": ("○", "bright_black"),
    "active": (None, "cyan"),
    "done": ("✓", "green"),
    "skipped": ("⊘", "yellow"),
    "error": ("✗", "red"),
}


def _truncate(text: str, width: int) -> str:
    if width <= 1:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


class StdoutRedirector:
    def __init__(self, sink):
        self.sink = sink
        self._buffer = ""

    def write(self, chunk: str) -> int:
        if not isinstance(chunk, str):
            chunk = str(chunk)
        self._buffer += chunk
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            stripped = line.rstrip()
            if stripped:
                self.sink(stripped)
        return len(chunk)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


class PhaseStream:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.tick = 0
        self._active = None
        self._lock = threading.Lock()
        try:
            self.tty = bool(self.stream.isatty())
        except Exception:
            self.tty = False

    def _spinner(self) -> str:
        return _SPINNER[self.tick % len(_SPINNER)]

    def _active_text(self) -> str:
        label, detail = self._active
        text = click.style(self._spinner(), fg="cyan", bold=True) + " " + click.style(label, bold=True)
        if detail:
            text += click.style("  — " + detail, fg="bright_black")
        return text

    def _redraw(self) -> None:
        if self._active is not None:
            self.stream.write("\r\x1b[K" + self._active_text())
            self.stream.flush()

    def _clear(self) -> None:
        if self._active is not None:
            self.stream.write("\r\x1b[K")

    def line(self, text: str) -> None:
        with self._lock:
            if not self.tty:
                self.stream.write(text + "\n")
                self.stream.flush()
                return
            self._clear()
            self.stream.write(text + "\n")
            self._redraw()

    def set_active(self, label: str, detail: str = "") -> None:
        with self._lock:
            if not self.tty:
                self.stream.write(click.style("● ", fg="cyan", bold=True) + click.style(label, bold=True) + "\n")
                self.stream.flush()
                return
            self._clear()
            self._active = (label, detail)
            self._redraw()

    def update_detail(self, detail: str) -> None:
        with self._lock:
            if not self.tty or self._active is None:
                return
            self._active = (self._active[0], detail)
            self._redraw()

    def finish(self, icon: str, color: str, label: str) -> None:
        with self._lock:
            text = click.style(icon, fg=color, bold=True) + " " + click.style(label)
            if not self.tty:
                self.stream.write(text + "\n")
                self.stream.flush()
                return
            self._clear()
            self._active = None
            self.stream.write(text + "\n")
            self.stream.flush()

    def animate(self) -> None:
        with self._lock:
            if self._active is None:
                return
            self.tick += 1
            self._redraw()

    def close(self) -> None:
        with self._lock:
            if self.tty and self._active is not None:
                self.stream.write("\n")
                self.stream.flush()
            self._active = None


class LiveConsole:
    def __init__(self, max_logs: int = 10, stream=None):
        self.stream = stream or sys.stdout
        self.max_logs = max_logs
        self.stages: list[dict] = []
        self.logs: list[str] = []
        self.tick = 0
        self._last_lines = 0
        self._lock = threading.Lock()
        try:
            self.tty = bool(self.stream.isatty())
        except Exception:
            self.tty = False

    def set_stages(self, stages: list[dict]) -> None:
        with self._lock:
            self.stages = list(stages)

    def add_log(self, message: str) -> None:
        with self._lock:
            for line in str(message).splitlines() or [""]:
                self.logs.append(line)
            if len(self.logs) > self.max_logs:
                self.logs = self.logs[-self.max_logs:]

    def note(self, message: str, dim: bool = False) -> None:
        with self._lock:
            for line in str(message).splitlines() or [""]:
                text = click.style(line, fg="bright_black") if dim else line
                self.stream.write(text + "\n")
            self.stream.flush()

    def _spinner(self) -> str:
        return _SPINNER[self.tick % len(_SPINNER)]

    def _build(self) -> list[str]:
        size = shutil.get_terminal_size((100, 24))
        width = max(20, size.columns)
        max_rows = max(8, size.lines - 1)

        out = [click.style("Stages", fg="yellow", bold=True)]
        for stage in self.stages:
            status = stage.get("status", "pending")
            icon, color = _ICONS.get(status, ("○", "bright_black"))
            if icon is None:
                icon = self._spinner()
            label = stage.get("label", stage.get("id", ""))
            detail = stage.get("detail") or ""
            body = label + (f" — {detail}" if detail else "")
            body = _truncate(body, width - 4)
            out.append("  " + click.style(icon, fg=color, bold=True) + " " + body)
            if status != "active":
                continue
            for child in stage.get("children", []):
                cstatus = child.get("status", "pending")
                cicon, ccolor = _ICONS.get(cstatus, ("○", "bright_black"))
                if cicon is None:
                    cicon = self._spinner()
                clabel = child.get("label", child.get("id", ""))
                cdetail = child.get("detail") or ""
                cbody = clabel + (f" — {cdetail}" if cdetail else "")
                cbody = _truncate(cbody, width - 8)
                out.append("      " + click.style(cicon, fg=ccolor, bold=True) + " " + cbody)

        out.append("")
        out.append(click.style("Recent logs", fg="yellow", bold=True))
        available = max(1, max_rows - len(out))
        rows = self.logs if self.logs else ["(waiting for output...)"]
        for raw in rows[-available:]:
            out.append("  " + click.style(_truncate(raw, width - 2), fg="bright_black"))

        if len(out) > max_rows:
            out = out[:max_rows]
        return out

    def render(self) -> None:
        if not self.tty:
            return
        with self._lock:
            self.tick += 1
            lines = self._build()
            chunks = []
            if self._last_lines:
                chunks.append(f"\x1b[{self._last_lines}A")
                chunks.append("\x1b[0J")
            chunks.append("\n".join(lines))
            chunks.append("\n")
            self.stream.write("".join(chunks))
            self.stream.flush()
            self._last_lines = len(lines)
