"""
Transcript Peek (personal build)
================================

On-demand read of the last few conversation turns of one session, for the
peek dialog this personal build adds.  This deliberately crosses the upstream
project's content-free line - the "Personal build" note in ``PRIVACY.md``
says so out loud - but keeps its discipline everywhere else:

* read only on an explicit user action (opening the dialog), never on the
  per-second poll;
* confined to the session's own transcript under its root's ``projects/``
  tree, with the session id validated as a UUID, exactly like the search and
  deletion surfaces;
* returning display text only: the user's prompts, the assistant's text, and
  a one-line marker per tool call (its name plus the ``description`` or
  ``file_path`` it was given).  Tool RESULTS are never read - they dwarf
  everything else and routinely carry whole files.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .paths import SessionRoot, projects_dir, transcript_path
# The wrapper-stripping pattern and the tail reader are transcript.py's
# knowledge of Claude Code's file framing; reusing them keeps the two views of
# a prompt identical and leaves one copy of the tail-reading subtleties.
from .transcript import _WRAPPER_PATTERN, _read_tail

__all__ = ['read_peek']

_SESSION_ID_PATTERN = re.compile(r'\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z')

_COMMAND_NAME_PATTERN = re.compile(r'<command-name>(.*?)</command-name>', re.S)
_COMMAND_ARGS_PATTERN = re.compile(r'<command-args>(.*?)</command-args>', re.S)

# One tail read per dialog refresh; the last dozen turns fit comfortably.
_PEEK_TAIL_BYTES = 262144

# Per-item display cap: enough to read what a turn is about, small enough
# that one long turn cannot flood the dialog.
_PEEK_MAX_CHARS = 240

_DEFAULT_MAX_ENTRIES = 12


def read_peek(root: SessionRoot, session_id: str, cwd: str, *, max_entries: int = _DEFAULT_MAX_ENTRIES) -> list[dict[str, str]]:
    """Return the last conversation turns of a session as display items.

    Each item is ``{'role': 'user' | 'assistant' | 'tool', 'text': str}``,
    oldest first.  Anything that fails validation or reading yields ``[]`` -
    the dialog then simply shows nothing.

    Parameters
    ----------
    root : SessionRoot
        The session root the session belongs to.
    session_id : str
        The session UUID (validated; anything else is refused).
    cwd : str
        The session working directory, mapped to its project slug.
    max_entries : int
        Cap on the returned items; the newest ones win.
    """
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.match(session_id):
        return []
    if not isinstance(cwd, str) or not cwd:
        return []

    try:
        path = transcript_path(root, session_id, cwd).resolve()
        path.relative_to(projects_dir(root).resolve())
    except (OSError, ValueError):
        return []

    items: list[dict[str, str]] = []
    for line in _read_tail(path, _PEEK_TAIL_BYTES):
        entry = _load(line)
        if entry is not None:
            items.extend(_entry_items(entry))

    return items[-max_entries:]


def _entry_items(entry: dict) -> list[dict[str, str]]:
    """Map one transcript entry to zero or more display items."""
    if entry.get('isSidechain') is True or entry.get('isMeta') is True:
        return []

    entry_type = entry.get('type')
    message = entry.get('message')
    content = message.get('content') if isinstance(message, dict) else None

    if entry_type == 'user':
        return _user_items(content)
    if entry_type == 'assistant':
        return _assistant_items(content)

    return []


def _user_items(content: Any) -> list[dict[str, str]]:
    """Render a user entry: its prompt text, or nothing for machine traffic."""
    text = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str):
                text = block['text']
                break

    if not isinstance(text, str):
        # A tool_result-only entry is machine traffic, not something the user said.
        return []

    command_match = _COMMAND_NAME_PATTERN.search(text)
    if command_match:
        command = command_match.group(1).strip()
        args_match = _COMMAND_ARGS_PATTERN.search(text)
        args = args_match.group(1).strip() if args_match else ''
        display = f'{command} {args}'.strip()
        return [{'role': 'user', 'text': _clip(display)}] if display else []

    cleaned = _clip(_WRAPPER_PATTERN.sub('', text))
    return [{'role': 'user', 'text': cleaned}] if cleaned else []


def _assistant_items(content: Any) -> list[dict[str, str]]:
    """Render an assistant entry: its text once, plus one marker per tool call."""
    items: list[dict[str, str]] = []

    if isinstance(content, str):
        text = _clip(content)
        return [{'role': 'assistant', 'text': text}] if text else []

    if not isinstance(content, list):
        return []

    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'text' and isinstance(block.get('text'), str):
            texts.append(block['text'])
        elif block.get('type') == 'tool_use':
            marker = _tool_marker(block)
            if marker:
                items.append({'role': 'tool', 'text': marker})

    text = _clip(' '.join(texts))
    if text:
        items.insert(0, {'role': 'assistant', 'text': text})

    return items


def _tool_marker(block: dict) -> str:
    """One line naming a tool call: the tool, plus its description or file path.

    Only those two input fields are read - never a command line, a prompt, or
    any other argument.
    """
    name = block.get('name')
    if not isinstance(name, str) or not name:
        return ''

    tool_input = block.get('input')
    hint = ''
    if isinstance(tool_input, dict):
        for key in ('description', 'file_path'):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                hint = value.strip()
                break

    return _clip(f'{name} - {hint}' if hint else name)


def _clip(text: str) -> str:
    """Collapse whitespace and cap the display length."""
    cleaned = ' '.join(text.split())
    if len(cleaned) > _PEEK_MAX_CHARS:
        cleaned = cleaned[:_PEEK_MAX_CHARS - 1] + '…'

    return cleaned


def _load(line: str) -> dict | None:
    """Parse one JSONL line into a dict, or return None on any error."""
    line = line.strip()
    if not line:
        return None

    try:
        value = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    return value if isinstance(value, dict) else None
