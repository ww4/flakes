"""`request_work()` — the asynchronous handoff from a chat to the gromit agent.

A request is appended as an ordinary SilverBullet task on a queue page. That is
the whole mechanism, and it is deliberately unglamorous: the queue is plain
markdown in the space, so both Chris and the agent can read, edit, reorder or
delete entries with the tools they already use, and migrating to a different
handoff layer later (see the `buzz-collab-platform` note) moves a file
convention rather than rewriting a system.

The queue page path is operator configuration, never caller input, so there is
no traversal surface here — it is validated once at startup by
`Settings.resolved_queue_page()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# One line per paragraph, deliberately. CONVENTIONS.md forbids hard-wrapping
# because SilverBullet renders every newline as a real line break — a wrapped
# paragraph shows up as three broken lines in the editor. (Caught on the first
# live deploy: this header was originally wrapped at 78 columns out of habit.)
HEADER = """# Agent work queue

Requests filed from Claude chats via the `homelab-mcp` connector. The agent picks these up on its scheduled runs. Check one off (`- [x]`) to close it, or delete the line to drop it — this page is ordinary markdown and yours to edit.
"""

VALID_URGENCY = ("whenever", "soon", "today")


@dataclass(frozen=True)
class QueuedRequest:
    page: str
    bytes_written: int


def append_request(
    queue_path: Path,
    space_root: Path,
    title: str,
    what: str,
    why: str | None = None,
    urgency: str = "whenever",
    now: datetime | None = None,
) -> QueuedRequest:
    if not title or not title.strip():
        raise ValueError("title must not be empty")
    if not what or not what.strip():
        raise ValueError("what must not be empty — describe the work concretely")
    if urgency not in VALID_URGENCY:
        raise ValueError(f"urgency must be one of {', '.join(VALID_URGENCY)}")

    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")

    # One line per bullet: CONVENTIONS.md forbids hard-wrapping, because
    # SilverBullet renders every newline as a real line break.
    lines = [
        "",
        f"- [ ] **{title.strip()}** #agent-request #{urgency}",
        f"    - filed {stamp} from a Claude chat",
        f"    - what: {' '.join(what.split())}",
    ]
    if why and why.strip():
        lines.append(f"    - why: {' '.join(why.split())}")

    chunk = ("\n".join(lines) + "\n").encode("utf-8")

    queue_path.parent.mkdir(parents=True, exist_ok=True)
    if not queue_path.exists():
        queue_path.write_text(HEADER, encoding="utf-8")

    with open(queue_path, "ab") as handle:
        handle.write(chunk)

    return QueuedRequest(
        page=queue_path.relative_to(space_root).as_posix(),
        bytes_written=len(chunk),
    )
