# herdr-machines-menu

A small [Herdr](https://herdr.dev) plugin (`thelad.machines-menu`) that opens a
popup menu for Herdr's saved SSH machines. Add, enable, disable, reconnect, rename and remove remote
machines from the keyboard.

The menu never edits Herdr's state itself. Every action runs the Herdr CLI
(`herdr machine list|status|add|reconnect|enable|disable|rename|remove`), so Herdr's own checks,
locking and live reload of open clients stay in place.

## Requirements

- Herdr 0.9.5 or newer
- Linux or macOS
- Python 3.11 or newer with the standard `curses` module (no other dependencies)
- A terminal with a UTF-8 locale

## Installation

From GitHub:

```bash
herdr plugin install thelad-dev/herdr-machines-menu
```

Or from a local checkout:

```bash
git clone https://github.com/thelad-dev/herdr-machines-menu
herdr plugin link ./herdr-machines-menu
```

The plugin does not add a key binding. To open it with a key, add one to your Herdr config, for
example:

```toml
[[keys.command]]
key = "prefix+m"
type = "plugin_action"
command = "thelad.machines-menu.open"
description = "Manage machines"
```

A second action, `thelad.machines-menu.add`, opens the menu straight in the add dialog. Without a
key binding you can run `herdr plugin action invoke thelad.machines-menu.open`.

## Keys

| Key | Action |
| --- | --- |
| Up/Down, j/k, PgUp/PgDn | select a machine |
| A | add |
| E | enable |
| D | disable |
| Space | toggle enabled |
| R | reconnect |
| N | rename |
| X, Del | remove (asks first, default is No) |
| U, F5 | refresh list and status |
| ? | help |
| Esc, Q | close |

## Features

- The table shows label, target, session, enabled and status. Status comes from
  `herdr machine status --json`, which runs in the background; while it runs the row shows a
  "checking" hint. The error text of a failing machine appears in the status line when it is
  selected.
- Add: pick a host from `~/.ssh/config` (`Include` is followed, patterns such as `Host *` are
  skipped) or type any target such as `user@host` or `ssh://user@host:2222`. Label and remote
  session are optional (empty means Herdr's default, or Herdr asks).
- Add and reconnect run directly in the popup terminal, so Herdr can ask questions there (session
  choice, installation, replacing the server, SSH login). Afterwards the menu shows the exit code
  and waits for Enter.
- Enable, disable, rename and remove run without a terminal. If a command fails, its full output is
  shown in an error window that you close yourself.
- Disable keeps the profile, remove forgets it. In both cases sessions and processes on the remote
  host keep running.
- `HERDR_MACHINES_MENU_SSH_CONFIG` points the add dialog at a different SSH config file (useful for
  testing); relative `Include` paths are then resolved against that file's directory.

## Limitations

- The user interface is in German.
- The SSH target and remote session of an existing profile cannot be changed, because Herdr does
  not support that. Remove the machine and add it again instead.
- Windows is not supported.
- Status comes from Herdr's non-interactive check. "Reachable" means the remote Herdr server
  answers right now; login or host key prompts are handled only by reconnect.
- Labels are display names only. All actions use the profile ID, even when two machines have
  similar labels.
- The machine catalog is global per user, not per Herdr session.
- Herdr 0.9.5 accepts `herdr machine rename` only as `<profile-id> --label <label>`, although its
  `--help` shows the reverse order; the plugin uses the working form.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The testable logic lives in `machines_core.py`, the curses interface in `machines_menu.py`.

## License

MIT, see [LICENSE](LICENSE).
