# WSL Support - Design

Date: 2026-07-31
Issue: [#6 - WSL support](https://github.com/jens-duttke/agent-monitor-for-claude/issues/6)
Status: approved by requester (jeroenbu); to be proposed to the maintainer before the PR

## Problem

Claude Code runs both natively on Windows and inside WSL distributions on the same
machine. Each instance writes its session registry and transcripts under its own
`~/.claude/` - the Windows one under `C:\Users\<user>\.claude`, a WSL one under
`/home/<user>/.claude` inside the distribution. Agent Monitor reads only the
Windows config directory, so WSL sessions are invisible.

Goal: feature parity - everything the monitor shows and does for a Windows
session works for a WSL session, with graceful degradation only where the
platform genuinely cannot provide it.

## Verified facts (measured on a Win11 machine, WSL2 Ubuntu, 37 processes in the distro)

All access goes through the `\\wsl.localhost\<distro>\` UNC path (the 9P file
server Windows provides for running distributions). Measured from Windows
Python 3.11, best of repeated runs:

| Operation | Cost |
|---|---|
| Registry glob + read (4 records) | 23 ms |
| Full `/proc` scan, all `<pid>/stat` (37 processes) | 108 ms |
| Targeted `/proc/<pid>/stat` read | ~1.5 ms per pid |
| Transcript `stat()` | ~1 ms |
| Targeted 256 KB transcript tail read | 16 ms |

Two facts make liveness detection clean:

1. `\\wsl.localhost\<distro>\proc\` exposes the Linux procfs read-only to
   Windows, including `stat` (ppid, start time, utime/stime, RSS).
2. The registry's `procStart` field, written by Claude Code inside WSL, is
   exactly field 22 of `/proc/<pid>/stat` (process start time in clock ticks
   since boot) - verified against a live session (registry `83860` == stat
   field 22 `83860`). On Windows the same field carries .NET wall-clock ticks;
   the two platforms need separate comparison code but identical semantics:
   a mismatch means the pid was recycled and the record is stale.

Stale-record reality check: the test machine's WSL registry held records from
months ago whose pids no longer exist; the `/proc` existence check classified
them correctly.

## Approach (chosen over alternatives)

**Read everything over `\\wsl.localhost` from the Windows side.** No process is
ever executed inside a distribution; the tool stays a pure reader of files, the
same stance it has on Windows. Rejected alternatives:

- *Querying via `wsl.exe -e ...` per poll*: spawns processes continuously,
  executes code inside the user's distro (a real widening of the security
  profile), keeps the distro awake, and is harder to test. The measured 9P
  numbers remove its only advantage.
- *Shared `CLAUDE_CONFIG_DIR` workaround*: does not work without code changes
  anyway (Linux pids would be validated against the Windows process table),
  and it degrades Claude Code itself (all transcript writes over 9P, shared
  credentials).

## Architecture

### Session roots

The single implicit config root becomes an explicit list of session roots.
Each root carries:

- `origin` - a stable string key: `'windows'` or `'wsl:<distro>'`
- `label` - display name for the UI (`None` for Windows; the distro name for WSL)
- `config_dir` - the `.claude` directory (`Path`)
- `proc_dir` - `\\wsl.localhost\<distro>\proc` for WSL roots, `None` for Windows
- `temp_dir` - `\\wsl.localhost\<distro>\tmp` for WSL roots; Windows keeps
  `tempfile.gettempdir()`

`paths.py` keeps all layout knowledge (registry, projects, task-output,
scratchpad locations) but is parameterized by root. The `cwd_to_slug` scheme is
identical for Linux paths (verified: `/home/jeroen/...` maps to
`-home-jeroen-...` on disk), so no slug changes are needed.

A new module `wsl.py` isolates the WSL side effects, following the repo's
one-module-per-side-effect rule:

- distro discovery (see below)
- `/proc` parsing: liveness, start-time validation, descendant tree, CPU/RSS

Every session record carries its `origin` through the whole pipeline:
snapshot -> UI -> back through bridge calls (`delete_session`, `get_tasks`,
`read_task_output`, `get_process_stats`, `focus_session`, `open_path`,
`start_search`, `get_history`). Python stays stateless; the UI passes the
origin back with each call, and Python resolves it against the current root
list (an unknown origin is a no-op, never a fallback to another root).

The origin key also fixes a latent collision: probe results are keyed by pid
today; pid 5728 can exist on Windows and in a distro simultaneously. All probe
maps become keyed by `(origin, pid)`, and a Linux pid is never passed to the
Windows process table (probe, ancestry, or window focus).

### Discovery - never wake a distribution

Touching `\\wsl.localhost\<distro>\` for a stopped distribution starts it.
This tool must never cause that. Therefore:

- The distro list comes only from `wsl.exe --list --running --quiet`
  (fixed arguments, `CREATE_NO_WINDOW`, output decoded as UTF-16-LE).
  This is the one new executed program: a built-in Windows utility, used to
  enumerate, never to run anything inside a distro.
- That call runs at most every ~10 s (cached), and only when the per-poll
  process-table scan (which already exists) sees a `vmmem*` process. WSL off
  means zero overhead and zero behavior change.
- Per running distro, roots are found by globbing `home\*\.claude` and
  checking `root\.claude`. A distro without a `.claude` (e.g. docker-desktop)
  never becomes a root.
- If `vmmem` disappears, all WSL roots are treated as gone immediately, even
  inside the cache window.

Known limitation (documented, accepted): if one distro is stopped while
another keeps the VM alive (docker-desktop), a poll inside the ~10 s cache
window can restart the stopped distro once; an idle distro shuts itself down
again shortly after.

Keeping a running distro awake: 9P reads reset the distro's idle timer, so a
distro with live sessions stays up (it would anyway - the sessions themselves
keep it up). When a distro has no live sessions and would idle out, our polls
could keep it alive; the fingerprint/poll path therefore only touches roots
that still appear in the `--running` list, and the monitor's reads stop as
soon as the distro leaves that list.

### Liveness and `/proc` parsing

For a WSL session record:

- `proc\<pid>\` missing -> not alive (~1.5 ms).
- Present and the record carries `procStart` -> compare with stat field 22;
  mismatch -> not alive (pid recycled).
- Parsing note: stat field 2 (`comm`) may contain spaces and parentheses
  (`(tmux: server)`); fields are parsed from the last `)` backwards/onwards,
  the standard procfs pitfall.

Descendants (the background-process badge) come from one scan of the distro's
`/proc` (108 ms measured), shared by all of that distro's sessions in the
poll: ppid links build the tree. The session-helper-window rule carries over
unchanged (a child whose start time - field 22 - lies within the existing
10 s window of the session's own start is a session-lifetime helper such as an
MCP server, not a running tool). There is no `conhost.exe` equivalent to
ignore.

The on-demand process panel computes, per descendant:

- CPU%: utime+stime (fields 14/15) delta between two samples divided by the
  sample interval and `CLK_TCK` - same non-blocking behavior as today
  (first reading `None`, real percentage a second later).
- RSS: field 24 x 4096.
- Uptime: field 22 / `CLK_TCK` relative to `btime` from `/proc/stat`.

`CLK_TCK` is assumed 100 (the Linux/WSL2 kernel default, verified on the test
distro); it cannot be queried without executing code in the distro, and a
deviation would only scale display figures, never affect liveness.

The existing `vmmem` context row remains what it is today: context for a
*Windows* session whose descendants include a WSL relay. A WSL-origin session
does not need it - its per-descendant rows from `/proc` already show the real
load individually, which the machine-wide VM figure never could.

## Feature parity

- **Status, cost, model, subagents, workflows**: unchanged logic; the readers
  receive root-aware paths. Transcript content, timestamps (UTC), usage
  fields, and the subagents/workflows directory layout are identical inside
  WSL. The incremental tail-scan cache keys gain the origin (a cwd string
  could in principle repeat across distros).
- **Fingerprint**: extends over WSL roots (registry read 23 ms + ~1 ms per
  transcript stat), so WSL changes surface as fast as Windows ones. With WSL
  off: no extra cost (empty cached root list).
- **Host and window focus**: a WSL CLI session has no Windows process, so the
  ancestry route is skipped entirely (a coincidentally matching Windows pid
  must never focus the wrong window). Host shows the distro with a WSL marker
  ("Ubuntu - WSL", styled like the existing CLI marker). Focus goes straight
  to the existing terminal-title fallback (`select_terminal_window`), which
  matches the session title Claude Code sets as the terminal title - covers
  Windows Terminal (the requester's setup) and the other known emulators.
  VS Code Remote-WSL degrades gracefully in v1 (no host claim, no deep-link
  guarantee).
- **Background-task output panel**: task files live at
  `/tmp/claude/<slug>/<session>/tasks/` -> read via the root's `temp_dir`.
  Redirect following gains one translation: a Linux target inside the
  session's scratchpad or project cwd resolves via the distro root; a
  `/mnt/<drive>/` target uses the existing translation. All confinement
  checks (`relative_to` against allowed roots, UUID/task-id validation)
  apply unchanged.
- **History**: scanned per root, on demand as today; records tagged with
  their origin.
- **Search**: transcript paths resolve per origin; path confinement checks
  against the union of known projects roots (each path against its own
  session's root).
- **Delete**: same three guards, root-aware - UUID validation, `relative_to`
  confinement against that origin's `projects/`, and a liveness re-probe via
  the WSL probe immediately before deletion.
- **Open folder / scratchpad**: a Linux cwd or scratchpad translates to its
  `\\wsl.localhost\...` equivalent before the existing `os.path.isdir` check
  and `os.startfile` (Explorer opens UNC paths natively).

## UI and settings

- Session records gain `origin` (string) and `origin_label` (distro name or
  `null`); `ui/logic.js` renders the host cell from it (distro + WSL marker).
  Everything else (grouping, sorting, chips, search scoping) is untouched -
  Windows and WSL sessions mix under their own project panels.
- `dev-mock.js` gains one WSL showcase session.
- New locale keys: kept minimal (the WSL marker tooltip), added to all 13
  locales in the same change (enforced by `test_i18n`).
- Settings: one new key, `"wsl": false` to opt out (bool, default `true`),
  documented in `docs/configuration.md`. Zero configuration required for the
  default experience.

## Privacy and transparency

`PRIVACY.md` and `CLAUDE.md` (Security & Transparency, Claude Code Internals)
are updated in the same change:

- New read surface, named explicitly: under `\\wsl.localhost\<distro>\`, the
  same three categories as on Windows - the `.claude` registry/transcripts,
  `/proc` process metadata (names, links, times; never command lines), and
  task output under `/tmp/claude/`. Nothing else.
- New executed program, named explicitly: `wsl.exe --list --running --quiet`,
  fixed arguments, enumeration only. No code ever runs inside a distro.
- Write guarantees unchanged: zero writes on the WSL side except the existing
  user-confirmed session deletion (same three guards).
- The "Verify it yourself" section gains matching grep commands and test
  names.

## Testing (all without WSL installed)

Session roots are injectable, so a temp directory can impersonate a distro
(fabricated `proc/<pid>/stat` files, registry, transcripts, task output).

- New `tests/test_wsl.py`: discovery parsing (UTF-16-LE output, distros
  without `.claude`, vmmem gate), stat parsing (comm with spaces/parens),
  liveness (missing pid, start-time mismatch), descendant tree with the
  helper window, CPU/RSS/uptime math, and the never-touch-a-stopped-distro
  rule (no path access for a distro absent from the running list).
- Extended existing suites: multi-root sessions/snapshot/fingerprint;
  delete and search confinement per root (load-bearing privacy tests are
  extended, never weakened); Linux redirect translation in `test_tasks.py`.
- `tests/js/logic.test.js`: origin rendering (host label, marker).

## Phasing and PR process

1. Post a compact design summary as a comment on issue #6 (English): the
   approach, the guarantees (read-only, nothing executed in the distro, never
   wakes a stopped distro), the measured costs, and two questions - does the
   maintainer want this as a PR, and one PR or two.
2. Build in the requester's fork on a feature branch, as two logically
   separate commit series so a split stays trivial:
   - Series 1 (core): roots, discovery, liveness, status/cost/history/
     search/delete, UI origin, settings, docs, tests.
   - Series 2 (process features): descendant badge, process panel, task
     output panel.
3. The requester can run the fork immediately, independent of upstream
   review.

## Out of scope / known limitations

- VS Code Remote-WSL host detection and deep-link focus (degrades gracefully).
- WSL1 distributions: whatever `--list --running` reports is tried and fails
  defensively; not a target.
- Non-default `$TMPDIR` inside a distro (assumed `/tmp`).
- The ~10 s restart window for a distro stopped while another keeps the VM
  alive (documented above).
- Distros of other Windows users on the same machine.
