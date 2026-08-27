# ADR FPGA 2024.1 轻量引导包

该包不携带约 54 GB 的 AMD 安装目录，也不包含许可证、AMD 登录令牌、SSH
密钥、工程源码或历史制品。目标机通过网络重新构建 Ubuntu 22.04 Podman
运行层，并由 AMD 2024.1 Web Installer 下载 Vivado/Vitis 与 Zynq UltraScale+
MPSoC/RFSoC 器件支持。

## 目标机要求

- x86_64 Linux；推荐 Ubuntu 24.04 或更新版本作为宿主。
- Podman 容器内固定使用 Ubuntu 22.04，因此 Vivado 不依赖宿主发行版。
- 宿主 Python 3.12 或更新版本，用于 `vivado-mcp` 远端 agent。
- 建议至少 120 GiB 可用空间；安装阶段低于 70 GiB 会拒绝继续。
- 能访问 Ubuntu 软件源和 AMD 下载服务。

## 安装

1. 从 AMD 下载下列固定版本的 Linux Web Installer：
   `FPGAs_AdaptiveSoCs_Unified_2024.1_0522_2023_Lin64.bin`。
2. 为目标 Linux 主机的 Host ID 生成 Vivado 许可证及
   `UltraScale+ Integrated 100G Ethernet` 许可证。
3. 校验解压后的 `SHA256SUMS`，然后运行：

```bash
sha256sum --check SHA256SUMS
bash install-host.sh \
  --installer /path/to/FPGAs_AdaptiveSoCs_Unified_2024.1_0522_2023_Lin64.bin \
  --accept-amd-eulas \
  --license /path/to/Xilinx.lic \
  --cmac-license /path/to/cmac_usplus.lic
```

AMD 账号密码只会在官方 `xsetup -b AuthTokenGen` 的交互终端内输入。安装脚本
不会读取、保存或记录密码。官方生成的短期下载 token 保存在目标用户私有目录
`~/.amd-xilinx-installer`。

许可证安装后的固定外置路径为：

- Vivado/RFSoC：`~/.Xilinx/Xilinx.lic`
- 100G CMAC：`~/.Xilinx/cmac_usplus.lic`

两个文件均为 `0600`，不会进入容器镜像、工程目录或共享产物。若使用浮动
许可证，可在启动 wrapper 前设置 `XILINXD_LICENSE_FILE=端口@服务器`；也可以
用 `OFFICE_XILINX_LICENSE_DIR` 指向另一个只包含 `.lic` 的私有目录。

安装完成后会自动验证：Ubuntu 22.04 容器、Vivado/Vitis 2024.1、
`xczu47dr-ffve1156-2-i` 器件、许可综合、CMAC 许可证以及 MCP wrapper。

## MCP 客户端

目标 Linux 主机只需上述 agent 目录和 wrapper；`vivado-mcp` 客户端首次连接时
会通过 SSH 自动上传其匹配的 agent。包内同时提供当前已验证的 MIT 许可
`vivado-mcp` 客户端源码快照。在 Windows/Codex 控制机运行：

```powershell
.\install-codex-client.ps1 `
  -SshTarget USER@BUILD_HOST `
  -WorkspacePath C:\path\to\fpga-project
```

脚本安装客户端依赖、生成 `vivado-mcp` 配置、把 STDIO 服务写入
`~/.codex/config.toml`，并执行一次端到端 `vmcp status`。完整的 AI 部署、诊断、
长任务等待和故障恢复方法见 `AI-ASSISTED-WORKFLOW.md`；复制到新项目的代理规则
见 `AGENTS.fpga.example.md`。

该引导包不会启用 ADR FPGA worker，也不会执行 JTAG、写 QSPI 或打开 RF TX。
