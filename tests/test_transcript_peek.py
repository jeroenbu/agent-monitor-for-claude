"""Tests for the personal build's on-demand transcript peek (transcript_peek)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_monitor_for_claude.paths import SessionRoot, cwd_to_slug
from agent_monitor_for_claude.transcript_peek import read_peek

SID = 'aaaaaaaa-1111-2222-3333-444444444444'
CWD = 'd:\\proj'


def _root(base: str) -> SessionRoot:
    return SessionRoot(origin='windows', label=None, config_dir=Path(base), proc_dir=None, temp_dir=Path(base) / 'tmp')


def _write(base: str, entries: list[dict]) -> SessionRoot:
    root = _root(base)
    project = root.config_dir / 'projects' / cwd_to_slug(CWD)
    project.mkdir(parents=True, exist_ok=True)
    (project / f'{SID}.jsonl').write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n', encoding='utf-8')
    return root


class PeekContentTest(unittest.TestCase):
    def test_returns_the_last_turns_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = _write(base, [
                {'type': 'user', 'message': {'content': 'bouw feature X'}},
                {'type': 'assistant', 'message': {'content': [
                    {'type': 'text', 'text': 'Ik ga beginnen.'},
                    {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'description': 'run tests'}},
                ]}},
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'OK'}]}},
                {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Klaar - alles groen.'}]}},
            ])

            items = read_peek(root, SID, CWD)

        self.assertEqual([item['role'] for item in items], ['user', 'assistant', 'tool', 'assistant'])
        self.assertEqual(items[0]['text'], 'bouw feature X')
        self.assertIn('Bash', items[2]['text'])
        self.assertIn('run tests', items[2]['text'])
        self.assertEqual(items[3]['text'], 'Klaar - alles groen.')

    def test_tool_results_never_leak(self) -> None:
        # Tool results are the one content class the peek deliberately keeps
        # out: they dwarf everything else and routinely carry whole files.
        with tempfile.TemporaryDirectory() as base:
            root = _write(base, [
                {'type': 'user', 'message': {'content': 'prompt'}},
                {'type': 'user', 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'SECRET_RESULT_TEXT'},
                ]}},
            ])

            items = read_peek(root, SID, CWD)

        self.assertNotIn('SECRET_RESULT_TEXT', json.dumps(items))

    def test_a_mixed_text_and_tool_result_entry_shows_only_the_text(self) -> None:
        # An interrupt during a tool call writes one user entry carrying both a
        # text block and the tool_result: the text may show, the result never.
        with tempfile.TemporaryDirectory() as base:
            root = _write(base, [
                {'type': 'user', 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'SECRET_RESULT_TEXT'},
                    {'type': 'text', 'text': '[Request interrupted by user]'},
                ]}},
            ])

            items = read_peek(root, SID, CWD)

        joined = json.dumps(items)
        self.assertNotIn('SECRET_RESULT_TEXT', joined)
        self.assertIn('[Request interrupted by user]', joined)

    def test_tool_marker_reads_only_description_and_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = _write(base, [
                {'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'id': 't1', 'name': 'Bash',
                     'input': {'description': 'run tests', 'command': 'pytest -x SECRET_ARG'}},
                    {'type': 'tool_use', 'id': 't2', 'name': 'Read',
                     'input': {'file_path': 'd:\\proj\\main.py'}},
                ]}},
            ])

            items = read_peek(root, SID, CWD)

        joined = json.dumps(items)
        self.assertIn('run tests', joined)
        self.assertIn('main.py', joined)
        self.assertNotIn('SECRET_ARG', joined)

    def test_command_prompt_shows_name_and_args(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = _write(base, [
                {'type': 'user', 'message': {'content':
                    '<command-name>/work-on-issue</command-name><command-args>#123</command-args>'}},
            ])

            items = read_peek(root, SID, CWD)

        self.assertEqual(items[0]['role'], 'user')
        self.assertEqual(items[0]['text'], '/work-on-issue #123')

    def test_skips_sidechain_and_meta_entries(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = _write(base, [
                {'type': 'user', 'isMeta': True, 'message': {'content': 'INJECTED_NOTICE'}},
                {'type': 'assistant', 'isSidechain': True, 'message': {'content': [{'type': 'text', 'text': 'SUBAGENT_TEXT'}]}},
                {'type': 'user', 'message': {'content': 'echte prompt'}},
            ])

            items = read_peek(root, SID, CWD)

        joined = json.dumps(items)
        self.assertNotIn('INJECTED_NOTICE', joined)
        self.assertNotIn('SUBAGENT_TEXT', joined)
        self.assertEqual(items[0]['text'], 'echte prompt')

    def test_caps_the_entry_count_and_truncates_long_text(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            entries = [{'type': 'user', 'message': {'content': f'prompt {index}'}} for index in range(30)]
            entries.append({'type': 'user', 'message': {'content': 'x' * 1000}})
            root = _write(base, entries)

            items = read_peek(root, SID, CWD, max_entries=5)

        self.assertEqual(len(items), 5)
        self.assertLess(len(items[-1]['text']), 300)
        self.assertTrue(items[-1]['text'].endswith('…'))
        self.assertEqual(items[0]['text'], 'prompt 26')


class PeekBoundaryTest(unittest.TestCase):
    def test_a_non_uuid_session_id_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = _write(base, [{'type': 'user', 'message': {'content': 'prompt'}}])
            self.assertEqual(read_peek(root, '..\\..\\evil', CWD), [])

    def test_a_missing_transcript_yields_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = _root(base)
            self.assertEqual(read_peek(root, SID, CWD), [])

    def test_the_read_is_confined_to_the_projects_tree(self) -> None:
        # A cwd whose slug directory is replaced by a symlink pointing outside
        # projects/ must be refused. Symlinks need privileges on Windows, so
        # simulate the same property the cheap way: a transcript path that
        # resolves outside the root because the projects dir itself is a file.
        with tempfile.TemporaryDirectory() as base:
            root = _root(base)
            (root.config_dir / 'projects').mkdir(parents=True)
            self.assertEqual(read_peek(root, SID, CWD), [])
