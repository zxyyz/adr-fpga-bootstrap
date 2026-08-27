#!/usr/bin/env bash
set -euo pipefail

readonly image="localhost/office-vivado-2024.1:ubuntu22.04"
readonly xilinx_root="/opt/amd/Xilinx"
readonly runtime_root="/srv/interop/fpga/runtime"
readonly installer_root="$runtime_root/installers/unified-2024.1-web"
readonly expected_installer_sha256="9a04ad206be0d9afd9d11cd7997b4e6978485eee44f47d4c08d07dbc30cb2f1e"

installer=""
license_file=""
cmac_license_file=""
accept_eulas=0
force_tool_install=0
skip_smoke=0
allow_license_pending=0

usage() {
    cat <<'EOF'
usage: bash install-host.sh --installer PATH --accept-amd-eulas \
    --license PATH --cmac-license PATH [OPTION]

Install the ADR Ubuntu FPGA execution environment. The AMD web installer and
licenses are user-supplied and are never copied into the portable bundle.

Options:
  --installer PATH          AMD 2024.1 Linux web installer .bin
  --license PATH            node-locked or certificate Xilinx .lic
  --cmac-license PATH       UltraScale+ Integrated 100G Ethernet .lic
  --accept-amd-eulas        confirm acceptance of XilinxEULA and 3rdPartyEULA
  --force-tool-install      rerun AMD installation even when Vivado exists
  --allow-license-pending   install tools but do not require a usable license
  --skip-smoke              do not run the final environment smoke test
  -h, --help                show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --installer)
            [[ $# -ge 2 ]] || { usage >&2; exit 64; }
            installer="$2"
            shift 2
            ;;
        --license)
            [[ $# -ge 2 ]] || { usage >&2; exit 64; }
            license_file="$2"
            shift 2
            ;;
        --cmac-license)
            [[ $# -ge 2 ]] || { usage >&2; exit 64; }
            cmac_license_file="$2"
            shift 2
            ;;
        --accept-amd-eulas) accept_eulas=1; shift ;;
        --force-tool-install) force_tool_install=1; shift ;;
        --allow-license-pending) allow_license_pending=1; shift ;;
        --skip-smoke) skip_smoke=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ADR-EDA-BOOTSTRAP-ARGUMENT: unknown option '$1'" >&2; usage >&2; exit 64 ;;
    esac
done

script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ -f "$script_root/payload/tools/linux_fpga/Containerfile.ubuntu22.04" ]]; then
    payload_root="$script_root/payload/tools/linux_fpga"
elif [[ -f "$script_root/../Containerfile.ubuntu22.04" ]]; then
    # Allows maintainers to exercise the script directly from the repository.
    payload_root="$(cd "$script_root/.." && pwd -P)"
else
    echo "ADR-EDA-BOOTSTRAP-PAYLOAD: tools/linux_fpga payload is missing" >&2
    exit 66
fi

[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || {
    echo "ADR-EDA-BOOTSTRAP-PLATFORM: an x86_64 Linux host is required" >&2
    exit 69
}

host_user="$(id -un)"
host_group="$(id -gn)"
run_root() {
    if [[ $(id -u) -eq 0 ]]; then
        "$@"
    else
        command -v sudo >/dev/null 2>&1 || {
            echo "ADR-EDA-BOOTSTRAP-SUDO: sudo is required for host installation" >&2
            return 77
        }
        sudo "$@"
    fi
}

if ! command -v podman >/dev/null 2>&1; then
    command -v apt-get >/dev/null 2>&1 || {
        echo "ADR-EDA-BOOTSTRAP-PODMAN: install Podman, uidmap, slirp4netns, and fuse-overlayfs first" >&2
        exit 69
    }
    run_root apt-get update
    run_root apt-get install -y podman uidmap slirp4netns fuse-overlayfs ca-certificates
fi
podman info >/dev/null

python3 - <<'PY' || {
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
    echo "ADR-EDA-BOOTSTRAP-MCP-PYTHON: Vivado MCP requires host Python 3.12 or newer; the Ubuntu 22.04 FPGA userland remains inside Podman" >&2
    exit 69
}

run_root install -d -o "$host_user" -g "$host_group" -m 0755 "$xilinx_root"
run_root install -d -o "$host_user" -g "$host_group" -m 0770 \
    /srv/interop/fpga/jobs /srv/interop/fpga/artifacts "$runtime_root" \
    "$runtime_root/installers"
install -d -m 0700 "$HOME/.Xilinx" "$HOME/.amd-xilinx-installer/.Xilinx" \
    "$HOME/.vivado-mcp"

for wrapper in office-vivado office-xilinx-tool office-vivado-runner \
    office-xilinx-auth-token office-xilinx-install-2024.1 office-vivado-mcp; do
    [[ -f "$payload_root/$wrapper" ]] || {
        echo "ADR-EDA-BOOTSTRAP-PAYLOAD: missing $payload_root/$wrapper" >&2
        exit 66
    }
    run_root install -o root -g root -m 0755 "$payload_root/$wrapper" "/usr/local/bin/$wrapper"
done
run_root install -d -o root -g root -m 0755 /usr/local/libexec/office-vivado-mcp/bin
run_root install -o root -g root -m 0755 "$payload_root/office-vivado-mcp" \
    /usr/local/libexec/office-vivado-mcp/bin/vivado
run_root install -o root -g root -m 0644 "$payload_root/office-vivado-mcp-init.tcl" \
    /usr/local/libexec/office-vivado-mcp/Vivado_init.tcl
run_root install -o root -g root -m 0644 "$payload_root/office-vivado-mcp-settings64.sh" \
    /usr/local/libexec/office-vivado-mcp/settings64.sh
install -m 0644 "$payload_root/install_config.2024.1.txt" \
    "$runtime_root/install_config.2024.1.txt"

podman build --tag "$image" --file "$payload_root/Containerfile.ubuntu22.04" "$payload_root"

if [[ -z "$installer" && -d "$script_root/downloads" ]]; then
    mapfile -d '' bundled_installers < <(
        find "$script_root/downloads" -maxdepth 1 -type f \
            -name 'FPGAs_AdaptiveSoCs_Unified_2024.1_*_Lin64.bin' -print0
    )
    if [[ ${#bundled_installers[@]} -eq 1 ]]; then
        installer="${bundled_installers[0]}"
    fi
fi

if [[ $force_tool_install -eq 1 || ! -x "$xilinx_root/Vivado/2024.1/bin/vivado" ]]; then
    [[ -n "$installer" && -f "$installer" ]] || {
        echo "ADR-EDA-BOOTSTRAP-INSTALLER: download the AMD 2024.1 Linux web installer and pass --installer PATH" >&2
        exit 66
    }
    [[ $accept_eulas -eq 1 ]] || {
        echo "ADR-EDA-BOOTSTRAP-EULA: review the AMD terms and pass --accept-amd-eulas to continue" >&2
        exit 64
    }
    installer="$(realpath -e "$installer")"
    actual_installer_sha256="$(sha256sum "$installer" | awk '{print $1}')"
    [[ "$actual_installer_sha256" == "$expected_installer_sha256" ]] || {
        echo "ADR-EDA-BOOTSTRAP-INSTALLER-HASH: the installer is not the pinned AMD 2024.1 Linux web installer" >&2
        exit 65
    }
    bash "$installer" --check

    install -d -m 0770 "$installer_root"
    if [[ ! -x "$installer_root/xsetup" ]]; then
        if find "$installer_root" -mindepth 1 -print -quit | grep -q .; then
            echo "ADR-EDA-BOOTSTRAP-INSTALLER-PARTIAL: $installer_root is non-empty but has no executable xsetup" >&2
            exit 73
        fi
        bash "$installer" --noexec --target "$installer_root"
    fi
    [[ -s "$HOME/.amd-xilinx-installer/.Xilinx/wi_authentication_key" ]] || \
        /usr/local/bin/office-xilinx-auth-token

    available_bytes="$(df --output=avail -B1 "$xilinx_root" | tail -n 1 | tr -d ' ')"
    if (( available_bytes < 75161927680 )); then
        echo "ADR-EDA-BOOTSTRAP-SPACE: at least 70 GiB free is required before the AMD download/install stage" >&2
        exit 73
    fi
    /usr/local/bin/office-xilinx-install-2024.1
fi

[[ -x "$xilinx_root/Vivado/2024.1/bin/vivado" ]] || {
    echo "ADR-EDA-BOOTSTRAP-VIVADO: Vivado 2024.1 was not installed" >&2
    exit 70
}

install_private_license() {
    local source="$1"
    local destination="$2"
    [[ -f "$source" ]] || {
        echo "ADR-EDA-BOOTSTRAP-LICENSE-SOURCE: missing license file '$source'" >&2
        return 66
    }
    source="$(realpath -e "$source")"
    if [[ "$source" != "$destination" ]]; then
        install -m 0600 "$source" "$destination"
    else
        chmod 0600 "$destination"
    fi
}

[[ -z "$license_file" ]] || install_private_license "$license_file" "$HOME/.Xilinx/Xilinx.lic"
[[ -z "$cmac_license_file" ]] || install_private_license "$cmac_license_file" "$HOME/.Xilinx/cmac_usplus.lic"

mapfile -d '' installed_licenses < <(
    find "$HOME/.Xilinx" -maxdepth 1 -type f -name '*.lic' -print0 | sort -z
)
if [[ ${#installed_licenses[@]} -eq 0 ]]; then
    if [[ $allow_license_pending -eq 0 ]]; then
        echo "ADR-EDA-BOOTSTRAP-LICENSE-PENDING: generate a license for this host and rerun with --license and --cmac-license" >&2
        exit 78
    fi
    echo "ADR-EDA-BOOTSTRAP-LICENSE-PENDING: tools installed; licensed synthesis was not run"
fi

if [[ $skip_smoke -eq 0 ]]; then
    smoke_args=()
    if [[ ${#installed_licenses[@]} -gt 0 ]]; then
        smoke_args+=(--require-license --require-cmac)
    fi
    bash "$script_root/smoke-test.sh" "${smoke_args[@]}"
fi

echo "ADR FPGA host bootstrap complete"
echo "Vivado wrapper: /usr/local/bin/office-vivado"
echo "MCP settings: /usr/local/libexec/office-vivado-mcp/settings64.sh"

