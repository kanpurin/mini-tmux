# mini-tmux

`mini-tmux` is a small tmux-like terminal multiplexer for Linux environments where
tmux itself is not available. It uses only the Python standard library.

## Features

- Create and reattach to named sessions.
- Keep shells running after the display detaches.
- Run multiple independent shells.
- Manage virtual windows.
- Split panes horizontally and vertically.
- Move focus between panes.
- List and kill sessions.
- Kill panes and windows.

This is intentionally a small MVP, not a full terminal emulator. Normal shell
workflows are supported, while complex full-screen terminal apps may not render
perfectly yet.

## Usage

```sh
chmod +x mini_tmux.py
./mini_tmux.py new -s work
./mini_tmux.py attach -t work
./mini_tmux.py ls
./mini_tmux.py kill-session -t work
```

Create a detached session:

```sh
./mini_tmux.py new -s work -d
```

## Key bindings

The prefix key is `Ctrl-b`.

| Key | Action |
| --- | --- |
| `Ctrl-b d` | detach |
| `Ctrl-b %` | split pane left/right |
| `Ctrl-b "` | split pane top/bottom |
| `Ctrl-b h/j/k/l` | focus left/down/up/right |
| `Ctrl-b Arrow` | focus by direction |
| `Ctrl-b Tab` | focus next pane |
| `Ctrl-b c` | create window |
| `Ctrl-b n` | next window |
| `Ctrl-b p` | previous window |
| `Ctrl-b 0..9` | select window |
| `Ctrl-b x` | kill focused pane |
| `Ctrl-b &` | kill active window |
| `Ctrl-b q` | kill session |

## Runtime files

Session sockets and metadata live under `$XDG_RUNTIME_DIR/mini-tmux` when
available, otherwise `/tmp/mini-tmux-$UID`.

## Development

Run unit tests:

```sh
python3 -m unittest discover -s tests
```
