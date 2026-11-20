#!/usr/bin/env python3
"""Curses menu for Herdr's saved SSH machines.

Runs inside a Herdr plugin popup. Every change goes through the herdr CLI
(`herdr machine ...`); see machines_core.py for the testable logic.
"""

from __future__ import annotations

import argparse
import curses
import locale
import os
import queue
import shlex
import subprocess
import sys
import threading

import machines_core as core

MIN_W = 30
MIN_H = 8
STATUS_TIMEOUT = 180
ACTION_TIMEOUT = 120

KEY_ESC = "\x1b"
KEY_ENTER = ("\n", "\r", curses.KEY_ENTER)
KEY_BACKSPACE = ("\x7f", "\b", curses.KEY_BACKSPACE)

HELP_VARIANTS = [
    "↑↓ wählen  A hinzufügen  E aktivieren  D deaktivieren  Leertaste umschalten"
    "  R neu verbinden  N umbenennen  X entfernen  U auffrischen  ? Hilfe  Q schließen",
    "↑↓ wählen  A hinzu  E an  D aus  Leer umschalten  R verbinden  N umbenennen"
    "  X entfernen  U neu  ? Hilfe  Q schließen",
    "A hinzu E an D aus R verb. N umben. X entf. U neu ? Hilfe Q Ende",
    "A E D R N X U  ? Hilfe  Q Ende",
    "? Hilfe  Q Ende",
]

HELP_KEYS = [
    ("\u2191/\u2193, j/k", "Maschine wählen"),
    ("Bild\u2191/Bild\u2193", "seitenweise blättern"),
    ("A", "Maschine hinzufügen"),
    ("E", "aktivieren"),
    ("D", "deaktivieren"),
    ("Leertaste", "aktiv/inaktiv umschalten"),
    ("R", "neu verbinden"),
    ("N", "umbenennen (nur das Label)"),
    ("X, Entf", "entfernen (mit Rückfrage)"),
    ("U, F5", "Liste und Zustand auffrischen"),
    ("?", "diese Hilfe"),
    ("Esc, Q", "Menü schließen"),
]

HELP_NOTES = [
    "Hinzufügen und Neu verbinden laufen direkt in diesem Fenster, damit Herdr "
    "nachfragen kann (Session-Auswahl, Installation, SSH-Anmeldung).",
    "Deaktivieren behält das Profil, Entfernen vergisst es. In beiden Fällen "
    "laufen die Sitzungen auf dem entfernten Rechner weiter.",
    "SSH-Ziel und Remote-Session eines Profils lassen sich nicht ändern; dafür "
    "die Maschine entfernen und neu hinzufügen.",
]


def help_text(width):
    """Key table that keeps its columns at any width."""
    key_w = max(core.display_width(k) for k, _ in HELP_KEYS) + 2
    lines = []
    for key, desc in HELP_KEYS:
        if key_w + core.display_width(desc) <= width:
            lines.append(core.pad(key, key_w) + desc)
        else:
            lines.append(key)
            lines += ["  " + part for part in core.wrap(desc, max(1, width - 2))]
    for note in HELP_NOTES:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# drawing helpers
# --------------------------------------------------------------------------


def put(win, y, x, text, attr=0, width=None):
    """addstr that never raises at screen edges and truncates by display width."""
    try:
        h, w = win.getmaxyx()
    except curses.error:
        return
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    room = w - x if width is None else min(width, w - x)
    text = core.truncate(text, room)
    if not text:
        return
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        # Writing the bottom right cell moves the cursor off screen; the text is
        # drawn anyway. Any other failure just leaves the cell empty.
        pass


def fill_line(win, y, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h:
        put(win, y, 0, " " * w, attr)


def read_key(win):
    """Return the next key (str or int), or None on timeout."""
    try:
        return win.get_wch()
    except curses.error:
        return None


def sync_size(stdscr):
    """Pick up a terminal size change that happened while curses was paused."""
    try:
        # Ask the tty itself; shutil would prefer stale COLUMNS/LINES.
        cols, rows = os.get_terminal_size(sys.__stdout__.fileno())
        h, w = stdscr.getmaxyx()
        if (rows, cols) != (h, w):
            curses.resizeterm(rows, cols)
    except (curses.error, OSError, ValueError):
        pass


def too_small(win):
    h, w = win.getmaxyx()
    return h < MIN_H or w < MIN_W


def draw_too_small(win):
    win.erase()
    h, w = win.getmaxyx()
    msg = "Fenster zu klein"
    put(win, h // 2, max(0, (w - core.display_width(msg)) // 2), msg, curses.A_BOLD)
    if h > 1:
        put(win, h // 2 + 1, 0, f"mind. {MIN_W}x{MIN_H}, Q schließt")
    win.refresh()


def fit(width, *variants):
    """First variant that fits the width, else the last one."""
    for text in variants:
        if core.display_width(text) <= width:
            return text
    return variants[-1]


def is_key(key, *chars):
    return isinstance(key, str) and key.lower() in chars


# --------------------------------------------------------------------------
# colors
# --------------------------------------------------------------------------

class Colors:
    ok = 0
    off = 0
    error = 0
    attention = 0
    accent = 0

    @classmethod
    def init(cls):
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_GREEN, bg)
            curses.init_pair(2, curses.COLOR_RED, bg)
            curses.init_pair(3, curses.COLOR_YELLOW, bg)
            curses.init_pair(4, curses.COLOR_CYAN, bg)
            cls.ok = curses.color_pair(1)
            cls.error = curses.color_pair(2)
            cls.attention = curses.color_pair(3)
            cls.accent = curses.color_pair(4)
            cls.off = curses.A_DIM
        except curses.error:
            pass

    @classmethod
    def for_kind(cls, kind):
        return {
            "ok": cls.ok,
            "off": cls.off,
            "error": cls.error | curses.A_BOLD,
            "attention": cls.attention | curses.A_BOLD,
            "auth": cls.attention | curses.A_BOLD,
        }.get(kind, 0)


# --------------------------------------------------------------------------
# dialogs
# --------------------------------------------------------------------------


class Box:
    """A centered frame on top of the redrawn menu."""

    def __init__(self, stdscr, want_w, want_h):
        # The box stays between the title line and the message line.
        h, w = stdscr.getmaxyx()
        self.w = max(10, min(want_w, w - 2))
        self.h = max(4, min(want_h, h - 3))
        self.y = max(0, 1 + (h - 3 - self.h) // 2)
        self.x = max(0, (w - self.w) // 2)
        self.scr = stdscr

    def draw(self, title):
        for row in range(self.h):
            put(self.scr, self.y + row, self.x, " " * self.w)
        try:
            win = self.scr.derwin(self.h, self.w, self.y, self.x)
            win.box()
        except curses.error:
            pass
        put(self.scr, self.y, self.x + 2, f" {title} ", curses.A_BOLD, self.w - 4)

    def text(self, row, text, attr=0, col=0):
        if 0 <= row < self.h - 2:
            put(self.scr, self.y + 1 + row, self.x + 2 + col, text, attr, self.w - 4 - col)

    @property
    def inner_w(self):
        return max(1, self.w - 4)

    @property
    def inner_h(self):
        return max(1, self.h - 2)


def dialog_loop(app, render, handle):
    """Generic modal loop: render(stdscr) draws, handle(key) returns
    (done, value). Resize and background updates redraw the whole screen."""
    scr = app.stdscr
    while True:
        if too_small(scr):
            draw_too_small(scr)
        else:
            app.draw(refresh=False)
            render(scr)
            scr.refresh()
        key = read_key(scr)
        if key is None:
            app.poll_status()
            continue
        if key == curses.KEY_RESIZE:
            sync_size(scr)
            continue
        if too_small(scr):
            if key == KEY_ESC or is_key(key, "q"):
                return None
            continue
        done, value = handle(key)
        if done:
            return value


def message_box(app, title, text, attr=0):
    """Scrollable message; dismissed with Enter, Esc or Q.

    text may be a callable taking the available width."""
    state = {"top": 0}

    def content(width):
        return text(width) if callable(text) else text

    def render(scr):
        h, w = scr.getmaxyx()
        first_w = max(10, min(100, w - 6))
        lines = core.wrap(content(first_w), first_w)
        box = Box(scr, max(len(title) + 8, max((core.display_width(l) for l in lines), default=0) + 4), len(lines) + 4)
        lines = core.wrap(content(box.inner_w), box.inner_w)
        view = box.inner_h - 1
        state["max"] = max(0, len(lines) - view)
        state["view"] = view
        state["top"] = min(state["top"], state["max"])
        box.draw(title)
        for i in range(view):
            idx = state["top"] + i
            if idx < len(lines):
                box.text(i, lines[idx], attr)
        if state["max"]:
            pos = f"{state['top'] + 1}-{min(len(lines), state['top'] + view)}/{len(lines)}"
            hint = fit(box.inner_w, f"↑↓ blättern ({pos})  Enter/Esc schließt",
                       f"↑↓ {pos}  Esc schließt", f"↑↓ {pos}")
        else:
            hint = fit(box.inner_w, "Enter/Esc schließt", "Esc schließt")
        box.text(box.inner_h - 1, hint, curses.A_DIM)

    def handle(key):
        if key in KEY_ENTER or key == KEY_ESC or is_key(key, "q", " "):
            return True, None
        step = {curses.KEY_UP: -1, curses.KEY_DOWN: 1,
                curses.KEY_PPAGE: -state.get("view", 1), curses.KEY_NPAGE: state.get("view", 1)}
        if is_key(key, "k"):
            key = curses.KEY_UP
        elif is_key(key, "j"):
            key = curses.KEY_DOWN
        if key in step:
            state["top"] = max(0, min(state.get("max", 0), state["top"] + step[key]))
        elif key == curses.KEY_HOME:
            state["top"] = 0
        elif key == curses.KEY_END:
            state["top"] = state.get("max", 0)
        return False, None

    dialog_loop(app, render, handle)


def confirm(app, title, text):
    """Yes/No question, default No. Returns True only for J."""

    def render(scr):
        h, w = scr.getmaxyx()
        lines = core.wrap(text, max(10, min(70, w - 6)))
        box = Box(scr, max((core.display_width(l) for l in lines), default=20) + 4, len(lines) + 4)
        lines = core.wrap(text, box.inner_w)
        box.draw(title)
        # leave a spacer above the answer line only when there is room for it
        room = box.inner_h - 2 if box.inner_h - 2 >= len(lines) else box.inner_h - 1
        for i, line in enumerate(lines[:room]):
            box.text(i, line)
        box.text(box.inner_h - 1, fit(box.inner_w, "J = ja, N/Esc/Enter = nein", "J ja, N nein"), curses.A_BOLD)

    def handle(key):
        if is_key(key, "j", "y"):
            return True, True
        if is_key(key, "n", "q") or key == KEY_ESC or key in KEY_ENTER:
            return True, False
        return False, None

    return bool(dialog_loop(app, render, handle))


class LineEditor:
    """Single line input with cursor, editing keys and horizontal scrolling."""

    def __init__(self, text=""):
        self.chars = list(text)
        self.pos = len(self.chars)

    @property
    def text(self):
        return "".join(self.chars)

    def set(self, text):
        self.chars = list(text)
        self.pos = len(self.chars)

    def handle(self, key):
        """Return True if the key was an editing key."""
        if key in KEY_BACKSPACE:
            if self.pos > 0:
                self.pos -= 1
                del self.chars[self.pos]
        elif key == curses.KEY_DC:
            if self.pos < len(self.chars):
                del self.chars[self.pos]
        elif key == curses.KEY_LEFT:
            self.pos = max(0, self.pos - 1)
        elif key == curses.KEY_RIGHT:
            self.pos = min(len(self.chars), self.pos + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            self.pos = 0
        elif key in (curses.KEY_END, "\x05"):
            self.pos = len(self.chars)
        elif key == "\x15":  # Ctrl+U
            del self.chars[: self.pos]
            self.pos = 0
        elif key == "\x0b":  # Ctrl+K
            del self.chars[self.pos:]
        elif isinstance(key, str) and key.isprintable() and len(key) == 1:
            self.chars.insert(self.pos, key)
            self.pos += 1
        else:
            return False
        return True

    def render(self, scr, y, x, width, attr=curses.A_UNDERLINE):
        if width <= 1:
            return
        before = "".join(self.chars[: self.pos])
        # keep the cursor visible: drop chars from the left until it fits
        start = 0
        while core.display_width(before[start:]) > width - 1:
            start += 1
        visible = "".join(self.chars[start:])
        put(scr, y, x, core.pad("", width), attr)
        shown = visible
        while core.display_width(shown) > width:
            shown = shown[:-1]
        put(scr, y, x, shown, attr)
        cx = x + core.display_width(before[start:])
        try:
            scr.move(y, min(cx, x + width - 1))
        except curses.error:
            pass


def prompt_line(app, title, label, initial="", hint=""):
    """Ask for one line of text. Returns the text, or None on Esc."""
    editor = LineEditor(initial)

    def render(scr):
        h, w = scr.getmaxyx()
        inner_w = Box(scr, min(70, w - 2), 4).inner_w
        label_lines = core.wrap(label, inner_w)[:2]
        hint_lines = core.wrap(hint, inner_w)[:2] if hint else []
        want = len(label_lines) + 1 + (len(hint_lines) + 1 if hint_lines else 0) + 2 + 2
        box = Box(scr, min(70, w - 2), want)
        box.draw(title)
        # editor and footer always get a row; label and hint use what is left
        row = 0
        for line in label_lines[: max(0, box.inner_h - 2)]:
            box.text(row, line)
            row += 1
        editor_row = row
        footer_row = box.inner_h - 1
        if hint_lines and footer_row - editor_row - 1 >= len(hint_lines) + 1:
            for i, line in enumerate(hint_lines):
                box.text(editor_row + 2 + i, line, curses.A_DIM)
        if footer_row > editor_row:
            box.text(footer_row, fit(box.inner_w, "Enter = übernehmen, Esc = abbrechen", "Enter ok, Esc Abbruch"), curses.A_DIM)
        editor.render(scr, box.y + 1 + editor_row, box.x + 2, box.inner_w)
        try:
            curses.curs_set(1)
        except curses.error:
            pass

    def handle(key):
        if key == KEY_ESC:
            return True, None
        if key in KEY_ENTER:
            return True, editor.text
        editor.handle(key)
        return False, None

    try:
        return dialog_loop(app, render, handle)
    finally:
        hide_cursor()


def hide_cursor():
    try:
        curses.curs_set(0)
    except curses.error:
        pass


def pick_target(app, hosts, warnings):
    """Choose an ssh target: hosts from the ssh config filtered by typing,
    or the typed text itself. Returns the target or None on Esc."""
    editor = LineEditor("")
    state = {"sel": 0, "top": 0}

    def items():
        text = editor.text.strip()
        matches = [h for h in hosts if h.matches(text)]
        # Matches first, so Enter after filtering takes the highlighted host;
        # the typed text stays available as the last entry.
        result = [("host", h.alias, h) for h in matches]
        if text and not any(h.alias == text for h in matches):
            # Text that already looks like a full target goes first.
            free = ("free", text, None)
            if any(c in text for c in "@:/"):
                result.insert(0, free)
            else:
                result.append(free)
        return result

    def render(scr):
        h, w = scr.getmaxyx()
        box = Box(scr, min(90, w - 2), max(12, len(hosts) + 7 + (1 if warnings else 0)))
        box.draw("Maschine hinzufügen: SSH-Ziel")
        # Small windows drop the intro line, the spacer and the footers first.
        roomy = box.inner_h >= 8
        show_warn = bool(warnings) and box.inner_h >= 9
        edit_row = 1 if roomy else 0
        if roomy:
            box.text(0, fit(box.inner_w, "SSH-Host wählen oder Ziel tippen (user@host, ssh://user@host:2222)",
                            "SSH-Host wählen oder Ziel tippen", "Host wählen/Ziel tippen"))
        put(scr, box.y + 1 + edit_row, box.x + 2, "Ziel: ", curses.A_BOLD)
        list_top = edit_row + (2 if roomy else 1)
        footer = (1 if box.inner_h >= 5 else 0) + (1 if show_warn else 0)
        view = max(1, box.inner_h - list_top - footer)
        entries = items()
        state["sel"] = max(0, min(state["sel"], len(entries) - 1))
        if state["sel"] < state["top"]:
            state["top"] = state["sel"]
        if state["sel"] >= state["top"] + view:
            state["top"] = state["sel"] - view + 1
        state["top"] = max(0, min(state["top"], max(0, len(entries) - view)))
        state["view"] = view
        if not entries:
            msg = "Keine Hosts in der SSH-Konfiguration gefunden. Ziel eintippen." if not hosts else "Kein Treffer."
            box.text(list_top, msg, curses.A_DIM)
        alias_w = min(32, max(10, box.inner_w // 2))
        for i in range(view):
            idx = state["top"] + i
            if idx >= len(entries):
                break
            kind, value, host = entries[idx]
            attr = curses.A_REVERSE if idx == state["sel"] else 0
            if kind == "free":
                line = f"Eingabe verwenden: {value}"
            else:
                line = core.pad(value, alias_w) + "  " + host.describe()
            box.text(list_top + i, core.pad(line, box.inner_w), attr)
        if show_warn:
            box.text(box.inner_h - 2, "Hinweis: " + "; ".join(warnings), Colors.attention)
        if footer:
            box.text(box.inner_h - 1, fit(box.inner_w, "Tippen filtert, ↑↓ wählt, Enter übernimmt, Esc bricht ab",
                                      "↑↓ wählen, Enter ok, Esc Abbruch"), curses.A_DIM)
        editor.render(scr, box.y + 1 + edit_row, box.x + 8, box.inner_w - 6)
        try:
            curses.curs_set(1)
        except curses.error:
            pass

    def handle(key):
        entries = items()
        if key == KEY_ESC:
            return True, None
        if key in KEY_ENTER:
            if entries:
                return True, entries[state["sel"]][1]
            return False, None
        if key == curses.KEY_UP:
            state["sel"] = max(0, state["sel"] - 1)
        elif key == curses.KEY_DOWN:
            state["sel"] = min(max(0, len(entries) - 1), state["sel"] + 1)
        elif key == curses.KEY_PPAGE:
            state["sel"] = max(0, state["sel"] - state.get("view", 1))
        elif key == curses.KEY_NPAGE:
            state["sel"] = min(max(0, len(entries) - 1), state["sel"] + state.get("view", 1))
        elif editor.handle(key):
            state["sel"] = 0
            state["top"] = 0
        return False, None

    try:
        return dialog_loop(app, render, handle)
    finally:
        hide_cursor()


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------


class App:
    def __init__(self, stdscr, herdr):
        self.stdscr = stdscr
        self.herdr = herdr
        self.machines: list[core.Machine] = []
        self.list_error = ""
        self.statuses: dict[str, core.MachineStatus] = {}
        self.status_running = False
        self.status_error = ""
        self.status_gen = 0
        self.status_queue: queue.Queue = queue.Queue()
        self.status_procs = set()
        self.status_lock = threading.Lock()
        self.status_stopped = False
        self.sel = 0
        self.top = 0
        self.message = ""
        self.message_attr = 0

    # ---- data -------------------------------------------------------------

    def selected(self):
        if 0 <= self.sel < len(self.machines):
            return self.machines[self.sel]
        return None

    def set_message(self, text, attr=0):
        self.message = text
        self.message_attr = attr

    def reload(self, check_status=True):
        keep_id = self.selected().id if self.selected() else None
        rc, out, err = core.run_cli(core.argv_list(self.herdr), timeout=ACTION_TIMEOUT)
        if rc != 0:
            detail = (err.strip() or out.strip() or "keine Ausgabe")
            self.list_error = f"Maschinenliste konnte nicht geladen werden (Exit-Code {rc}):\n{detail}"
            self.machines = []
        else:
            try:
                self.machines = core.parse_machine_list(out)
                self.list_error = ""
            except core.ParseError as exc:
                self.machines = []
                self.list_error = f"Maschinenliste konnte nicht gelesen werden:\n{exc}"
        self.sel = 0
        if keep_id is not None:
            for i, m in enumerate(self.machines):
                if m.id == keep_id:
                    self.sel = i
                    break
        self.sel = max(0, min(self.sel, len(self.machines) - 1))
        if check_status and self.machines:
            self.start_status()

    def start_status(self):
        # A newer check supersedes the running one: end it instead of letting
        # ssh probes pile up on repeated refreshes.
        with self.status_lock:
            self.status_gen += 1
            gen = self.status_gen
            for proc in self.status_procs:
                core.signal_cli(proc)
        self.status_running = True
        self.status_error = ""
        argv = core.argv_status(self.herdr)

        def probe():
            # Start under the lock so stop_status never misses a process.
            with self.status_lock:
                if gen != self.status_gen or self.status_stopped:
                    return
                try:
                    proc = core.start_cli(argv)
                except OSError as exc:
                    self.status_queue.put((gen, core.RC_NOT_FOUND, "", f"Konnte {argv[0]!r} nicht starten: {exc}"))
                    return
                self.status_procs.add(proc)
            rc, out, err = core.finish_cli(proc, STATUS_TIMEOUT)
            with self.status_lock:
                self.status_procs.discard(proc)
            self.status_queue.put((gen, rc, out, err))

        threading.Thread(target=probe, daemon=True).start()

    def poll_status(self):
        changed = False
        while True:
            try:
                gen, rc, out, err = self.status_queue.get_nowait()
            except queue.Empty:
                return changed
            if gen != self.status_gen:
                continue
            changed = True
            self.status_running = False
            try:
                self.statuses = core.interpret_status_result(rc, out, err)
                self.status_error = ""
            except core.ParseError as exc:
                self.status_error = str(exc)

    def stop_status(self):
        """Stop running status checks so no child outlives the popup."""
        with self.status_lock:
            self.status_stopped = True
            procs = list(self.status_procs)
        for proc in procs:
            core.kill_cli(proc)

    # ---- drawing ----------------------------------------------------------

    def columns(self, w):
        aktiv = 5
        zustand = 16 if w >= 72 else 11
        session = 14 if w >= 90 else (10 if w >= 56 else 0)
        gaps = 4 if session else 3
        rest = max(8, w - 1 - aktiv - zustand - session - gaps)
        label = max(4, rest * 2 // 5)
        target = max(4, rest - label)
        return label, target, session, aktiv, zustand

    def zustand(self, m):
        st = self.statuses.get(m.id)
        if st is None:
            if self.status_running:
                return "prüfe …", curses.A_DIM
            if self.status_error:
                return "unbekannt", curses.A_DIM
            return "", 0
        text = core.status_label(st.status)
        if self.status_running:
            text += " …"
        return text, Colors.for_kind(core.status_kind(st.status))

    def draw(self, refresh=True):
        scr = self.stdscr
        scr.erase()
        if too_small(scr):
            draw_too_small(scr)
            return
        h, w = scr.getmaxyx()

        title = "Herdr-Maschinen"
        if self.machines:
            title += f" ({len(self.machines)})"
        fill_line(scr, 0, curses.A_REVERSE)
        put(scr, 0, 1, title, curses.A_REVERSE | curses.A_BOLD)
        if self.status_running:
            right = "Zustand: prüfe …"
        elif self.status_error:
            right = "Zustand: Fehler"
        else:
            right = ""
        if right and w > core.display_width(title) + core.display_width(right) + 4:
            put(scr, 0, w - core.display_width(right) - 1, right, curses.A_REVERSE)

        label_w, target_w, session_w, aktiv_w, zustand_w = self.columns(w)
        header = [("Label", label_w), ("Ziel", target_w)]
        if session_w:
            header.append(("Session", session_w))
        header += [("Aktiv", aktiv_w), ("Zustand", zustand_w)]
        x = 1
        for name, cw in header:
            put(scr, 1, x, core.pad(name, cw), curses.A_BOLD | curses.A_UNDERLINE)
            x += cw + 1

        list_top = 2
        list_h = max(1, h - list_top - 2)
        self.list_h = list_h

        if self.list_error:
            for i, line in enumerate(core.wrap(self.list_error, w - 2)[:list_h]):
                put(scr, list_top + i, 1, line, Colors.error | curses.A_BOLD)
        elif not self.machines:
            msg = "Noch keine gespeicherten Maschinen. Mit A fügst du eine hinzu."
            for i, line in enumerate(core.wrap(msg, w - 2)[:list_h]):
                put(scr, list_top + 1 + i, 1, line)
        else:
            if self.sel < self.top:
                self.top = self.sel
            if self.sel >= self.top + list_h:
                self.top = self.sel - list_h + 1
            self.top = max(0, min(self.top, max(0, len(self.machines) - list_h)))
            for row in range(list_h):
                idx = self.top + row
                if idx >= len(self.machines):
                    break
                m = self.machines[idx]
                y = list_top + row
                selected = idx == self.sel
                base = curses.A_REVERSE if selected else (curses.A_DIM if not m.enabled else 0)
                if selected:
                    fill_line(scr, y, base)
                cells = [(m.label, label_w), (m.target, target_w)]
                if session_w:
                    cells.append((m.session or "", session_w))
                cells.append(("ja" if m.enabled else "nein", aktiv_w))
                x = 1
                for text, cw in cells:
                    put(scr, y, x, core.pad(text, cw), base)
                    x += cw + 1
                ztext, zattr = self.zustand(m)
                put(scr, y, x, core.pad(ztext, zustand_w), base | (0 if selected else zattr))

        # status / message line
        msg, attr = self.message, self.message_attr
        if not msg:
            m = self.selected()
            st = self.statuses.get(m.id) if m else None
            if self.status_error:
                msg, attr = "Zustandsprüfung: " + self.status_error.replace("\n", " "), Colors.error
            elif st is not None and st.error:
                msg, attr = f"{m.label}: {st.error}", Colors.for_kind(core.status_kind(st.status))
            elif m is not None:
                msg, attr = f"{m.label}  {m.target}  ID {m.id}", curses.A_DIM
        put(scr, h - 2, 1, msg, attr)

        help_line = next((v for v in HELP_VARIANTS if core.display_width(v) <= w - 2), HELP_VARIANTS[-1])
        fill_line(scr, h - 1, curses.A_REVERSE)
        put(scr, h - 1, 1, help_line, curses.A_REVERSE)
        if refresh:
            scr.refresh()

    # ---- actions ----------------------------------------------------------

    def run_interactive(self, argv, what):
        """Run argv in the plain terminal so herdr can ask questions."""
        curses.def_prog_mode()
        curses.endwin()
        rc = None
        try:
            print(f"\n{what}", flush=True)
            print(f"Starte: {shlex.join(argv)}\n", flush=True)
            try:
                proc = subprocess.Popen(argv)
            except OSError as exc:
                print(f"Konnte {argv[0]!r} nicht starten: {exc}", flush=True)
                rc = core.RC_NOT_FOUND
            else:
                while True:
                    try:
                        rc = proc.wait()
                        break
                    except KeyboardInterrupt:
                        # Ctrl+C reaches herdr directly; just keep waiting for it.
                        continue
            if rc == 0:
                print("\nFertig: Exit-Code 0 (erfolgreich).", flush=True)
            elif rc is not None and rc < 0:
                print(f"\nAbgebrochen durch Signal {-rc}.", flush=True)
            else:
                print(f"\nFehlgeschlagen: Exit-Code {rc}.", flush=True)
            try:
                input("Enter drücken, um zum Menü zurückzukehren ... ")
            except (EOFError, KeyboardInterrupt):
                print()
        finally:
            curses.reset_prog_mode()
            sync_size(self.stdscr)
            hide_cursor()
            self.stdscr.clear()
        return rc

    def run_captured(self, argv, ok_text):
        self.set_message("Läuft: " + shlex.join(argv[1:]) + " ...")
        self.draw()
        rc, out, err = core.run_cli(argv, timeout=ACTION_TIMEOUT)
        if rc != 0:
            parts = [f"Befehl: {shlex.join(argv)}", f"Exit-Code: {rc}"]
            if err.strip():
                parts += ["", err.strip()]
            if out.strip():
                parts += ["", "Ausgabe:", out.strip()]
            if not err.strip() and not out.strip():
                parts += ["", "(keine Ausgabe)"]
            self.reload()
            message_box(self, "Fehler", "\n".join(parts), Colors.error)
            self.set_message(f"Fehlgeschlagen (Exit-Code {rc}): {shlex.join(argv[1:])}", Colors.error)
            return False
        text = (out.strip() or err.strip()).splitlines()
        self.set_message(text[-1] if text else ok_text, Colors.ok)
        self.reload()
        return True

    def require_selection(self):
        m = self.selected()
        if m is None:
            self.set_message("Keine Maschine gewählt.", Colors.attention)
        return m

    def action_enable(self, enable):
        m = self.require_selection()
        if m is None:
            return
        if enable:
            self.run_captured(core.argv_enable(self.herdr, m.id), f"{m.label} aktiviert.")
        else:
            self.run_captured(core.argv_disable(self.herdr, m.id), f"{m.label} deaktiviert.")

    def action_toggle(self):
        m = self.require_selection()
        if m is not None:
            self.action_enable(not m.enabled)

    def action_rename(self):
        m = self.require_selection()
        if m is None:
            return
        new = prompt_line(self, "Umbenennen", f"Neues Label für {m.label} ({m.target}):", m.label)
        if new is None:
            self.set_message("Umbenennen abgebrochen.")
            return
        new = new.strip()
        if not new:
            self.set_message("Ein leeres Label ist nicht möglich.", Colors.attention)
            return
        if new == m.label:
            self.set_message("Label unverändert.")
            return
        try:
            argv = core.argv_rename(self.herdr, m.id, new)
        except ValueError as exc:
            message_box(self, "Fehler", str(exc), Colors.error)
            return
        self.run_captured(argv, f"Umbenannt in {new}.")

    def action_remove(self):
        m = self.require_selection()
        if m is None:
            return
        text = (
            f"Maschine „{m.label}“ ({m.target}) entfernen?\n\n"
            "Herdr vergisst nur das Profil. Sitzungen und Programme auf dem "
            "entfernten Rechner laufen weiter."
        )
        if not confirm(self, "Entfernen", text):
            self.set_message("Entfernen abgebrochen.")
            return
        self.run_captured(core.argv_remove(self.herdr, m.id), f"{m.label} entfernt.")

    def action_reconnect(self):
        m = self.require_selection()
        if m is None:
            return
        rc = self.run_interactive(core.argv_reconnect(self.herdr, m.id), f"Neu verbinden: {m.label} ({m.target})")
        self.set_message(
            f"Neu verbinden {m.label}: Exit-Code {rc}" + (" (erfolgreich)" if rc == 0 else ""),
            Colors.ok if rc == 0 else Colors.error,
        )
        self.reload()

    def action_add(self):
        """Returns True if herdr machine add was started."""
        cfg, ssh_dir = core.default_ssh_config()
        hosts, warnings = core.load_ssh_hosts(cfg, ssh_dir)
        target = pick_target(self, hosts, warnings)
        if target is None or not target.strip():
            self.set_message("Hinzufügen abgebrochen.")
            return False
        target = target.strip()
        label = prompt_line(
            self, "Maschine hinzufügen: Label",
            f"Label für {target}:", "",
            "Leer lassen für den Herdr-Standard (Hostname bzw. Host/Session).",
        )
        if label is None:
            self.set_message("Hinzufügen abgebrochen.")
            return False
        session = prompt_line(
            self, "Maschine hinzufügen: Remote-Session",
            f"Remote-Session auf {target} (optional):", "",
            "Leer lassen: Herdr wählt die laufende Session oder fragt nach.",
        )
        if session is None:
            self.set_message("Hinzufügen abgebrochen.")
            return False
        try:
            argv = core.argv_add(self.herdr, target, label, session)
        except ValueError as exc:
            message_box(self, "Fehler", str(exc), Colors.error)
            return False
        rc = self.run_interactive(argv, f"Maschine hinzufügen: {target}")
        self.set_message(
            f"Hinzufügen {target}: Exit-Code {rc}" + (" (erfolgreich)" if rc == 0 else " (nicht gespeichert)"),
            Colors.ok if rc == 0 else Colors.error,
        )
        self.reload()
        return True

    def refresh_all(self):
        self.set_message("Aufgefrischt.")
        self.reload()

    def move(self, delta):
        if self.machines:
            self.sel = max(0, min(len(self.machines) - 1, self.sel + delta))
        self.message = ""

    # ---- main loop --------------------------------------------------------

    def handle_key(self, key):
        """Returns False to quit."""
        if key == KEY_ESC or is_key(key, "q"):
            return False
        if too_small(self.stdscr):
            return True
        page = max(1, getattr(self, "list_h", 1))
        if key == curses.KEY_UP or is_key(key, "k"):
            self.move(-1)
        elif key == curses.KEY_DOWN or is_key(key, "j"):
            self.move(1)
        elif key == curses.KEY_PPAGE:
            self.move(-page)
        elif key == curses.KEY_NPAGE:
            self.move(page)
        elif key == curses.KEY_HOME:
            self.move(-len(self.machines))
        elif key == curses.KEY_END:
            self.move(len(self.machines))
        elif is_key(key, "a"):
            self.message = ""
            self.action_add()
        elif is_key(key, "e"):
            self.action_enable(True)
        elif is_key(key, "d"):
            self.action_enable(False)
        elif key == " ":
            self.action_toggle()
        elif is_key(key, "r"):
            self.action_reconnect()
        elif is_key(key, "n"):
            self.action_rename()
        elif is_key(key, "x") or key == curses.KEY_DC:
            self.action_remove()
        elif is_key(key, "u") or key == curses.KEY_F5:
            self.refresh_all()
        elif is_key(key, "?") or key == curses.KEY_F1:
            message_box(self, "Hilfe", help_text)
        return True

    def run(self, add_mode):
        self.stdscr.timeout(200)
        self.stdscr.keypad(True)
        hide_cursor()
        self.reload()
        if add_mode:
            if not self.action_add():
                return
        while True:
            self.draw()
            key = read_key(self.stdscr)
            if key is None:
                self.poll_status()
                continue
            if key == curses.KEY_RESIZE:
                sync_size(self.stdscr)
                continue
            if not self.handle_key(key):
                return


def _main(stdscr, add_mode):
    try:
        curses.set_escdelay(25)
    except (AttributeError, curses.error):
        pass
    Colors.init()
    app = App(stdscr, core.herdr_bin())
    try:
        app.run(add_mode)
    finally:
        app.stop_status()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Herdr-Maschinenmenü")
    parser.add_argument("--add", action="store_true", help="direkt mit dem Hinzufügen-Dialog starten")
    args = parser.parse_args(argv)
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(_main, args.add)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
