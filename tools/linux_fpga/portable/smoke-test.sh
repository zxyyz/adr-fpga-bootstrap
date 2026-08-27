#!/usr/bin/env bash
set -euo pipefail

readonly image="${OFFICE_VIVADO_IMAGE:-localhost/office-vivado-2024.1:ubuntu22.04}"
require_license=0
require_cmac=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --require-license) require_license=1 ;;
        --require-cmac) require_cmac=1 ;;
        -h|--help)
            echo "usage: bash smoke-test.sh [--require-license] [--require-cmac]"
            exit 0
            ;;
        *) echo "ADR-EDA-BOOTSTRAP-SMOKE-ARGUMENT: unknown option '$1'" >&2; exit 64 ;;
    esac
    shift
done

for command in podman office-vivado office-xilinx-tool python3; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "ADR-EDA-BOOTSTRAP-SMOKE-COMMAND: '$command' is unavailable" >&2
        exit 127
    }
done
podman image exists "$image" || {
    echo "ADR-EDA-BOOTSTRAP-SMOKE-IMAGE: '$image' is unavailable" >&2
    exit 66
}
podman run --rm --network none "$image" bash -lc \
    '. /etc/os-release; test "$ID" = ubuntu && test "$VERSION_ID" = 22.04'

python3 - <<'PY' || {
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
    echo "ADR-EDA-BOOTSTRAP-SMOKE-MCP-PYTHON: host Python 3.12 or newer is required" >&2
    exit 69
}

script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ -d "$script_root/smoke" ]]; then
    smoke_source="$script_root/smoke"
elif [[ -d "$script_root/../smoke" ]]; then
    smoke_source="$(cd "$script_root/../smoke" && pwd -P)"
else
    echo "ADR-EDA-BOOTSTRAP-SMOKE-PAYLOAD: smoke sources are missing" >&2
    exit 66
fi

scratch="$(mktemp -d -t adr-fpga-smoke.XXXXXXXX)"
trap 'rm -rf -- "$scratch"' EXIT
install -m 0644 "$smoke_source/verify_device.tcl" "$scratch/verify_device.tcl"
install -m 0644 "$smoke_source/xsct_version.tcl" "$scratch/xsct_version.tcl"
(
    cd "$scratch"
    OFFICE_VIVADO_NETWORK=none office-vivado -mode batch -nolog -nojournal \
        -source verify_device.tcl
    OFFICE_XILINX_NETWORK=none office-xilinx-tool xsct xsct_version.tcl
)

license_dir="${OFFICE_XILINX_LICENSE_DIR:-$HOME/.Xilinx}"
if [[ $require_license -eq 1 ]]; then
    find "$license_dir" -maxdepth 1 -type f -name '*.lic' -print -quit | grep -q . || {
        echo "ADR-EDA-BOOTSTRAP-SMOKE-LICENSE: no external .lic file is installed" >&2
        exit 78
    }
    install -m 0644 "$smoke_source/top.sv" "$scratch/top.sv"
    install -m 0644 "$smoke_source/synth_smoke.tcl" "$scratch/synth_smoke.tcl"
    (
        cd "$scratch"
        office-vivado -mode batch -nolog -nojournal -source synth_smoke.tcl
    )
fi

if [[ $require_cmac -eq 1 ]]; then
    grep -hEq '^(FEATURE|INCREMENT)[[:space:]]+cmac_usplus([[:space:]]|$)' \
        "$license_dir"/*.lic 2>/dev/null || {
        echo "ADR-EDA-BOOTSTRAP-SMOKE-CMAC: cmac_usplus is absent from the installed licenses" >&2
        exit 78
    }
fi

[[ -r /usr/local/libexec/office-vivado-mcp/settings64.sh ]] || {
    echo "ADR-EDA-BOOTSTRAP-SMOKE-MCP-SETTINGS: MCP settings are missing" >&2
    exit 66
}
bash -c 'source /usr/local/libexec/office-vivado-mcp/settings64.sh; command -v vivado >/dev/null'
if [[ -x "$HOME/.vivado-mcp/bin/vmcp-agent.pyz" ]]; then
    "$HOME/.vivado-mcp/bin/vmcp-agent.pyz" info >/dev/null
fi

echo "ADR_BOOTSTRAP_SMOKE_OK=1"
echo "ADR_BOOTSTRAP_IMAGE=$image"
echo "ADR_BOOTSTRAP_DEVICE=xczu47dr-ffve1156-2-i"

