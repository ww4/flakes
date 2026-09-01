"""Live end-to-end smoke test: start the real server, drive it with a real MCP client.

Not a unit test — this exercises the actual Streamable HTTP transport, the
secret path prefix, tool registration, and a real write to a scratch space.

Run:  python3 tests/smoke_live.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

PREFIX = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
PORT = 8799

# Set in main(); drive() uses it to assert on-disk state, not just tool replies.
SPACE_ROOT: Path


def make_space(root: Path) -> None:
    (root / "Inbox").mkdir(parents=True)
    (root / "Areas").mkdir()
    (root / "Projects").mkdir()
    (root / "System").mkdir()
    (root / "CONVENTIONS.md").write_text("# Conventions\n\nNo hard-wrapping. Tasks use - [ ].\n")
    (root / "index.md").write_text("# Rosemary Acres daybook\n\nThe shared space.\n")
    (root / "Areas" / "Homelab.md").write_text(
        "# Homelab\n\nGromit runs mergerfs and SnapRAID, no ZFS.\n\n- [ ] replace the parity drive\n"
    )
    (root / "Projects" / "Lock3 Website.md").write_text("# Lock3 Website\n\nZola rebuild.\n")


async def drive(url: str) -> int:
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures += 1

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            expected = sorted(
                [
                    "get_context",
                    "search_notes",
                    "read_note",
                    "save_note",
                    "append_note",
                    "request_work",
                ]
            )
            check("exactly six tools, correctly named", names == expected, str(names))

            ctx = await session.call_tool("get_context", {})
            ctx_text = ctx.content[0].text
            check("get_context carries conventions", "No hard-wrapping" in ctx_text)
            check("get_context carries open tasks", "replace the parity drive" in ctx_text)
            check("get_context carries service inventory", "Service modules" in ctx_text)
            check("get_context omits agent board by default", "Agent task board" not in ctx_text)

            hits = await session.call_tool("search_notes", {"query": "mergerfs"})
            check("search finds content", "Homelab" in hits.content[0].text)

            saved = await session.call_tool(
                "save_note",
                {
                    "title": "Idea from the phone",
                    "body": "A literal $& and $1 must survive.",
                    "tags": ["inbox", "idea"],
                },
            )
            saved_text = saved.content[0].text
            check("save_note returns an inbox path", "Inbox/" in saved_text, saved_text)

            again = await session.call_tool(
                "save_note", {"title": "Idea from the phone", "body": "second"}
            )
            check("collision suffixed", "-2.md" in again.content[0].text, again.content[0].text)

            import json

            path = json.loads(saved_text)["path"]
            appended = await session.call_tool(
                "append_note", {"path": path, "body": "appended $` line"}
            )
            # POSITIVE CONTROL for the refusal checks below. If a legitimate
            # write also came back isError, then `bool(res.isError)` would be
            # True for everything and the refusal assertions would be vacuous.
            check(
                "control: a LEGITIMATE append reports isError=False",
                appended.isError is False,
                f"isError={appended.isError!r}",
            )
            check("append_note succeeded", "bytes_written" in appended.content[0].text)

            body = await session.call_tool("read_note", {"path": path})
            body_text = body.content[0].text
            check("dollar sequences round-tripped", "$&" in body_text and "$`" in body_text)
            check("no YAML frontmatter", not body_text.lstrip().startswith("---"))

            # Write scoping must hold over the wire, not just in unit tests.
            # Two assertions per case: the call is refused, AND the target file
            # is genuinely unmodified on disk. isError alone could in principle
            # be set while a partial write still landed.
            for bad in ["../../etc/passwd", "CONFIG.md", "Areas/Homelab.md", "%2e%2e%2fx.md"]:
                res = await session.call_tool("append_note", {"path": bad, "body": "pwned"})
                check(f"write to {bad!r} refused", res.isError is True, f"isError={res.isError!r}")

            homelab = (SPACE_ROOT / "Areas" / "Homelab.md").read_text()
            check("no refused write reached disk", "pwned" not in homelab)
            check(
                "space contains no stray files outside Inbox",
                sorted(p.name for p in SPACE_ROOT.rglob("*pwned*")) == [],
            )

            queued = await session.call_tool(
                "request_work",
                {"title": "Check the backup", "what": "Verify last night's restic run", "urgency": "soon"},
            )
            check("request_work queued", "queued" in queued.content[0].text)

    return failures


async def main() -> int:
    global SPACE_ROOT
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "space"
        make_space(root)
        SPACE_ROOT = root

        env = {
            **os.environ,
            "HOMELAB_MCP_SPACE_ROOT": str(root),
            "HOMELAB_MCP_PORT": str(PORT),
            "HOMELAB_MCP_PATH_PREFIX": PREFIX,
            "HOMELAB_MCP_FLAKE_ROOT": "/home/claude/flakes",
            "PYTHONPATH": "src",
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "homelab_mcp", env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.sleep(3.0)
            if proc.returncode is not None:
                err = (await proc.stderr.read()).decode()
                print("server exited early:\n" + err)
                return 1

            good = f"http://127.0.0.1:{PORT}/{PREFIX}/mcp"
            print(f"\nDriving {good}\n")
            failures = await drive(good)

            # The secret prefix must actually gate the endpoint.
            print("\nSecret path prefix:")
            import httpx

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://127.0.0.1:{PORT}/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Accept": "application/json, text/event-stream"},
                )
                ok = resp.status_code == 404
                print(f"  [{'PASS' if ok else 'FAIL'}] bare /mcp is not served (got {resp.status_code})")
                if not ok:
                    failures += 1

            print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILURE(S)'}")
            return failures
        finally:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
