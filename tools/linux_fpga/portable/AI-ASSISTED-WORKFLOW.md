# AI 辅助部署与 Vivado 调试

## 1. 职责边界

- 目标 Linux 主机负责 Podman、Vivado/Vitis 2024.1、XSim、XSCT 和产物生成。
- Codex/AI 控制机通过 SSH、项目 runner 或 `vivado-mcp` 操作目标主机。
- AMD 密码只在官方 `xsetup -b AuthTokenGen` 交互终端输入。
- AI 可以执行环境安装、版本检查、MCP 配置、仿真、综合、实现和报告诊断；
  用户负责下载受许可约束的 AMD Web Installer，并为目标 Host ID 申请许可证。
- 默认不执行 JTAG、QSPI 或物理 RF TX；这些步骤需要项目自己的硬件门禁。

## 2. AI 辅助安装顺序

1. AI 核对 `hostname`、`id -un`、`uname -m`、磁盘空间和目标路径。
2. 用户把 AMD 2024.1 Web Installer 和目标机许可证放到任意临时目录。
3. AI 运行 `install-host.sh`。脚本遇到 AMD 登录时保持交互终端，由用户输入密码。
4. 工具链下载完成后，脚本把许可证复制到：
   - `~/.Xilinx/Xilinx.lic`
   - `~/.Xilinx/cmac_usplus.lic`
5. 脚本自动运行许可综合、RFSoC 器件、XSCT、CMAC 和 MCP wrapper 检查。
6. 在 Codex 控制机运行 `install-codex-client.ps1`，然后重启 Codex 客户端。

许可证目录可以通过 `OFFICE_XILINX_LICENSE_DIR` 覆盖。Node-Locked 许可证必须
按目标 Linux 主机 Host ID 生成；复制另一台机器的 `.lic` 不会使新主机可用。

## 3. MCP 验证

```powershell
$vmcp = "$env:USERPROFILE\.codex\mcp\vivado-mcp"
uv run --directory $vmcp vmcp status
uv run --directory $vmcp vmcp open --tool vivado-2024.1
```

`status` 必须显示目标 `hostname`、`vivado-2024.1`、`exists=true` 和无错误。
打开会话后使用返回的 session ID 执行：

```powershell
uv run --directory $vmcp vmcp eval <SESSION_ID> 'version -short'
uv run --directory $vmcp vmcp close <SESSION_ID>
```

预期版本为 `2024.1`。MCP 客户端会把匹配版本的远端 agent 上传到
`~/.vivado-mcp/bin/vmcp-agent.pyz`，不需要手工复制。

## 4. AI 调试方法

优先从低成本、只读或可重复的检查开始：

1. `vmcp status`：确认主机、资源、并发槽和 Vivado wrapper。
2. `session_list` / `job_list`：检查遗留会话或任务，避免重复启动实现。
3. `job_logs` / `job_messages`：读取首错、关键警告和最新有意义日志。
4. `project_inspect` / Tcl `get_*`：检查 part、fileset、IP、run 和约束绑定。
5. `report_get` / `report_diff`：分析时序、利用率、DRC、CDC 和前后版本差异。
6. 修改源码后只运行与变化相关的仿真、综合或实现门禁。

长时间综合、实现和 bitstream 任务应作为 MCP detached job 提交。提交后调用
`job_wait` 阻塞等待，或者在直接 SSH 终端使用最长实用超时；不要每隔几秒读取
进程或日志。连接中断后用同一 job ID 调用 `job_status` 和 `job_wait` 恢复，不要
在状态不明时重复启动同一构建。

## 5. 推荐给 Codex 的任务描述

```text
Use the configured Vivado MCP server and the Linux FPGA execution host for all
Vivado, Vitis, XSim, and XSCT work. Start with status and focused diagnostics.
For long-running synthesis, implementation, simulation, or bitstream jobs,
submit a detached job and wait with job_wait; do not repeatedly poll at short
intervals. Keep licenses external and read-only. Do not perform JTAG, QSPI, or
physical RF operations unless the project instructions explicitly authorize it.
```

Codex 会从 `~/.codex/config.toml` 读取 MCP，并从项目根目录的 `AGENTS.md` 读取
持续工程规则。官方说明见：

- https://learn.chatgpt.com/docs/extend/mcp
- https://learn.chatgpt.com/docs/agent-configuration/agents-md

