# vivado-mcp

vivado-mcp is a remote automation server for AMD Vivado built on the Model
Context Protocol (MCP). It allows Claude Code and other MCP clients to use
Vivado on a Linux build server over SSH, without requiring the Xilinx/AMD
toolchain on the client machine.

The current release provides full Vivado session control, Git workspace
synchronization, project management, durable build jobs, build progress and log
inspection, and structured analysis of utilization, timing, DRC, CDC, power,
and related reports.

## Key capabilities

- Connects to the build server over SSH without opening additional TCP ports.
- Automatically deploys a remote Python agent backed by a persistent Unix
  socket service.
- Maintains long-lived Vivado Tcl sessions so open projects and checkpoints can
  be reused across commands.
- Runs synthesis, implementation, and bitstream builds as independent jobs.
  Job state survives SSH disconnections and daemon restarts.
- Provides cancellable jobs, long polling, and incremental event cursors.
- Synchronizes dirty Git worktrees through a temporary index without changing
  the user's branch, HEAD, or staging area.
- Enforces workspace boundaries for every file path and exposes paths as
  workspace-relative POSIX paths.
- Normalizes Vivado reports instead of sending large text reports directly into
  the model context.
- Caches report results by DCP content hash and compares utilization or timing
  across builds.
- Deduplicates Vivado messages by message ID to condense repeated warnings and
  errors.
- Enforces a single-writer lock for each project, preventing multiple Vivado
  processes from modifying the same `.xpr` file concurrently.

## Requirements

### Client

- Python 3.12 or later
- `uv`
- OpenSSH client
- Git
- rsync
- An MCP client with stdio transport support, such as Claude Code

### Build server

- Linux
- Python 3.12 or later
- Git
- rsync
- Non-interactive SSH access
- An installed Vivado release with an accessible `settings64.sh`

The remote agent uses only the Python standard library. It does not require the
project's Python dependencies or root privileges on the build server.

## Installation

Clone the repository on the client and install its dependencies:

```sh
uv sync --locked
```

Create `~/.config/vivado-mcp/config.toml`. To use a different configuration
file, set the `VMCP_CONFIG` environment variable:

```toml
[host]
name = "fpga-builder"
ssh = "fpga-builder"
agent_dir = "~/.vivado-mcp"
max_concurrent_jobs = 1
default_jobs = 16
nice = 10
stall_timeout_s = 900

[[host.tools]]
label = "vivado-2023.2"
kind = "vivado"
settings_sh = "/tools/Xilinx/Vivado/2023.2/settings64.sh"
default = true

[[workspace]]
name = "example-project"
local = "/absolute/path/to/example-project"
host = "fpga-builder"
build = "build"
default = true

[security]
allow_eval = true
```

`host.ssh` may be a `user@hostname` target or a host alias from
`~/.ssh/config`. Public-key authentication is recommended. Verify that the
following command runs without prompting for input:

```sh
ssh fpga-builder python3 --version
```

Multiple `[[host.tools]]` entries may be configured to select among Vivado
versions. The `tool` argument accepted by MCP tools refers to the corresponding
`label`. When omitted, vivado-mcp uses the entry marked `default = true`, or the
first entry if no default is declared.

Multiple `[[workspace]]` entries are also supported. MCP calls may omit the
`workspace` argument when exactly one workspace is configured or one entry is
marked `default = true`.

## Registering the MCP server

Register the server with Claude Code:

```sh
claude mcp add vivado -- uv run --directory /absolute/path/to/vivado-mcp vmcp-mcp
```

Other MCP clients can use the same stdio command:

```text
command: uv
args: run --directory /absolute/path/to/vivado-mcp vmcp-mcp
```

On the first tool call, the client builds and deploys the remote agent. Later
calls verify the protocol version and payload hash. If the running agent needs
an upgrade, the server reports that state explicitly instead of closing active
Vivado sessions without authorization.

The remote service uses namespaced host resources under `agent_dir`:
`vmcp-agentd.sock`, `vmcp-agentd.lock`, and `vmcp-agentd.log`. This avoids
collisions with unrelated services that use generic `agentd` names.

## Workspace and path model

`workspace.local` must point to the root of a local Git repository. For each
configured workspace, vivado-mcp maintains three separate areas on the build
server:

- a bare Git repository that receives synchronization commits;
- a source worktree checked out at the exact selected commit;
- a persistent build directory for non-Git artifacts such as `.xpr` files, run
  directories, DCPs, bitstreams, and reports.

Project, source, DCP, and ordinary file paths passed to MCP tools are POSIX-style
paths relative to the workspace. For example:

```text
rtl/top.sv
constraints/top.xdc
build/demo/demo.xpr
build/flows/j_12345678/post_synth.dcp
```

Absolute paths, `..`, backslashes, and symlink escapes are rejected. Paths that
begin with the configured `build` prefix map to the persistent build directory;
all other paths map to the remote source worktree.

File synchronization deliberately has one transport in each direction:

- `sync_push` is the only source-upload path. It captures modified, staged, and
  untracked files through a temporary Git index and a synthetic commit, without
  altering the user's HEAD, branch, or real index. rsync push, SSHFS, and shared
  filesystem source backends are not supported.
- Build artifacts live outside the Git worktree and never enter synchronization
  commits. `sync_pull` uses rsync over SSH to copy explicitly requested remote
  artifacts, including the complete configured build directory when requested.
  It never performs deletions.

The local checkout remains the source of truth. Direct remote source edits are
not part of the normal workflow; edit locally and run `sync_push` again.

## Recommended workflows

### Project mode

```text
host_status
  → sync_status
  → sync_push
  → project_create or project_open
  → sources_add / set_top / set_generics / set_property
  → project_close
  → session_close
  → build
  → job_wait
  → report_utilization / report_timing_summary / report_drc
```

`project_close` closes the project and releases its single-writer lock.
`session_close` terminates the Vivado process and releases its memory. After
editing a project, call both in that order.

### Non-project mode

```text
sync_push
  → flow_run
  → job_wait
  → job_artifacts
  → report_timing_summary
```

`flow_run` reads HDL and XDC files directly. It is well suited to small
experiments, quick synthesis runs, and automation that does not need a
long-lived `.xpr` project.

### Iterative optimization

```text
report_utilization(old_job)
  → report_timing_summary(old_job)
  → modify the design and rebuild
  → report_diff(old_job, new_job, kind="utilization")
  → report_diff(old_job, new_job, kind="timing")
```

## MCP tool reference

The current release exposes 47 MCP tools. In the signatures below, `workspace`
and `tool` may be omitted when the configuration provides an unambiguous
default.

### Host and agent

| Tool | Parameters | Description |
|---|---|---|
| `host_status` | None | Returns the agent version and uptime, host CPU/memory/disk information, configured tool probes, active sessions, active jobs, and job-slot usage. This is the recommended first call. |
| `agent_ensure` | `force: bool = false` | Deploys or upgrades the remote agent. `force=true` restarts the daemon and closes active sessions; durable jobs are unaffected. |

### Vivado sessions and Tcl

| Tool | Parameters | Description |
|---|---|---|
| `session_open` | `tool?: str`, `cwd?: str` | Starts a long-lived tool interpreter and returns a `session_id`. `tool` selects a configured tool version; `cwd` is the remote process working directory and can usually be omitted. |
| `session_list` | None | Lists live sessions, including their `idle`, `busy`, or `dead` state, PID, idle time, command count, and log path. |
| `session_close` | `session_id: str` | Closes a session and releases its memory. Interpreter state that has not been written to disk is lost. |
| `tcl_eval` | `session_id: str`, `script: str`, `timeout_s: float = 120`, `allow_blocking: bool = false` | Executes Tcl in an existing session and returns `rc`, `result`, `log`, `errorinfo`, and elapsed time. Long-running synthesis, implementation, and bitstream commands are rejected by default and should be submitted as jobs. `exit`, `quit`, and `start_gui` are always rejected. |
| `tcl_help` | `session_id: str`, `command: str` | Runs `help <command>` in the active Vivado version and returns its actual syntax and option documentation. |

`tcl_eval` is an intentionally powerful interface with the same Tcl privileges
as the remote user. If arbitrary Tcl execution is unnecessary, set
`security.allow_eval = false`.

### Workspace synchronization and file access

| Tool | Parameters | Description |
|---|---|---|
| `sync_status` | `workspace?: str` | Returns the local Git HEAD and dirty-file list together with the exact commit currently active on the build server. |
| `sync_push` | `workspace?: str`, `paths?: list[str]`, `dry_run: bool = false` | Synchronizes the local worktree to the build server. Omitting `paths` captures all changes; providing it limits the overlay on HEAD to those paths. `dry_run=true` reports status only. The user's branch and staging area are not modified. |
| `sync_pull` | `paths: list[str]`, `workspace?: str` | Pulls selected remote artifacts or directories into the local workspace using rsync over SSH. Passing the configured build directory pulls the complete build tree. Paths must be workspace-relative, and `--delete` is never used. |
| `file_read` | `path: str`, `workspace?: str`, `offset: int = 0`, `limit: int = 12000` | Reads a bounded UTF-8 segment from a remote workspace file. Returns `next_offset`, total file size, and truncation status. Each read is limited to 64 KiB. |
| `file_grep` | `pattern: str`, `workspace?: str`, `glob: str = "**/*"`, `limit: int = 200` | Searches the remote source and build trees with a regular expression and returns relative paths, line numbers, and matching text. Results are capped at 1,000 entries, and the number of scanned files is bounded. |

### Vivado projects, sources, and IP

| Tool | Parameters | Description |
|---|---|---|
| `project_create` | `path: str`, `part?: str`, `board?: str`, `top?: str`, `target_language: str = "Verilog"`, `workspace?: str`, `tool?: str` | Creates and opens an `.xpr` inside the workspace, returning the `session_id` that holds the project lock. Exactly one of `part` or `board` is required. `target_language` accepts `Verilog` or `VHDL`. |
| `project_open` | `path: str`, `workspace?: str`, `tool?: str` | Opens an existing `.xpr` in a new session and acquires its single-writer lock. The call is rejected if another session owns the project. |
| `project_close` | `session_id: str` | Closes the current project and releases its single-writer lock without terminating the Vivado session. |
| `project_info` | `session_id: str`, `workspace?: str` | Returns the project path, part, board, top, target language, source inventory, run status, IP inventory, and residual lock information. File paths are converted to workspace-relative paths. |
| `sources_add` | `session_id: str`, `files: list[str]`, `workspace?: str`, `type: str = "auto"`, `library?: str`, `scoped_to?: str` | Adds HDL, constraint, or data files to the current project. Vivado `FILE_TYPE`, library, and `SCOPED_TO_REF` values may be set explicitly. |
| `sources_remove` | `session_id: str`, `files: list[str]`, `workspace?: str` | Removes file references from the project without deleting the files from disk. |
| `sources_list` | `session_id: str`, `workspace?: str` | Lists files registered with the current project, including their type and library. |
| `set_top` | `session_id: str`, `top: str` | Sets the top-level module or entity for the current source fileset. |
| `set_generics` | `session_id: str`, `generics: dict[str, str]` | Sets generic or parameter overrides on the current fileset. |
| `add_include_dirs` | `session_id: str`, `directories: list[str]`, `workspace?: str` | Appends workspace-relative directories to the Verilog/SystemVerilog include path. |
| `set_run_strategy` | `session_id: str`, `run: str`, `strategy: str` | Sets the Vivado strategy for a named synthesis or implementation run. |
| `set_property` | `session_id: str`, `object: str`, `name: str`, `value: str`, `workspace?: str` | Sets one Vivado property on a typed object. `object` accepts `project`, `fileset`, `run:<name>`, `ip:<name>`, or `file:<path>`. Values for Tcl pre/post hooks are resolved as workspace paths. |
| `ip_create` | `session_id: str`, `module_name: str`, `vlnv: str`, `config?: dict[str, str]` | Creates a Vivado IP instance and optionally sets `CONFIG.*` properties. The `CONFIG.` prefix is added automatically when omitted. |
| `ip_upgrade` | `session_id: str`, `ips: list[str]` | Upgrades selected IP instances to the current catalog version. |
| `ip_generate` | `session_id: str`, `ips: list[str]` | Runs `generate_target all` for the selected IP instances. |
| `flow_run` | `sources: list[str]`, `part: str`, `top: str`, `xdc?: list[str]`, `target: str = "synth"`, `workspace?: str`, `tool?: str`, `idempotency_key?: str`, `timeout_s: float = 0` | Starts a durable non-project job. `target` accepts `synth`, `impl`, or `bitstream`; outputs are written under `build/flows/<job_id>`. `timeout_s=0` disables the wall-clock timeout. |
| `project_unlock` | `path: str`, `workspace?: str` | Removes stale Vivado `.lck` files left after a process crash. The operation is rejected while any active session owns the project. |

The complete Block Design Tcl interface remains available through `tcl_eval`.
Potentially long-running commands should still be submitted as jobs rather than
blocking an interactive interpreter.

### Build jobs and observability

| Tool | Parameters | Description |
|---|---|---|
| `build` | `project: str`, `target: str = "bitstream"`, `run?: str`, `jobs?: int`, `reset: bool = false`, `strategy?: str`, `idempotency_key?: str`, `tool?: str`, `timeout_s: float = 0`, `workspace?: str` | Starts an independent durable job for an existing `.xpr` and returns its `job_id` immediately. `target` accepts `synth`, `impl`, `bitstream`, or `flow`; non-project builds should normally use `flow_run`. `jobs` defaults to the host configuration, and `timeout_s=0` disables the wall-clock timeout. |
| `job_list` | `limit: int = 100` | Lists durable jobs in reverse creation order. |
| `job_status` | `job_id: str` | Returns job state, the current step and phase, a monotonic progress estimate, elapsed time, message counts, the log tail, and the latest event sequence number. |
| `job_events` | `job_id: str`, `since_seq: int = 0`, `limit: int = 200` | Reads events after `since_seq` from the append-only event stream. Use the returned `last_seq` as the next cursor to avoid duplicates or gaps. |
| `job_wait` | `job_id: str`, `timeout_s: float = 600`, `since_seq?: int` | Long-polls a job until it reaches a terminal state, emits an important event, or times out. Each call is limited to 3,600 seconds and uses MCP progress notifications to remain visible to the client. |
| `job_cancel` | `job_id: str` | Cancels a queued or running job and sends a termination signal to its dedicated process group. Calling it on a terminal job is safe. |
| `job_logs` | `job_id: str`, `tail: int = 100`, `grep?: str` | Returns a bounded job log tail, optionally filtered by a regular expression. It never returns an entire multi-megabyte Vivado log. |
| `messages` | `job_id: str`, `min_severity: str = "warning"` | Aggregates log messages by Vivado message ID and returns the severity, occurrence count, and one example. `min_severity` accepts `warning`, `critical_warning`, `error`, or `fatal`. |
| `job_artifacts` | `job_id: str` | Lists DCP, bitstream, report, XSA, LTX, and related job artifacts. Workspace-backed jobs return workspace-relative paths. |

Job states follow this lifecycle:

```text
queued → starting → running → succeeded
                           ↘ failed
                           ↘ cancelled
                           ↘ timeout
                           ↘ oom
                           ↘ lost
```

`lost` means the daemon cannot determine the process's final outcome; it is not
reported as an ordinary failure. Use an `idempotency_key` when retrying a job
submission whose response may have been lost to a network interruption. A
repeated key returns the existing job instead of launching another expensive
build.

### Reports and analysis

The `source` accepted by report tools may be a `job_id` or a workspace-relative
DCP path. Jobs emit a standard report bundle when they finish. When only a DCP
is available, the agent reuses a read-only Vivado session to generate missing
reports on demand. Normalized results are cached by DCP SHA-256, report type,
and arguments.

| Tool | Parameters | Description |
|---|---|---|
| `report_utilization` | `source: str`, `hierarchical: bool = false`, `cells?: list[str]`, `workspace?: str`, `tool?: str` | Returns used, available, and percentage values for LUT, LUTRAM, FF, BRAM, URAM, DSP, IO, and BUFG resources. Zero-use resources are omitted by default. Hierarchical output and cell filters are supported. |
| `report_timing_summary` | `source: str`, `max_paths: int = 5`, `workspace?: str`, `tool?: str` | Returns WNS, TNS, WHS, THS, pulse-width metrics, failing endpoint counts, per-clock-domain statistics, clock frequencies, and compact worst-path summaries. `max_paths` must be between 1 and 20. |
| `report_timing_paths` | `source: str`, `from_endpoint?: str`, `to_endpoint?: str`, `through?: str`, `nworst: int = 5`, `delay_type: str = "max"`, `detail: str = "summary"`, `workspace?: str`, `tool?: str` | Queries targeted timing paths. Endpoint arguments use Vivado object-matching patterns. `delay_type` accepts `max` or `min`; `detail` accepts `summary` or `full`. To bound response size, `detail="full"` requires `nworst=1`. |
| `report_clocks` | `source: str`, `workspace?: str`, `tool?: str` | Returns clock periods, target frequencies, WNS-derived achieved frequencies, and timing metrics for each clock domain. |
| `report_drc` | `source: str`, `workspace?: str`, `tool?: str` | Aggregates DRC violations by rule and returns the rule, severity, count, description, and one example. |
| `report_methodology` | `source: str`, `workspace?: str`, `tool?: str` | Aggregates Vivado methodology findings by rule without returning the full report. |
| `report_cdc` | `source: str`, `workspace?: str`, `tool?: str` | Aggregates clock-domain crossing findings by rule and severity. |
| `report_power` | `source: str`, `workspace?: str`, `tool?: str` | Returns total on-chip, dynamic, and static power, junction temperature, confidence, and component-level power data. |
| `report_diff` | `a: str`, `b: str`, `kind: str`, `workspace?: str`, `tool?: str` | Compares normalized reports from two jobs or DCPs. `kind` accepts `utilization` or `timing` and returns only semantic resource or timing deltas. DCP paths must belong to the same workspace. |

## Reliability and runtime semantics

vivado-mcp deliberately separates interactive sessions from long-running
builds:

- Sessions handle fast Tcl queries and project editing. They survive SSH
  reconnections, but a daemon restart closes them.
- Jobs run in independent process groups with state and events persisted to
  disk. They survive SSH disconnections and daemon restarts.
- Job state, events, and JSON indexes are written atomically. Process identity
  is verified with both the PID and Linux process start time to avoid mistakes
  caused by PID reuse.
- Build artifacts and reports remain in the persistent remote build directory;
  source synchronization does not clear that directory.
- Large logs and reports are always returned in bounded, paginated, or
  structured form to protect the MCP context window.

## Security considerations

- Network access inherits the authentication and authorization policy of SSH;
  the agent does not listen on a TCP port.
- Workspace tools reject absolute paths, parent-directory traversal,
  backslashes, and out-of-bounds symlinks.
- Only one active write session may own a project at any time.
- `sync_pull` never deletes remote or local files.
- `tcl_eval` is equivalent to executing Vivado Tcl as the SSH user. When MCP
  callers are not fully trusted, set `security.allow_eval = false` and apply the
  principle of least privilege to the remote account.
- `agent_ensure(force=true)` closes active sessions. Check `host_status` or
  `session_list` first to understand the impact. Running durable jobs are not
  terminated.

## Supported scope

The public interface currently covers project and non-project Vivado flows,
including synthesis, implementation, bitstream generation, IP management, and
static report analysis. Vitis Unified, Vitis HLS, and hardware programming are
not yet exposed through MCP tools.

## Regression tests

Run the local regression suite with:

```sh
uv run pytest
```

The suite exercises the public `vmcp` CLI parser and every subcommand with a
recording transport, plus configuration, workspace path safety, Tcl command
guards, build-script generation, progress parsing, and report diffs.

Remote CLI tests are opt-in. Point `VMCP_TEST_CONFIG` at a normal vivado-mcp
configuration; the SSH target and credentials are read exclusively from that
file and are not embedded in the tests. `VMCP_TEST_TOOL` may select a non-default
tool entry:

```sh
VMCP_TEST_CONFIG=/path/to/test-config.toml \
VMCP_TEST_TOOL=vivado-2024.2 \
uv run pytest -m remote_cli
```

These end-to-end checks run `status`, session/job listing, and a complete
open/eval/list/close Vivado session lifecycle. Tests marked `remote_cli` skip
automatically when `VMCP_TEST_CONFIG` is unset.
