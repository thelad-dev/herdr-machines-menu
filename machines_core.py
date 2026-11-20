"""Pure logic for the Herdr machines menu.

Nothing in here touches curses, so everything can be unit tested:
ssh config parsing, parsing of the herdr machine JSON output, status labels,
argv building for every machine action, and a small CLI runner.
"""

from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import unicodedata
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# herdr binary and argv building
# --------------------------------------------------------------------------

PROFILE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def herdr_bin(environ=None) -> str:
    """Return the herdr binary: HERDR_BIN_PATH if set and not empty, else 'herdr'."""
    env = os.environ if environ is None else environ
    value = env.get("HERDR_BIN_PATH", "")
    return value if value.strip() else "herdr"


def _require_profile_id(profile_id: str) -> str:
    if not isinstance(profile_id, str) or not PROFILE_ID_RE.match(profile_id):
        raise ValueError(
            f"Ungültige Profil-ID {profile_id!r}: erwartet werden 32 Hex-Zeichen."
        )
    return profile_id


def _require_text(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} darf nicht leer sein.")
    if value.startswith("-"):
        raise ValueError(f"{what} darf nicht mit '-' beginnen: {value!r}")
    return value


def argv_list(herdr: str) -> list[str]:
    return [herdr, "machine", "list", "--json"]


def argv_status(herdr: str) -> list[str]:
    return [herdr, "machine", "status", "--json"]


def argv_add(herdr: str, target: str, label=None, remote_session=None) -> list[str]:
    target = _require_text(target, "Das SSH-Ziel").strip()
    argv = [herdr, "machine", "add", target]
    if label is not None and label.strip():
        argv += ["--label", _require_text(label.strip(), "Das Label")]
    if remote_session is not None and remote_session.strip():
        argv += ["--remote-session", _require_text(remote_session.strip(), "Die Remote-Session")]
    return argv


def argv_reconnect(herdr: str, profile_id: str) -> list[str]:
    # reconnect accepts label or id; the menu always passes the id.
    return [herdr, "machine", "reconnect", _require_profile_id(profile_id)]


def argv_enable(herdr: str, profile_id: str) -> list[str]:
    return [herdr, "machine", "enable", _require_profile_id(profile_id)]


def argv_disable(herdr: str, profile_id: str) -> list[str]:
    return [herdr, "machine", "disable", _require_profile_id(profile_id)]


def argv_rename(herdr: str, profile_id: str, label: str) -> list[str]:
    label = _require_text(label, "Das neue Label").strip()
    # Herdr 0.9.5 accepts only "<profile-id> --label <label>", although its
    # --help shows the reverse order.
    return [
        herdr,
        "machine",
        "rename",
        _require_profile_id(profile_id),
        "--label",
        label,
    ]


def argv_remove(herdr: str, profile_id: str) -> list[str]:
    return [herdr, "machine", "remove", _require_profile_id(profile_id)]


# --------------------------------------------------------------------------
# CLI runner
# --------------------------------------------------------------------------

RC_NOT_FOUND = 127
RC_TIMEOUT = 124


def start_cli(argv: list[str]) -> subprocess.Popen:
    """Start a noninteractive CLI call with captured output and no stdin.

    The call gets its own process group, so a timeout or stop also reaches
    the ssh children herdr starts.
    """
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )


def signal_cli(proc: subprocess.Popen, sig=signal.SIGTERM) -> None:
    """Send sig to the whole process group of a call started by start_cli."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def kill_cli(proc: subprocess.Popen, grace: float = 2.0) -> None:
    """Terminate the process group, escalating to SIGKILL after grace seconds."""
    signal_cli(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    signal_cli(proc, signal.SIGKILL)


def finish_cli(proc: subprocess.Popen, timeout=None) -> tuple[int, str, str]:
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_cli(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A child escaped the group and holds the pipes open: give up on
            # the rest of the output instead of hanging.
            out, err = "", ""
        err = (err or "") + f"\nZeitüberschreitung nach {timeout} Sekunden, Befehl abgebrochen."
        return RC_TIMEOUT, out or "", err
    return proc.returncode, out or "", err or ""


def run_cli(argv: list[str], timeout=None) -> tuple[int, str, str]:
    """Run argv and return (rc, stdout, stderr).

    A missing binary or any other start failure is reported as rc 127 with the
    reason in stderr, so the caller can show it instead of crashing.
    """
    try:
        proc = start_cli(argv)
    except OSError as exc:
        return RC_NOT_FOUND, "", f"Konnte {argv[0]!r} nicht starten: {exc}"
    return finish_cli(proc, timeout)


# --------------------------------------------------------------------------
# JSON parsing
# --------------------------------------------------------------------------


class ParseError(Exception):
    """The herdr output could not be understood."""


@dataclass
class Machine:
    id: str
    label: str
    target: str
    session: str | None
    enabled: bool
    selected: bool = False


@dataclass
class MachineStatus:
    id: str
    label: str
    status: str
    error: str | None = None


def _load_json_array(text: str, what: str) -> list:
    if text is None or not text.strip():
        raise ParseError(f"{what}: keine Ausgabe erhalten (erwartet wurde ein JSON-Array).")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"{what}: ungültiges JSON ({exc}).") from exc
    if not isinstance(data, list):
        raise ParseError(f"{what}: erwartet wurde ein JSON-Array, erhalten {type(data).__name__}.")
    return data


def _opt_str(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def parse_machine_list(text: str) -> list[Machine]:
    data = _load_json_array(text, "machine list")
    machines = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ParseError(f"machine list: Eintrag {index} ist kein Objekt.")
        mid = item.get("id")
        if not isinstance(mid, str) or not mid:
            raise ParseError(f"machine list: Eintrag {index} hat keine id.")
        label = _opt_str(item.get("label")) or mid
        machines.append(
            Machine(
                id=mid,
                label=label,
                target=_opt_str(item.get("target")) or "",
                session=_opt_str(item.get("session")),
                enabled=bool(item.get("enabled", True)),
                selected=bool(item.get("selected", False)),
            )
        )
    return machines


def parse_machine_status(text: str) -> dict[str, MachineStatus]:
    """Return a dict profile id -> MachineStatus."""
    data = _load_json_array(text, "machine status")
    result = {}
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ParseError(f"machine status: Eintrag {index} ist kein Objekt.")
        mid = item.get("id")
        if not isinstance(mid, str) or not mid:
            raise ParseError(f"machine status: Eintrag {index} hat keine id.")
        result[mid] = MachineStatus(
            id=mid,
            label=_opt_str(item.get("label")) or mid,
            status=_opt_str(item.get("status")) or "",
            error=_opt_str(item.get("error")),
        )
    return result


def interpret_status_result(rc: int, out: str, err: str) -> dict[str, MachineStatus]:
    """Interpret a finished 'machine status --json' call.

    rc 1 with valid JSON is normal (at least one machine is not reachable).
    Without valid JSON the error carries rc and stderr.
    """
    try:
        return parse_machine_status(out)
    except ParseError as exc:
        detail = err.strip()
        msg = f"{exc}"
        if rc != 0:
            msg = f"machine status endete mit Exit-Code {rc}."
            if detail:
                msg += f"\n{detail}"
            elif out.strip():
                msg += f"\n{out.strip()}"
        raise ParseError(msg) from None


# --------------------------------------------------------------------------
# status labels
# --------------------------------------------------------------------------

_STATUS_LABELS = {
    "reachable": "erreichbar",
    "disabled": "deaktiviert",
    "error": "Fehler",
    "attention": "Attention",
}


def status_kind(status: str | None) -> str:
    """Classify a raw status: ok, off, error, attention, auth, unknown, none."""
    if status is None or not status.strip():
        return "none"
    key = status.strip().lower()
    if key == "reachable":
        return "ok"
    if key == "disabled":
        return "off"
    if key == "error":
        return "error"
    if "auth" in key:
        return "auth"
    if key == "attention":
        return "attention"
    return "unknown"


def status_label(status: str | None) -> str:
    kind = status_kind(status)
    if kind == "none":
        return ""
    if kind == "auth":
        return "Anmeldung nötig"
    key = status.strip().lower()
    return _STATUS_LABELS.get(key, status.strip())


# --------------------------------------------------------------------------
# ssh config
# --------------------------------------------------------------------------

MAX_INCLUDE_DEPTH = 16


@dataclass
class SshHost:
    alias: str
    hostname: str | None = None
    user: str | None = None
    source: str = ""

    def describe(self) -> str:
        """Short display hint like 'user@hostname' (empty when nothing is known)."""
        if self.hostname and self.user:
            return f"{self.user}@{self.hostname}"
        if self.hostname:
            return self.hostname
        if self.user:
            return f"{self.user}@"
        return ""

    def matches(self, query: str) -> bool:
        q = query.strip().lower()
        if not q:
            return True
        hay = " ".join(x for x in (self.alias, self.hostname, self.user) if x).lower()
        return q in hay


@dataclass
class _ParseState:
    hosts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    stack: list = field(default_factory=list)


_KEYWORD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)(?:\s*=\s*|\s+|$)(.*)$")


def _split_args(text: str) -> list[str]:
    """Split ssh config arguments: whitespace separated, double quotes group,
    an unquoted '#' at the start of a word starts a comment."""
    args = []
    cur = []
    in_quote = False
    in_word = False
    for ch in text:
        if in_quote:
            if ch == '"':
                in_quote = False
            else:
                cur.append(ch)
            continue
        if ch == '"':
            in_quote = True
            in_word = True
        elif ch.isspace():
            if in_word:
                args.append("".join(cur))
                cur = []
                in_word = False
        elif ch == "#" and not in_word:
            break
        else:
            cur.append(ch)
            in_word = True
    if in_word:
        args.append("".join(cur))
    return args


def _is_concrete(name: str) -> bool:
    return bool(name) and not any(c in name for c in "*?!")


def _resolve_include(pattern: str, ssh_dir: str) -> list[str]:
    path = os.path.expanduser(pattern)
    if not os.path.isabs(path):
        path = os.path.join(ssh_dir, path)
    if glob.has_magic(path):
        return sorted(glob.glob(path))
    return [path]


def _parse_text(text: str, source: str, ssh_dir: str, state: _ParseState) -> None:
    current: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _KEYWORD_RE.match(line)
        if not m:
            continue
        keyword = m.group(1).lower()
        args = _split_args(m.group(2))
        if keyword == "host":
            current = []
            for name in args:
                if not _is_concrete(name):
                    continue
                if name not in state.hosts:
                    state.hosts[name] = SshHost(alias=name, source=source)
                current.append(name)
        elif keyword == "match":
            current = []
        elif keyword == "hostname" and args:
            for name in current:
                if state.hosts[name].hostname is None:
                    state.hosts[name].hostname = args[0]
        elif keyword == "user" and args:
            for name in current:
                if state.hosts[name].user is None:
                    state.hosts[name].user = args[0]
        elif keyword == "include":
            for pattern in args:
                for path in _resolve_include(pattern, ssh_dir):
                    _parse_file(path, ssh_dir, state, f"{source}:{lineno}")


def _parse_file(path: str, ssh_dir: str, state: _ParseState, origin: str = "") -> None:
    real = os.path.realpath(path)
    if real in state.stack:
        state.warnings.append(f"Include-Schleife ignoriert: {path} ({origin})")
        return
    if len(state.stack) >= MAX_INCLUDE_DEPTH:
        state.warnings.append(f"Include zu tief verschachtelt, ignoriert: {path}")
        return
    if os.path.isdir(real):
        return
    try:
        with open(real, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        return
    except OSError as exc:
        state.warnings.append(f"Konnte {path} nicht lesen: {exc}")
        return
    state.stack.append(real)
    try:
        _parse_text(text, path, ssh_dir, state)
    finally:
        state.stack.pop()


def parse_ssh_config_text(text: str, ssh_dir: str, source: str = "<text>") -> tuple[list[SshHost], list[str]]:
    """Parse ssh config text. Returns (hosts in file order, warnings)."""
    state = _ParseState()
    _parse_text(text, source, ssh_dir, state)
    return list(state.hosts.values()), state.warnings


def load_ssh_hosts(path: str, ssh_dir: str | None = None) -> tuple[list[SshHost], list[str]]:
    """Read an ssh config file (missing file means no hosts).

    Relative Include paths resolve against ssh_dir, default ~/.ssh.
    """
    if ssh_dir is None:
        ssh_dir = os.path.expanduser("~/.ssh")
    state = _ParseState()
    _parse_file(os.path.expanduser(path), ssh_dir, state, "")
    return list(state.hosts.values()), state.warnings


def default_ssh_config(environ=None) -> tuple[str, str]:
    """Return (config path, include base dir).

    HERDR_MACHINES_MENU_SSH_CONFIG overrides the file; its directory then
    serves as the base for relative Include paths.
    """
    env = os.environ if environ is None else environ
    override = env.get("HERDR_MACHINES_MENU_SSH_CONFIG", "")
    if override.strip():
        path = os.path.abspath(os.path.expanduser(override))
        return path, os.path.dirname(path)
    return os.path.expanduser("~/.ssh/config"), os.path.expanduser("~/.ssh")


# --------------------------------------------------------------------------
# display width helpers
# --------------------------------------------------------------------------


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch) in ("Cc", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def printable(text: str) -> str:
    """Replace control characters, which curses would draw as ^X."""
    return "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in text)


def display_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


ELLIPSIS = "…"


def truncate(text: str, width: int) -> str:
    """Cut text to at most width display cells, marking cuts with an ellipsis."""
    if width <= 0:
        return ""
    text = printable(text)
    if display_width(text) <= width:
        return text
    out = []
    used = 0
    for ch in text:
        w = char_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + ELLIPSIS


def pad(text: str, width: int) -> str:
    text = truncate(text, width)
    return text + " " * (width - display_width(text))


def wrap(text: str, width: int) -> list[str]:
    """Hard-wrap text by display width, keeping explicit newlines."""
    width = max(1, width)
    lines = []
    for para in text.replace("\t", "    ").replace("\r\n", "\n").split("\n"):
        para = printable(para)
        if not para:
            lines.append("")
            continue
        cur = []
        used = 0
        for word in re.split(r"(\s+)", para):
            if not word:
                continue
            ww = display_width(word)
            if used + ww <= width:
                cur.append(word)
                used += ww
                continue
            if word.isspace():
                lines.append("".join(cur).rstrip())
                cur, used = [], 0
                continue
            if cur:
                lines.append("".join(cur).rstrip())
                cur, used = [], 0
            for ch in word:
                cw = char_width(ch)
                if used + cw > width:
                    lines.append("".join(cur))
                    cur, used = [], 0
                cur.append(ch)
                used += cw
        lines.append("".join(cur).rstrip())
    return lines
