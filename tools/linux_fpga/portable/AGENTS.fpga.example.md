# Linux FPGA agent rules

- Run all Vivado, Vitis, XSim, XSCT, synthesis, implementation, simulation,
  bitstream, and XSA tasks on the configured Linux FPGA host. Do not compile
  FPGA designs on Windows.
- Use the configured Vivado MCP server for focused inspection, Tcl diagnostics,
  report retrieval, and detached EDA jobs. Use the project runner when its
  reproducible artifact contract is required.
- Before a mutation, verify the remote hostname, user, Vivado version, target
  part, workspace, and source identity. Preserve unrelated changes.
- Keep licenses outside the repository and container image. The default private
  paths are `~/.Xilinx/Xilinx.lic` and `~/.Xilinx/cmac_usplus.lic`; mount them
  read-only through the supplied wrappers.
- For long-running EDA tasks such as Vivado synthesis, implementation,
  simulation, or bitstream generation, do not repeatedly poll the process with
  short intervals. Submit a detached MCP job and use `job_wait` when available.
  Otherwise wait on the attached terminal using the longest practical timeout.
  Resume work immediately when the process exits.
- Reconnect to an existing job by ID after a client or SSH interruption. Do not
  submit a duplicate while the earlier job state is unknown.
- Store useful logs, reports, DCP, bitstream, XSA, and manifests under the
  project output directory or its declared artifact store.
- Do not execute JTAG programming, write QSPI, or enable physical RF TX unless
  the active project instructions explicitly authorize that hardware stage.

