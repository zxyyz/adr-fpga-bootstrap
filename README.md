# ADR FPGA Bootstrap

Portable, secret-free bootstrap for running AMD Vivado/Vitis 2024.1 in an
Ubuntu 22.04 Podman userland, with Zynq UltraScale+ RFSoC device support,
external licensing, reproducible runners, and AI-assisted debugging through
Vivado MCP.

The repository does **not** contain the AMD installer, Vivado/Vitis binaries,
license files, AMD authentication tokens, SSH keys, FPGA projects, bitstreams,
or hardware credentials. The destination downloads the official toolchain from
AMD after the user accepts the applicable terms and authenticates directly with
the official installer.

## What it provides

- Ubuntu 22.04 container recipe for Vivado/Vitis 2024.1.
- Pinned install selection for Zynq UltraScale+ MPSoC and RFSoC.
- Rootless Podman wrappers for Vivado, Vitis, XSCT, XSim, and direct jobs.
- External, read-only license mounting, including 100G CMAC.
- A lightweight portable ZIP exporter with SHA-256 manifests.
- A verified `vivado-mcp` 0.4.0 client snapshot and Codex configurator.
- AI deployment/debugging guidance and project `AGENTS.md` rules.
- Smoke tests for the Ubuntu userland, tool versions, RFSoC part, licensed
  synthesis, CMAC license visibility, XSCT, and MCP connectivity.

## Requirements

- x86_64 Linux build host; Ubuntu 24.04 or newer is recommended.
- Host Python 3.12+ for the Vivado MCP remote agent.
- At least 70 GiB free before installation; 120 GiB is recommended.
- Windows PowerShell and `uv` on the Codex/MCP control machine.
- An official AMD 2024.1 Linux Web Installer downloaded by the user.
- Licenses generated for the destination host or a reachable floating server.

The Ubuntu 22.04 compatibility environment runs inside Podman. The host does
not need to run Ubuntu 22.04 itself.

## Quick start

Clone this repository on the control machine:

```powershell
git clone https://github.com/zxyyz/adr-fpga-bootstrap.git
cd adr-fpga-bootstrap
.\tools\linux_fpga\export-adr-fpga-bootstrap.ps1
```

Transfer the generated ZIP to the Linux build host, extract it, and verify its
manifest:

```bash
sha256sum --check SHA256SUMS
bash install-host.sh \
  --installer /path/to/FPGAs_AdaptiveSoCs_Unified_2024.1_0522_2023_Lin64.bin \
  --accept-amd-eulas \
  --license /path/to/Xilinx.lic \
  --cmac-license /path/to/cmac_usplus.lic
```

The installed private license paths are:

- `~/.Xilinx/Xilinx.lic`
- `~/.Xilinx/cmac_usplus.lic`

Both are installed with mode `0600`, remain outside the image and repository,
and are mounted read-only. Set `OFFICE_XILINX_LICENSE_DIR` for another private
directory or `XILINXD_LICENSE_FILE=port@server` for a floating license.

Configure the Windows Codex/MCP client:

```powershell
.\tools\linux_fpga\portable\install-codex-client.ps1 `
  -SshTarget USER@BUILD_HOST `
  -WorkspacePath C:\path\to\fpga-project
```

The script installs the bundled MCP client, creates its SSH/workspace config,
adds the STDIO server to `~/.codex/config.toml`, and verifies `vmcp status`.
Restart Codex after it completes.

## AI-assisted workflows

- [Deployment and debugging runbook](tools/linux_fpga/portable/AI-ASSISTED-WORKFLOW.md)
- [Reusable FPGA agent instructions](tools/linux_fpga/portable/AGENTS.fpga.example.md)
- [Linux host package guide](tools/linux_fpga/portable/README.md)

Long EDA work is submitted as a detached MCP job and awaited with `job_wait`,
or run in a directly attached terminal with a long blocking timeout. The
workflow deliberately avoids short-interval process and log polling.

## Tested baseline

- Ubuntu 22.04 Podman userland on an Ubuntu 26.04 x86_64 host.
- Vivado/Vitis/XSCT 2024.1.
- `xczu47dr-ffve1156-2-i` device discovery and licensed synthesis.
- UltraScale+ Integrated 100G Ethernet license visibility.
- `vivado-mcp` status and Vivado `open -> eval -> close` lifecycle.

No worker service, JTAG programming, QSPI write, or RF transmission is enabled
by the bootstrap installer.

## Licensing and trademarks

Project-authored files are licensed under the MIT License. The vendored
`vivado-mcp` snapshot retains its original MIT license and attribution; see
[NOTICE](NOTICE).

AMD, Xilinx, Vivado, Vitis, Zynq, and related marks are trademarks of their
respective owners. This community project is not affiliated with or endorsed
by AMD. You are responsible for obtaining AMD software from authorized sources
and complying with its license terms.

