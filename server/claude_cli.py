"""Thin async wrapper around the local `claude` CLI (uses the Mac's logged-in Claude subscription).

Every call is a fresh, tool-less, non-persisted `claude -p` run. `--safe-mode` skips the user's
plugins, hooks and CLAUDE.md so calls are fast and only see what we send.
"""

import asyncio
import json
import os
import shutil
from pathlib import Path

from .config import CLAUDE_WORKDIR


class ClaudeError(Exception):
    pass


def find_claude() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for p in ("~/.local/bin/claude", "~/.claude/local/claude", "~/.npm-global/bin/claude",
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude", "/usr/bin/claude"):
        path = Path(p).expanduser()
        if path.exists():
            return str(path)
    return None


CLAUDE_BIN = find_claude()
BASE_ARGS = ["-p", "--safe-mode", "--tools", "", "--no-session-persistence", "--strict-mcp-config"]


def _env() -> dict:
    env = dict(os.environ)
    # If NoteKarLo itself was launched from inside a Claude Code session, don't let the
    # child think it is nested.
    for key in list(env):
        if key == "CLAUDECODE" or key.startswith("CLAUDE_CODE_") or key == "CLAUDE_AGENT_SDK_VERSION":
            env.pop(key, None)
    return env


def _args(system: str, model: str, output: str) -> list[str]:
    if not CLAUDE_BIN:
        raise ClaudeError("Claude CLI not found. Install it and run `claude` once to log in.")
    return [CLAUDE_BIN, *BASE_ARGS, "--model", model, "--system-prompt", system, "--output-format", output]


async def run_json(prompt: str, system: str, model: str, schema: dict | None = None, timeout: float = 120) -> dict:
    """One-shot call. Returns the CLI's final result object (has `result` and `structured_output`)."""
    args = _args(system, model, "json")
    if schema:
        args += ["--json-schema", json.dumps(schema)]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(CLAUDE_WORKDIR),
        env=_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ClaudeError(f"Claude took longer than {int(timeout)}s")
    return _parse_result(out, err)


def _parse_result(out: bytes, err: bytes) -> dict:
    text = out.decode(errors="replace").strip()
    data = None
    for line in reversed(text.splitlines()):
        try:
            data = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if data is None:
        raise ClaudeError((err.decode(errors="replace") or text or "no output from claude")[-400:])
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        raise ClaudeError(str(data.get("result") or data.get("subtype") or "claude error")[:400])
    return data


async def stream_text(prompt: str, system: str, model: str, on_delta, timeout: float = 240) -> str:
    """Streams text deltas to `on_delta(str)` (may be async) and returns the full text."""
    args = _args(system, model, "stream-json") + ["--include-partial-messages", "--verbose"]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(CLAUDE_WORKDIR),
        env=_env(),
        limit=16 * 1024 * 1024,
    )
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    final = None
    chunks: list[str] = []

    async def pump():
        nonlocal final
        async for raw in proc.stdout:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "stream_event":
                ev = event.get("event", {})
                if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                    piece = ev["delta"]["text"]
                    chunks.append(piece)
                    res = on_delta(piece)
                    if asyncio.iscoroutine(res):
                        await res
            elif event.get("type") == "result":
                final = event

    try:
        await asyncio.wait_for(pump(), timeout)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        raise ClaudeError(f"Claude took longer than {int(timeout)}s")
    if final is None:
        err = (await proc.stderr.read()).decode(errors="replace")
        raise ClaudeError(err[-400:] or "no result from claude")
    if final.get("is_error"):
        raise ClaudeError(str(final.get("result"))[:400])
    return final.get("result") or "".join(chunks)
