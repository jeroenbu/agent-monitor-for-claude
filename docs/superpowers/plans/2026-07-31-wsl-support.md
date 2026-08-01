# WSL Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show Claude Code sessions running inside WSL distributions in Agent Monitor, at feature parity with Windows sessions, by reading their files over `\\wsl.localhost\` - never executing anything inside a distro and never waking a stopped one.

**Architecture:** The single implicit config root becomes an explicit list of `SessionRoot`s (Windows + one per running WSL distro with a `.claude`). Every record carries an `origin` string through snapshot -> UI -> bridge calls. A new `wsl.py` module isolates all WSL side effects: distro discovery (`wsl.exe --list --running --quiet`, gated and cached) and `/proc` parsing (liveness, start-time validation, descendant tree, CPU/RSS). Spec: `docs/superpowers/specs/2026-07-31-wsl-support-design.md`.

**Tech Stack:** Python 3.10+ (stdlib + psutil + pywebview), vanilla JS UI, `unittest` + `node:test`.

## Global Constraints

- Repo style rules from `.claude/CLAUDE.md` apply to every task: single quotes, 140-160 char lines, hyphens for dashes (never em dashes), numpydoc docstrings, `from __future__ import annotations`, no new dependencies, defensive parsing (`(data.get(...) or default)`, `isinstance`), side effects isolated per module, helpers underscore-prefixed, `__all__` in library modules.
- Never execute anything inside a distro; the only new executed program is `wsl.exe --list --running --quiet` with `CREATE_NO_WINDOW`.
- Never touch `\\wsl.localhost\<distro>\` for a distro absent from the running list.
- Load-bearing tests (`test_transcript_privacy.py`, `test_search.py`, `test_session_delete.py`, the HTML-safety group in `logic.test.js`) may be extended, never weakened.
- Both suites must pass after every task: `.venv\Scripts\python -m unittest discover -s tests` and `node --test tests/js/logic.test.js`.
- All work happens in the user's fork on branch `feat/wsl-support`; commits end with the Co-Authored-By trailer the harness specifies.
- `CLK_TCK` is assumed 100 (constant `_CLK_TCK` in `wsl.py`); it affects only display math and the helper window, never liveness.
- Docs (`README.md`, `PRIVACY.md`, `docs/*.md`, `CHANGELOG.md`, all 13 locales) are updated in the tasks that change the behavior they describe.

---

## Series 0: preparation

### Task 1: Fork, branch, and maintainer alignment comment

**Files:**
- No repo files. GitHub operations + local git only.

**Interfaces:**
- Produces: a fork `<user>/agent-monitor-for-claude`, local branch `feat/wsl-support` off `main`, and a design comment posted on upstream issue #6.

- [ ] **Step 1: Verify gh auth and fork**

```bash
gh auth status
gh repo fork jens-duttke/agent-monitor-for-claude --clone=false
git remote add fork https://github.com/<user>/agent-monitor-for-claude.git  # <user> from gh auth status
git fetch origin && git checkout -b feat/wsl-support origin/main
```

- [ ] **Step 2: Show the issue comment draft to the user for approval, then post it**

The comment is outward-facing and posts under the user's account: show it, get an explicit OK, then `gh issue comment 6 --repo jens-duttke/agent-monitor-for-claude --body-file <tmpfile>`. Draft:

> I'd like to contribute this myself, and before building it I want to check the approach with you.
>
> **Plan:** read the WSL side purely over `\\wsl.localhost\<distro>\` from Windows - the session registry and transcripts under `home\*\.claude`, and process liveness from `proc\<pid>\stat` (its field 22 is exactly what Claude Code writes as `procStart` inside WSL, so the same recycled-PID validation works). Nothing is ever executed inside a distro; the one new executed program is `wsl.exe --list --running --quiet` (fixed args, hidden window) so a stopped distro is never touched - and never woken - by a UNC read. Discovery is gated on a `vmmem` process existing, so with WSL off the feature costs zero. Everything stays testable without WSL via injectable roots.
>
> Measured on my machine (WSL2 Ubuntu): registry read 23 ms, transcript tail 16 ms, stat ~1 ms, full `/proc` scan 108 ms - all fine for the existing poll cadence.
>
> Sessions gain an `origin` field; the UI shows the distro as the host ("Ubuntu (WSL) > CLI") and everything else (status, cost, subagents, history, search, delete, task output, process panel) works identically through root-parameterized paths. One new setting: `"wsl": false` to opt out. PRIVACY.md and the docs would name the new read surface and the `wsl.exe` invocation explicitly, with tests guarding path confinement per root.
>
> Two questions: would you take this as a PR? And would you prefer one PR or two (core monitoring first, then the process/task panels)?

- [ ] **Step 3: Commit checkpoint**

Nothing to commit; confirm `git status` is clean and the branch tracks `origin/main`.

---

## Series 1: core monitoring

### Task 2: `SessionRoot` in paths.py, threaded through all callers (behavior unchanged)

**Files:**
- Modify: `agent_monitor_for_claude/paths.py` (whole module)
- Modify (mechanical, first param `root`): `sessions.py:31`, `transcript.py:150,203`, `subagents.py:84,303`, `search.py:197,248`, `session_delete.py:69`, `snapshot.py:34-51,119-131,146-153`, `history.py:56-70`, `tasks.py:119,129,172,210-213,223,249,314`, `app.py:313`
- Test: `tests/test_paths.py`

**Interfaces:**
- Produces (paths.py):
  - `@dataclass(frozen=True) class SessionRoot: origin: str; label: str | None; config_dir: Path; proc_dir: Path | None; temp_dir: Path`
  - `windows_root() -> SessionRoot` - `SessionRoot('windows', None, config_dir(), None, Path(tempfile.gettempdir()))`
  - `config_dir() -> Path` unchanged (still used by `settings.py`, `app.py:_default_effort`)
  - `sessions_dir(root)`, `projects_dir(root)`, `transcript_path(root, session_id, cwd)`, `task_output_dir(root, session_id, cwd)`, `task_output_path(root, session_id, cwd, task_id)`, `scratchpad_dir(root, session_id, cwd)` - all take `root: SessionRoot` as first parameter, derive from `root.config_dir` / `root.temp_dir`
  - `wsl_path_to_windows(root, path_text: str) -> str` - `/mnt/<drive>/...` -> `C:\...` (move `_WSL_MOUNT_PATTERN` + `_wsl_to_windows` here from `tasks.py`); other absolute POSIX paths (`/...`) -> `str(root.config_dir)[:...]`-independent UNC form `\\wsl.localhost\<root.label>\<path>` when `root.label` is set; everything else passed through unchanged
  - `cwd_to_slug(cwd)` unchanged
- Consumes: nothing new. Every existing caller passes `windows_root()` in this task; multi-root arrives in later tasks.

- [ ] **Step 1: Write failing tests** (append to `tests/test_paths.py`)

```python
from agent_monitor_for_claude.paths import SessionRoot, windows_root, wsl_path_to_windows, transcript_path, task_output_dir
from pathlib import Path


class SessionRootTests(unittest.TestCase):
    def _wsl_root(self):
        return SessionRoot(origin='wsl:Ubuntu', label='Ubuntu',
                           config_dir=Path(r'\\wsl.localhost\Ubuntu\home\dev\.claude'),
                           proc_dir=Path(r'\\wsl.localhost\Ubuntu\proc'),
                           temp_dir=Path(r'\\wsl.localhost\Ubuntu\tmp'))

    def test_windows_root_shape(self):
        root = windows_root()
        self.assertEqual(root.origin, 'windows')
        self.assertIsNone(root.label)
        self.assertIsNone(root.proc_dir)
        self.assertTrue(root.config_dir.name == '.claude' or 'CLAUDE_CONFIG_DIR' in os.environ)

    def test_transcript_path_uses_root(self):
        root = self._wsl_root()
        path = transcript_path(root, 'abc', '/home/dev/proj')
        self.assertEqual(path, root.config_dir / 'projects' / '-home-dev-proj' / 'abc.jsonl')

    def test_task_output_dir_uses_root_temp(self):
        root = self._wsl_root()
        self.assertEqual(task_output_dir(root, 'abc', '/home/dev/proj'),
                         root.temp_dir / 'claude' / '-home-dev-proj' / 'abc' / 'tasks')

    def test_wsl_path_to_windows_mnt(self):
        self.assertEqual(wsl_path_to_windows(self._wsl_root(), '/mnt/c/Users/dev/out.log'), 'C:\\Users\\dev\\out.log')

    def test_wsl_path_to_windows_posix(self):
        self.assertEqual(wsl_path_to_windows(self._wsl_root(), '/home/dev/run.log'),
                         '\\\\wsl.localhost\\Ubuntu\\home\\dev\\run.log')

    def test_wsl_path_to_windows_passthrough_on_windows_root(self):
        self.assertEqual(wsl_path_to_windows(windows_root(), 'C:\\x\\y.log'), 'C:\\x\\y.log')
        self.assertEqual(wsl_path_to_windows(windows_root(), '/mnt/c/x/y.log'), 'C:\\x\\y.log')
```

- [ ] **Step 2: Run to verify failure** - `.venv\Scripts\python -m unittest tests.test_paths -v` -> ImportError on `SessionRoot`.

- [ ] **Step 3: Implement** - rewrite `paths.py` per the interface above (module docstring: this is the one module that knows both the Claude Code layout and where each root lives). `wsl_path_to_windows` on a WSL root: `/mnt` translation first, then `if path_text.startswith('/'): return '\\\\wsl.localhost\\' + root.label + path_text.replace('/', '\\')`. Then mechanically update every listed caller to pass `windows_root()` (import it; inside `tasks.py` delete `_wsl_to_windows`/`_WSL_MOUNT_PATTERN` and call `wsl_path_to_windows(root, target)`). Signatures of the callers themselves do not change yet - each computes `root = windows_root()` at its top.

- [ ] **Step 4: Run both full suites** - all green (behavior unchanged).

- [ ] **Step 5: Commit** - `refactor: parameterize all Claude Code paths by session root`

### Task 3: `wsl.py` discovery + `wsl` setting (never wake a stopped distro)

**Files:**
- Create: `agent_monitor_for_claude/wsl.py`
- Modify: `agent_monitor_for_claude/settings.py` (add `WSL_MONITORING`), `agent_monitor_for_claude/process_probe.py` (public `vmmem_present()`)
- Test: `tests/test_wsl.py` (new), `tests/test_settings.py` (one case)

**Interfaces:**
- Produces (wsl.py):
  - `wsl_roots() -> list[SessionRoot]` - `[]` when `settings.WSL_MONITORING` is False, when no `vmmem*` process exists (checked via `process_probe.vmmem_present()`, cached `_VMMEM_TTL = 5.0` s), or when no running distro has a `.claude`. Distro list from `_list_running_distros()` cached `_DISCOVERY_TTL = 10.0` s; sorted by name for stable fingerprints. Roots: for each distro `d`, every `home\*\.claude` dir plus `root\.claude` under `_UNC_BASE / d`; origin `f'wsl:{d}'` (one distro with two `.claude` homes yields origin `f'wsl:{d}:{home_name}'` for the second and later - keep first as plain `wsl:{d}` for stability). If vmmem disappears the cache is bypassed and `[]` returned immediately.
  - `_parse_distro_list(raw: bytes) -> list[str]` - pure: decode UTF-16-LE, splitlines, strip, drop empties.
  - `_list_running_distros() -> list[str]` - `subprocess.run(['wsl.exe', '--list', '--running', '--quiet'], capture_output=True, timeout=5, creationflags=0x08000000)`; any failure -> `[]`.
  - `_discover_roots(distros: list[str], unc_base: Path) -> list[SessionRoot]` - pure given a base path; tests inject a temp dir as `unc_base`.
  - `reset_caches() -> None` - test hook clearing both TTL caches (module-level `_cache` dicts guarded by one `threading.Lock`).
- Produces (process_probe.py): `vmmem_present() -> bool` - one `_scan_processes()` pass, `any(name.startswith('vmmem') ...)`. Imported in `wsl.py` as `from .process_probe import vmmem_present as _vmmem_present`, so tests patch `wsl._vmmem_present`.
- Produces (settings.py): `WSL_MONITORING: bool = _S.get('wsl', True)`; `'wsl'` added to `_BOOL_KEYS`; `'WSL_MONITORING'` in `__all__`.

- [ ] **Step 1: Write failing tests** (`tests/test_wsl.py`)

```python
"""Tests for WSL distro discovery - all without WSL installed."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_monitor_for_claude import wsl


class ParseDistroListTests(unittest.TestCase):
    def test_utf16_output(self):
        raw = 'Ubuntu\r\ndocker-desktop\r\n'.encode('utf-16-le')
        self.assertEqual(wsl._parse_distro_list(raw), ['Ubuntu', 'docker-desktop'])

    def test_empty_and_garbage(self):
        self.assertEqual(wsl._parse_distro_list(b''), [])
        self.assertEqual(wsl._parse_distro_list('\r\n\r\n'.encode('utf-16-le')), [])


class DiscoverRootsTests(unittest.TestCase):
    def test_home_and_root_claude(self):
        with tempfile.TemporaryDirectory() as base:
            claude = Path(base) / 'Ubuntu' / 'home' / 'dev' / '.claude'
            claude.mkdir(parents=True)
            (Path(base) / 'Ubuntu' / 'root').mkdir(parents=True)
            (Path(base) / 'docker-desktop').mkdir()  # no .claude anywhere
            roots = wsl._discover_roots(['Ubuntu', 'docker-desktop'], Path(base))
            self.assertEqual(len(roots), 1)
            self.assertEqual(roots[0].origin, 'wsl:Ubuntu')
            self.assertEqual(roots[0].label, 'Ubuntu')
            self.assertEqual(roots[0].config_dir, claude)
            self.assertEqual(roots[0].proc_dir, Path(base) / 'Ubuntu' / 'proc')
            self.assertEqual(roots[0].temp_dir, Path(base) / 'Ubuntu' / 'tmp')

    def test_stopped_distro_never_globbed(self):
        with tempfile.TemporaryDirectory() as base:
            (Path(base) / 'Stopped' / 'home' / 'dev' / '.claude').mkdir(parents=True)
            self.assertEqual(wsl._discover_roots([], Path(base)), [])


class WslRootsGateTests(unittest.TestCase):
    def setUp(self):
        wsl.reset_caches()
        self.addCleanup(wsl.reset_caches)

    def test_no_vmmem_short_circuits(self):
        with mock.patch.object(wsl, '_vmmem_present', return_value=False), \
             mock.patch.object(wsl, '_list_running_distros') as listing:
            self.assertEqual(wsl.wsl_roots(), [])
            listing.assert_not_called()

    def test_setting_off_short_circuits(self):
        with mock.patch.object(wsl, 'WSL_MONITORING', False), \
             mock.patch.object(wsl, '_vmmem_present') as probe:
            self.assertEqual(wsl.wsl_roots(), [])
            probe.assert_not_called()

    def test_discovery_cached_within_ttl(self):
        with mock.patch.object(wsl, '_vmmem_present', return_value=True), \
             mock.patch.object(wsl, '_list_running_distros', return_value=[]) as listing:
            wsl.wsl_roots()
            wsl.wsl_roots()
            self.assertEqual(listing.call_count, 1)
```

- [ ] **Step 2: Run to verify failure** - ModuleNotFoundError `wsl`.

- [ ] **Step 3: Implement** `wsl.py` (docstring names the guarantees: enumeration-only `wsl.exe`, never touch a distro absent from the running list), `vmmem_present()` in process_probe (add to `__all__`), settings key. `wsl.py` imports `WSL_MONITORING` at module level as `from .settings import WSL_MONITORING` - re-bindable in tests via `mock.patch.object(wsl, 'WSL_MONITORING', ...)`. `_UNC_BASE = Path(r'\\wsl.localhost')`.

- [ ] **Step 4: Run suites** - green, including the new settings validation case (`{'wsl': 'yes'}` reports a type error and is dropped).

- [ ] **Step 5: Commit** - `feat: discover running WSL distros with a .claude, behind a wsl setting`

### Task 4: `/proc` probe in `wsl.py` (liveness, recycled pids, descendants)

**Files:**
- Modify: `agent_monitor_for_claude/wsl.py`, `agent_monitor_for_claude/process_probe.py` (rename `_SESSION_HELPER_WINDOW_SECONDS` -> public `SESSION_HELPER_WINDOW_SECONDS`, keep value 10.0)
- Test: `tests/test_wsl.py`

**Interfaces:**
- Produces (wsl.py):
  - `probe_wsl_sessions(root: SessionRoot, requests: list[tuple[int, int | None]]) -> dict[int, ProcessInfo]` - `ProcessInfo` reused from `process_probe`. One `iterdir()` of `root.proc_dir`; numeric entries' `stat` files parsed into `{pid: (comm, ppid, starttime)}`. Per request: absent pid -> `ProcessInfo(alive=False, tool_running=False)`; `proc_start_ticks` given and `!= starttime` -> not alive; else alive with `host=None`, `via_cli=False`, `child_count=len(descendants)`, `tool_running=child_count > 0`. Descendants: ppid tree below the session pid, excluding children whose `starttime - session_starttime <= SESSION_HELPER_WINDOW_SECONDS * _CLK_TCK` (session-lifetime helpers, e.g. stdio MCP servers). Any OSError on the proc dir -> every request not alive.
  - `_parse_stat(text: str) -> tuple[str, list[str]] | None` - pure: `head, _, tail = text.rpartition(')')`; comm between first `(` and that last `)`; returns `(comm, tail.split())` where index N-3 holds stat field N (ppid=idx 1, utime=11, stime=12, starttime=19, rss pages=21). None when malformed.
- Consumes: `SessionRoot` (Task 2), `ProcessInfo` and `SESSION_HELPER_WINDOW_SECONDS` from `process_probe`.

- [ ] **Step 1: Write failing tests** (append; helper builds a fake proc tree)

```python
def _write_stat(proc_dir: Path, pid: int, comm: str, ppid: int, starttime: int) -> None:
    entry = proc_dir / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    fields3plus = ['S', str(ppid), '1', '1', '0', '-1', '4194304', '0', '0', '0', '0',
                   '50', '10', '0', '0', '20', '0', '4', '0', str(starttime), '1000000', '500']
    (entry / 'stat').write_text(f'{pid} ({comm}) ' + ' '.join(fields3plus), encoding='utf-8')


class ParseStatTests(unittest.TestCase):
    def test_comm_with_spaces_and_parens(self):
        parsed = wsl._parse_stat('123 (tmux: server (x)) S 1 123 123 0 -1 4 0 0 0 0 5 6 0 0 20 0 1 0 83860 1 2')
        self.assertIsNotNone(parsed)
        comm, fields = parsed
        self.assertEqual(comm, 'tmux: server (x)')
        self.assertEqual(fields[1], '1')        # ppid (field 4)
        self.assertEqual(fields[19], '83860')   # starttime (field 22)

    def test_malformed(self):
        self.assertIsNone(wsl._parse_stat('no parens here'))


class ProbeWslSessionsTests(unittest.TestCase):
    def _root(self, base: str):
        return wsl.SessionRoot(origin='wsl:U', label='U', config_dir=Path(base) / 'cfg',
                               proc_dir=Path(base) / 'proc', temp_dir=Path(base) / 'tmp')

    def test_liveness_and_recycled_pid(self):
        with tempfile.TemporaryDirectory() as base:
            root = self._root(base)
            _write_stat(root.proc_dir, 100, 'claude', 1, 5000)
            self.assertTrue(wsl.probe_wsl_sessions(root, [(100, 5000)])[100].alive)
            self.assertFalse(wsl.probe_wsl_sessions(root, [(100, 4999)])[100].alive)   # recycled
            self.assertFalse(wsl.probe_wsl_sessions(root, [(200, None)])[200].alive)   # gone

    def test_descendants_and_helper_window(self):
        with tempfile.TemporaryDirectory() as base:
            root = self._root(base)
            _write_stat(root.proc_dir, 100, 'claude', 1, 5000)
            _write_stat(root.proc_dir, 101, 'node', 100, 5000 + 500)     # helper: within 10 s * 100 ticks
            _write_stat(root.proc_dir, 102, 'cargo', 100, 5000 + 60000)  # real tool child
            _write_stat(root.proc_dir, 103, 'rustc', 102, 5000 + 60010)  # grandchild
            info = wsl.probe_wsl_sessions(root, [(100, 5000)])[100]
            self.assertTrue(info.alive)
            self.assertEqual(info.child_count, 2)
            self.assertTrue(info.tool_running)
            self.assertIsNone(info.host)

    def test_unreadable_proc_dir(self):
        root = self._root(tempfile.mkdtemp())
        root = wsl.SessionRoot(origin='wsl:U', label='U', config_dir=root.config_dir,
                               proc_dir=root.proc_dir / 'missing', temp_dir=root.temp_dir)
        self.assertFalse(wsl.probe_wsl_sessions(root, [(1, None)])[1].alive)
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** (probe + `_parse_stat`; rename the helper-window constant in `process_probe.py` and its two internal uses; add to `__all__`).
- [ ] **Step 4: Run both suites.**
- [ ] **Step 5: Commit** - `feat: probe WSL session liveness and children from procfs over 9P`

### Task 5: `roots.py` + origin-tagged registry records

**Files:**
- Create: `agent_monitor_for_claude/roots.py`
- Modify: `agent_monitor_for_claude/sessions.py`
- Test: `tests/test_sessions.py`, new `tests/test_roots.py`

**Interfaces:**
- Produces (roots.py): `session_roots() -> list[SessionRoot]` (`[windows_root(), *wsl_roots()]`); `root_for_origin(origin: object) -> SessionRoot | None` (exact string match against `session_roots()`; non-str or unknown -> None). Module docstring: an unknown origin is always a refusal, never a fallback - a stale UI call must not touch another root.
- Produces (sessions.py): `list_sessions(root: SessionRoot)` records gain `'origin': root.origin` and `'origin_label': root.label`.

- [ ] **Step 1: Failing tests** - `test_roots.py`: with `wsl.wsl_roots` mocked to return one fake root, `session_roots()` is `[windows, fake]`; `root_for_origin('wsl:U')` finds it; `root_for_origin('nope')`/`root_for_origin(5)` -> None. `test_sessions.py`: existing temp-registry fixture, assert `records[0]['origin'] == 'windows'` and `origin_label is None`; plus one case pointing `list_sessions` at a fake WSL root's registry asserting `origin == 'wsl:U'`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run suites.**
- [ ] **Step 5: Commit** - `feat: enumerate session roots and tag registry records with their origin`

### Task 6: Multi-root snapshot, fingerprint, and scan-cache pruning

**Files:**
- Modify: `agent_monitor_for_claude/snapshot.py`, `agent_monitor_for_claude/transcript.py` (only `prune_scan_cache`)
- Test: `tests/test_snapshot.py`, `tests/test_scan_appended.py`

**Interfaces:**
- Produces (snapshot.py):
  - `build_snapshot()` iterates `session_roots()`; Windows records probed via `probe_all`, each WSL root's records via `probe_wsl_sessions(root, ...)`; lookup keyed `(root.origin, pid)` so a Linux pid never hits the Windows table and colliding pids cannot cross. Session records pass `origin`/`origin_label` through. `_build_session_record(root, record, info)` calls `state_for(root, ...)`, `count_subagents(root, ...)`.
  - `live_or_recent_ids() -> set[str]` - same iteration.
  - `registry_fingerprint()` - per root, parts prefixed `f'{root.origin}:{...}'`; roots in `session_roots()` order (stable: windows first, distros sorted).
- Produces (transcript.py): `prune_scan_cache(active: Iterable[tuple[SessionRoot, str, str]])` - keys via `transcript_path(root, session_id, cwd)`.
- Consumes: Tasks 2-5.

- [ ] **Step 1: Failing tests** - `test_snapshot.py`: build a fake WSL root (temp config dir with registry + transcript + proc dir with a live claude pid) and mock `snapshot.session_roots` to return `[windows_temp_root, fake_wsl_root]`; assert the WSL session appears with `origin == 'wsl:U'`, `alive True`, correct `age_seconds`; assert a Windows record with the same pid number stays independent; `registry_fingerprint()` contains both `windows:` and `wsl:U:` parts and changes when the WSL transcript grows. `test_scan_appended.py`: prune call updated to the new tuple shape keeps cache behavior identical.
- [ ] **Step 2: Run to verify failure.** - `state_for`/`count_subagents` already take root (Task 2), so failures are the missing multi-root iteration and tuple shape.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run suites.**
- [ ] **Step 5: Commit** - `feat: assemble the snapshot and fingerprint across all session roots`

### Task 7: Multi-root history

**Files:**
- Modify: `agent_monitor_for_claude/history.py`
- Test: `tests/test_history.py`

**Interfaces:**
- Produces: `list_history()` loops `session_roots()`; per root: its own `projects_dir(root)` scan, its own `slug_to_cwd` map built from that root's `list_sessions(root)` (a Windows cwd must never canonicalize a WSL slug), records gain `'origin': root.origin, 'origin_label': root.label`. Dedup set `live_or_recent_ids()` fetched once (session ids are UUIDs, globally unique).

- [ ] **Step 1: Write the failing test** (append to `tests/test_history.py`; reuse that file's temp-registry helpers)

```python
def test_history_lists_wsl_root_with_own_cwd(self):
    with tempfile.TemporaryDirectory() as base:
        wsl_root = SessionRoot(origin='wsl:U', label='U', config_dir=Path(base) / 'cfg',
                               proc_dir=Path(base) / 'proc', temp_dir=Path(base) / 'tmp')
        project = wsl_root.config_dir / 'projects' / '-home-dev-proj'
        project.mkdir(parents=True)
        (project / 'aaaaaaaa-1111-2222-3333-444444444444.jsonl').write_text(
            json.dumps({'type': 'user', 'cwd': '/home/dev/proj', 'timestamp': '2026-07-01T10:00:00Z',
                        'message': {'content': 'hello wsl'}}) + '\n', encoding='utf-8')

        with mock.patch.object(history, 'session_roots', return_value=[self.windows_root, wsl_root]), \
             mock.patch.object(history, 'live_or_recent_ids', return_value=set()):
            records = history.list_history()

    wsl_records = [r for r in records if r['origin'] == 'wsl:U']
    self.assertEqual(len(wsl_records), 1)
    self.assertEqual(wsl_records[0]['cwd'], '/home/dev/proj')
    self.assertEqual(wsl_records[0]['origin_label'], 'U')
```

- [ ] **Step 2: Run to verify failure** - `origin` KeyError / single-root scan misses the WSL transcript.
- [ ] **Step 3: Implement** (loop roots; per-root `slug_to_cwd` from that root's `list_sessions(root)` only).
- [ ] **Step 4: Run both suites.**
- [ ] **Step 5: Commit** - `feat: list past sessions from every root in history`

### Task 8: Origin-aware content search

**Files:**
- Modify: `agent_monitor_for_claude/search.py`
- Test: `tests/test_search.py` (extend; never weaken)

**Interfaces:**
- Produces: session refs become `(session_id, cwd, origin)` (`_valid_refs` reads `item.get('origin')`, non-str -> `'windows'` for backward shape-compat in tests, unknown origin -> ref dropped). `_ordered_transcripts` resolves each ref's root via `root_for_origin` and confines via `transcript_path(root, ...).resolve().relative_to(projects_dir(root).resolve())`. The projects roots are resolved once per call, per origin.
- Consumes: `root_for_origin` (Task 5).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_search.py`; reuse its `_collect_updates` helper pattern)

```python
def _wsl_root_with_transcript(self, base: str, text: str) -> SessionRoot:
    root = SessionRoot(origin='wsl:U', label='U', config_dir=Path(base) / 'cfg',
                       proc_dir=None, temp_dir=Path(base) / 'tmp')
    project = root.config_dir / 'projects' / '-home-dev-proj'
    project.mkdir(parents=True)
    (project / f'{WSL_SID}.jsonl').write_text(json.dumps({'type': 'user', 'message': {'content': text}}) + '\n',
                                              encoding='utf-8')
    return root

def test_wsl_ref_matches_via_its_origin(self):
    with tempfile.TemporaryDirectory() as base:
        root = self._wsl_root_with_transcript(base, 'needle-in-wsl')
        refs = [{'session_id': WSL_SID, 'cwd': '/home/dev/proj', 'origin': 'wsl:U'}]
        with mock.patch.object(search, 'root_for_origin', side_effect=lambda o: root if o == 'wsl:U' else None):
            updates = self._collect_updates('needle-in-wsl', refs)
    self.assertIn(WSL_SID, self._all_ids(updates))

def test_unknown_origin_scans_nothing(self):
    with tempfile.TemporaryDirectory() as base:
        root = self._wsl_root_with_transcript(base, 'needle-in-wsl')
        refs = [{'session_id': WSL_SID, 'cwd': '/home/dev/proj', 'origin': 'nope'}]
        with mock.patch.object(search, 'root_for_origin', return_value=None):
            updates = self._collect_updates('needle-in-wsl', refs)
    self.assertEqual(self._all_ids(updates), [])

def test_wsl_confinement_refuses_escaping_cwd(self):
    with tempfile.TemporaryDirectory() as base:
        root = self._wsl_root_with_transcript(base, 'needle-in-wsl')
        secret = Path(base) / 'outside.jsonl'
        secret.write_text('needle-in-wsl\n', encoding='utf-8')
        # A cwd crafted so slug + id would escape projects/ must be refused per root,
        # mirroring the existing Windows confinement case.
        refs = [{'session_id': WSL_SID, 'cwd': '../../..', 'origin': 'wsl:U'}]
        with mock.patch.object(search, 'root_for_origin', return_value=root):
            updates = self._collect_updates('needle-in-wsl', refs)
    self.assertEqual(self._all_ids(updates), [])
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** (refs `(session_id, cwd, origin)`; per-origin root resolution and `projects_dir(root)` confinement; unknown origin drops the ref).
- [ ] **Step 4: Run both suites** - including every pre-existing search test untouched.
- [ ] **Step 5: Commit** - `feat: search transcripts across roots with per-root confinement`

### Task 9: Origin-aware session deletion

**Files:**
- Modify: `agent_monitor_for_claude/session_delete.py`
- Test: `tests/test_session_delete.py` (extend; never weaken)

**Interfaces:**
- Produces: `delete_session(session_id: str, cwd: str, origin: str = 'windows') -> bool`. `root = root_for_origin(origin)`; None -> False (a distro no longer running can never be deleted from - by design, since its files are unreachable anyway). Confinement against `projects_dir(root)`. `_is_live(session_id)` becomes root-wide: for every root in `session_roots()`, records with this id are probed via that root's probe kind (windows -> `probe_all`, wsl -> `probe_wsl_sessions`); any alive -> refuse.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_session_delete.py`; `_write_stat` helper from `test_wsl.py`, imported or duplicated locally)

```python
def _wsl_root_with_session(self, base: str, alive_pid: int | None) -> SessionRoot:
    root = SessionRoot(origin='wsl:U', label='U', config_dir=Path(base) / 'cfg',
                       proc_dir=Path(base) / 'proc', temp_dir=Path(base) / 'tmp')
    project = root.config_dir / 'projects' / '-home-dev-proj'
    (project / WSL_SID).mkdir(parents=True)                      # subagent dir
    (project / f'{WSL_SID}.jsonl').write_text('{}\n', encoding='utf-8')
    root.proc_dir.mkdir(parents=True)
    if alive_pid is not None:
        _write_stat(root.proc_dir, alive_pid, 'claude', 1, 5000)
        registry = root.config_dir / 'sessions'
        registry.mkdir(parents=True)
        (registry / f'{alive_pid}.json').write_text(
            json.dumps({'pid': alive_pid, 'sessionId': WSL_SID, 'cwd': '/home/dev/proj', 'procStart': '5000'}),
            encoding='utf-8')
    return root

def test_deletes_dead_wsl_session(self):
    with tempfile.TemporaryDirectory() as base:
        root = self._wsl_root_with_session(base, alive_pid=None)
        with mock.patch.object(session_delete, 'root_for_origin', return_value=root), \
             mock.patch.object(session_delete, 'session_roots', return_value=[root]):
            self.assertTrue(session_delete.delete_session(WSL_SID, '/home/dev/proj', 'wsl:U'))
        self.assertFalse((root.config_dir / 'projects' / '-home-dev-proj' / f'{WSL_SID}.jsonl').exists())
        self.assertFalse((root.config_dir / 'projects' / '-home-dev-proj' / WSL_SID).exists())

def test_refuses_live_wsl_session(self):
    with tempfile.TemporaryDirectory() as base:
        root = self._wsl_root_with_session(base, alive_pid=321)
        with mock.patch.object(session_delete, 'root_for_origin', return_value=root), \
             mock.patch.object(session_delete, 'session_roots', return_value=[root]):
            self.assertFalse(session_delete.delete_session(WSL_SID, '/home/dev/proj', 'wsl:U'))
        self.assertTrue((root.config_dir / 'projects' / '-home-dev-proj' / f'{WSL_SID}.jsonl').exists())

def test_refuses_unknown_origin(self):
    with mock.patch.object(session_delete, 'root_for_origin', return_value=None):
        self.assertFalse(session_delete.delete_session(WSL_SID, '/home/dev/proj', 'gone:X'))
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** (`delete_session(session_id, cwd, origin='windows')`; `_is_live` walks `session_roots()` and probes each root with its own probe kind).
- [ ] **Step 4: Run both suites** - every pre-existing deletion guard untouched.
- [ ] **Step 5: Commit** - `feat: delete past WSL sessions with the same three guards`

### Task 10: Bridge and focus routing

**Files:**
- Modify: `agent_monitor_for_claude/app.py`, `agent_monitor_for_claude/window_focus.py`
- Test: `tests/test_app.py`, `tests/test_window_focus.py`

**Interfaces:**
- Produces (window_focus.py): `focus_terminal_window(session_title: str) -> bool` - enum windows, `select_terminal_window(windows, process_names(), session_title)`, activate. No pid parameter; add to `__all__`.
- Produces (app.py):
  - `focus_session(pid, project_name='', session_id='', vscode_deeplink=False, session_title='', origin='windows')` - when `isinstance(origin, str) and origin.startswith('wsl:')`: return `focus_terminal_window(title)` (a Linux pid must never reach `focus_session_window` - a colliding Windows pid would raise the wrong window); else unchanged.
  - `delete_session(session_id, cwd, origin='windows')`, `get_tasks(..., origin='windows')`, `read_task_output(..., origin='windows')`, `scratchpad_path(session_id, cwd, origin='windows')` - each resolves `root_for_origin(origin)` and refuses (False/empty/`{'tasks': [], 'total': 0}`) on None. `scratchpad_path` is fully origin-correct here already: it calls `scratchpad_dir(root, ...)` (root-parameterized since Task 2). `get_tasks`/`read_task_output` validate the origin but still call the single-root `tasks.py` API (its functions gain a root parameter only in Task 14); until then a WSL origin yields the empty/None result those functions produce for absent files - the documented interim degradation.
  - `open_path(path, origin='windows')` - `root = root_for_origin(origin)`; None -> False; `open_directory(wsl_path_to_windows(root, path))`.
  - `get_process_stats(pid, origin='windows')` - WSL origin returns `[]` for now (Task 13 fills it in); the registry lookup for `proc_start_ticks` moves to the matching root's `list_sessions(root)`.
- Consumes: everything above.

- [ ] **Step 1: Failing tests** - `test_window_focus.py`: `focus_terminal_window` finds a terminal window by title via the pure selectors (mock `_enum_windows`/`process_names`/`_activate`). `test_app.py`: `open_path('/home/dev/proj', 'wsl:U')` with mocked `root_for_origin`/`open_directory` receives the UNC translation; `focus_session(..., origin='wsl:U')` never calls `focus_session_window` (mock it, assert not called) and calls `focus_terminal_window`; `delete_session` passes origin through; unknown origin -> False everywhere.
- [ ] **Step 2-5:** red -> implement -> green -> commit `feat: route bridge calls by session origin`.

### Task 11: UI - origin through logic.js, index.js, dev-mock, locales

**Files:**
- Modify: `agent_monitor_for_claude/ui/logic.js` (buildSession ~line 891, hostLabel block ~782), `agent_monitor_for_claude/ui/index.js` (hostText ~2062, focusSession ~2404, openRowMenu/scratchpad ~2462, delete ~1430, tasks ~1239/1252, process stats ~1227, currentSessionRefs, row dataset), `agent_monitor_for_claude/ui/dev-mock.js`, all 13 `locale/*.json`
- Test: `tests/js/logic.test.js`, `tests/test_i18n.py` (keyset check runs as-is)

**Interfaces:**
- Produces (logic.js): `buildSession` output gains `origin: typeof raw.origin === 'string' ? raw.origin : 'windows'`, `wsl: origin.startsWith('wsl:')`, `origin_label: raw.origin_label || null`, and `host` becomes: for a WSL session `(raw.origin_label || 'WSL') + ' (WSL)'`, else `hostLabel(raw.host, raw.entrypoint)` as today.
- Produces (index.js): `hostText` unchanged (host already carries the marker; `via_cli` from `entrypoint 'cli'` renders "Ubuntu (WSL) > CLI"); host cell gets `data-tip` from new label `host_wsl_tip` when `session.wsl`; every bridge call listed in Task 10 passes `session.origin` (row dataset `data-origin`); `currentSessionRefs()` items gain `origin`.
- Produces (locales): key `host_wsl_tip`, English: `"This agent runs inside the WSL distribution \u201c{distro}\u201d. Its files are read over \\\\wsl.localhost; nothing is ever executed inside the distribution."` - `{distro}` substituted UI-side like existing placeholder labels; translated in all 13 files.
- Produces (dev-mock.js): one extra session - `rawSession({ pid: 4242, session_id: '...', cwd: '/home/dev/projects/orbital-sim', origin: 'wsl:Ubuntu', origin_label: 'Ubuntu', entrypoint: 'cli', ... })`.

- [ ] **Step 1: Failing JS tests** (logic.test.js)

```javascript
test('buildSession: WSL origin shapes host and flags', () => {
    const session = logic.buildSession({ ...baseRaw, origin: 'wsl:Ubuntu', origin_label: 'Ubuntu', entrypoint: 'cli' }, labels, null);
    assert.equal(session.origin, 'wsl:Ubuntu');
    assert.equal(session.wsl, true);
    assert.equal(session.host, 'Ubuntu (WSL)');
    assert.equal(session.via_cli, true);
});
test('buildSession: absent origin defaults to windows', () => {
    const session = logic.buildSession(baseRaw, labels, null);
    assert.equal(session.origin, 'windows');
    assert.equal(session.wsl, false);
});
```

- [ ] **Step 2: Run to verify failure** - `node --test tests/js/logic.test.js`.
- [ ] **Step 3: Implement** logic.js + index.js + dev-mock; add the locale key to `locale/en.json` and translate into de/es/fr/hi/id/it/ja/ko/pt-BR/uk/zh-CN/zh-TW.
- [ ] **Step 4: Run both suites** (test_i18n enforces the keyset) **and open `ui/index.html?mock` in a browser** - the WSL showcase session renders with host "Ubuntu (WSL) > CLI".
- [ ] **Step 5: Commit** - `feat: show WSL sessions with their distro as host in the UI`

### Task 12: Series-1 docs

**Files:**
- Modify: `README.md`, `PRIVACY.md`, `.claude/CLAUDE.md`, `docs/how-it-works.md`, `docs/configuration.md`, `CHANGELOG.md`

**Interfaces:** none (docs).

- [ ] **Step 1: Write the updates**
  - README: feature bullet under "What you see every day" ("**WSL sessions too** - Claude Code agents running inside WSL distributions appear alongside your Windows agents, read over `\\wsl.localhost` with nothing ever executed inside the distro; the distro shows as the session's host"), requirements note, and the `wsl` setting pointer.
  - configuration.md: `wsl` (bool, default true) row.
  - how-it-works.md: a "WSL sessions" section - roots, running-list gate, procfs liveness (field 22 == `procStart`).
  - PRIVACY.md: extend the read-surface list (the `.claude` registry/transcripts, `/proc` metadata - names, links, times, never command lines - under `\\wsl.localhost\<distro>\`), the executed-program list (`wsl.exe --list --running --quiet`, enumeration only), the never-wake guarantee, and "Verify it yourself" (grep for `wsl.exe` showing the single fixed invocation; test names `test_wsl.py`, the delete/search confinement cases).
  - CLAUDE.md: Security & Transparency (the new read surface + the one enumeration invocation), Claude Code Internals (WSL registry `procStart` = stat field 22), Purpose & Scope (multi-root sentence).
  - CHANGELOG under `[Unreleased]` / Added: "Sessions running inside WSL distributions now appear alongside Windows sessions - with status, cost, history, search and deletion working identically; a new `wsl` setting turns this off".
- [ ] **Step 2: Re-run the PRIVACY.md "Verify it yourself" commands** - quoted output must match reality.
- [ ] **Step 3: Run both suites** (unchanged, but the gate applies to every task).
- [ ] **Step 4: Commit** - `docs: document WSL monitoring, its read surface, and the wsl setting`

---

## Series 2: process features

### Task 13: WSL process panel stats

**Files:**
- Modify: `agent_monitor_for_claude/wsl.py`, `agent_monitor_for_claude/app.py` (`get_process_stats` WSL branch), `agent_monitor_for_claude/ui/index.js` (pass origin - done in Task 11's dataset, wire the call)
- Test: `tests/test_wsl.py`

**Interfaces:**
- Produces (wsl.py): `wsl_process_stats(root: SessionRoot, pid: int, proc_start_ticks: int | None) -> list[ChildProcessStat]` - `ChildProcessStat` reused from `process_probe`. Same descendant set as `probe_wsl_sessions`. Per descendant: `name=comm`, `rss_bytes=rss_pages * 4096`, `uptime_seconds=(now_ticks - starttime)/_CLK_TCK` where `now_ticks` derives from `btime` in `<proc>/stat` (`btime` line) and `time.time()`; `cpu_percent` from a module-level sample cache `{(origin, pid): (starttime, cpu_ticks, wall_time)}` - first sighting None, then `delta_ticks/_CLK_TCK / delta_wall * 100.0`; cache pruned to the live descendant set, guarded by a lock, recycled starttime invalidates. Stale/missing session pid -> `[]`. Rows sorted `(name, pid)`. No `wsl_vm` context row here: the per-descendant rows ARE the real load (the vmmem row remains a Windows-session concept).
- Produces (app.py): `get_process_stats(pid, origin)` WSL branch - root + `proc_start_ticks` from that root's registry records, call `wsl_process_stats`.

- [ ] **Step 1: Failing tests** - fake proc tree: first call yields `cpu None`, rss/uptime correct (btime file written in the fake proc dir root as `btime 1700000000` line among others); second call after bumping utime/stime in the stat file and patching `time.time()` +1.0 yields `cpu` approximately `(delta/100)/1.0*100`; recycled child starttime resets to None; dead session pid -> `[]`.
- [ ] **Step 2-5:** red -> implement -> green -> commit `feat: live CPU/memory/uptime for WSL session processes in the panel`.

### Task 14: WSL background-task output

**Files:**
- Modify: `agent_monitor_for_claude/tasks.py` (root as first param on `list_tasks`/`read_task_output`/`_task_meta`/`_effective_output_path`/`_resolve_redirect`), `agent_monitor_for_claude/app.py` (pass resolved root)
- Test: `tests/test_tasks.py` (extend; confinement cases never weakened)

**Interfaces:**
- Produces (tasks.py): `list_tasks(root, session_id, cwd, *, max_tasks=..., recent_seconds=None)`, `read_task_output(root, session_id, cwd, task_id, *, max_bytes=...)`. `_resolve_redirect(command, root, session_id, cwd)`: target passes through `wsl_path_to_windows(root, target)`; a relative target resolves against `wsl_path_to_windows(root, cwd)`; confinement roots are `scratchpad_dir(root, ...)` and `Path(wsl_path_to_windows(root, cwd))`.
- Consumes: `wsl_path_to_windows` (Task 2), roots resolved in app.py (Task 10 signatures already exist).

- [ ] **Step 1: Failing tests** - with a fake WSL root (temp dirs standing in for the UNC tree): a task output file under `temp_dir/claude/<slug>/<sid>/tasks/` lists and reads; an empty capture file with command `make > /home/dev/proj/build.log 2>&1` follows the redirect to the translated project path; a redirect to `/etc/passwd` is refused; a `/mnt/c/...` redirect translates and is refused unless inside the (translated) cwd; existing Windows cases green unchanged.
- [ ] **Step 2-5:** red -> implement -> green -> commit `feat: follow WSL task output and redirects inside session-owned trees`.

### Task 15: Series-2 docs + end-to-end verification

**Files:**
- Modify: `README.md` (process/task bullets mention WSL parity), `PRIVACY.md` (task-output read surface under the distro's `/tmp/claude/`), `CHANGELOG.md`
- Test: full suites + live smoke test

**Interfaces:** none.

- [ ] **Step 1: Docs updates** (as listed; re-run the PRIVACY verify commands).
- [ ] **Step 2: Full suites** - `.venv\Scripts\python -m unittest discover -s tests` and `node --test tests/js/logic.test.js`.
- [ ] **Step 3: Live smoke test on this machine** - run `.venv\Scripts\python -m agent_monitor_for_claude` with a claude session open in WSL Ubuntu and one in PowerShell; verify: both sessions visible, WSL host label + tooltip, status flips when prompting the WSL session, cost shown, history chip lists dead WSL sessions, search finds a WSL-only string, process badge appears for a `sleep 300 &` child, task output panel streams a `run_in_background` task, focus click raises the Windows Terminal tab, `wsl --shutdown` makes WSL sessions disappear without errors and does not restart the distro (watch `wsl -l -v` for 60 s).
- [ ] **Step 4: Commit** - `docs: document WSL process-panel and task-output parity`
- [ ] **Step 5: Push and open the PR** (after user approval of the final state): `git push fork feat/wsl-support`, then `gh pr create --repo jens-duttke/agent-monitor-for-claude --title "Show Claude Code sessions running inside WSL distributions" --body-file <drafted body referencing issue #6, summarizing the guarantees and the test story, ending with the required PR trailer>` - or two PRs if the maintainer asked for the split in Task 1's issue thread.

---

## Self-review notes

- Spec coverage: roots/discovery/gates (T3), liveness+descendants (T4), origin pipeline (T5-T6), fingerprint (T6), history/search/delete (T7-T9), focus/host/open-path (T10-T11), settings (T3), UI+locales+dev-mock (T11), privacy/docs (T12, T15), process panel (T13), task output (T14), phasing/PR (T1, T15). The spec's "never wake" rule is tested in T3 (`test_stopped_distro_never_globbed`) and smoke-checked in T15.
- Type consistency: `SessionRoot` fields used identically in T2-T14; `ProcessInfo`/`ChildProcessStat` reused, never redefined; `probe_wsl_sessions` returns `dict[int, ProcessInfo]` keyed by pid within one root, and cross-root keying `(origin, pid)` lives only in snapshot.py (T6).
- Known simplification: `get_tasks`/`read_task_output`/`scratchpad_path` accept origins from Task 10 on, but WSL task files only resolve once Task 14 lands; in between they degrade to empty results, which is the tool's normal absent-directory behavior.
