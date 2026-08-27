[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$SshTarget,
    [Parameter(Mandatory)]
    [string]$WorkspacePath,
    [string]$WorkspaceName = 'ADR',
    [string]$McpServerName = 'vivado-ubuntu-2024-1',
    [string]$InstallDirectory = "$env:USERPROFILE\.codex\mcp\vivado-mcp",
    [string]$VmcpConfigPath = "$env:USERPROFILE\.config\vivado-mcp\config.toml",
    [string]$CodexConfigPath = "$env:USERPROFILE\.codex\config.toml"
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$bundleRoot = $PSScriptRoot
$source = Join-Path $bundleRoot 'client\vivado-mcp'
if (-not (Test-Path -LiteralPath (Join-Path $source 'pyproject.toml') -PathType Leaf)) {
    $repositorySource = Join-Path $bundleRoot '..\..\..\client\vivado-mcp'
    if (Test-Path -LiteralPath (Join-Path $repositorySource 'pyproject.toml') -PathType Leaf) {
        $source = (Resolve-Path -LiteralPath $repositorySource).Path
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $source 'pyproject.toml') -PathType Leaf)) {
    throw "ADR-EDA-MCP-CLIENT-SOURCE: bundled vivado-mcp source is missing from $source"
}
$workspace = (Resolve-Path -LiteralPath $WorkspacePath).Path.Replace('\', '/')
$installParent = Split-Path -Parent $InstallDirectory
New-Item -ItemType Directory -Force -Path $installParent | Out-Null
if (Test-Path -LiteralPath $InstallDirectory) {
    if ((Get-ChildItem -LiteralPath $InstallDirectory -Force | Select-Object -First 1)) {
        throw "ADR-EDA-MCP-CLIENT-DESTINATION: $InstallDirectory already exists and is non-empty"
    }
} else {
    New-Item -ItemType Directory -Path $InstallDirectory | Out-Null
}
Copy-Item -Path (Join-Path $source '*') -Destination $InstallDirectory -Recurse -Force

$uv = (Get-Command uv -ErrorAction Stop).Source
& $uv sync --directory $InstallDirectory
if ($LASTEXITCODE -ne 0) { throw "ADR-EDA-MCP-CLIENT-SYNC: uv sync failed with $LASTEXITCODE" }

function ConvertTo-TomlLiteral([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}

$vmcpParent = Split-Path -Parent $VmcpConfigPath
New-Item -ItemType Directory -Force -Path $vmcpParent | Out-Null
$vmcp = @"
[host]
name = "adr-fpga-builder"
ssh = $(ConvertTo-TomlLiteral $SshTarget)
agent_dir = "~/.vivado-mcp"
max_concurrent_jobs = 1
default_jobs = 16
nice = 0
stall_timeout_s = 900
ssh_options = ["BatchMode=yes", "ServerAliveInterval=15", "ServerAliveCountMax=3"]

[[host.tools]]
label = "vivado-2024.1"
kind = "vivado"
settings_sh = "/usr/local/libexec/office-vivado-mcp/settings64.sh"
default = true

[[workspace]]
name = $(ConvertTo-TomlLiteral $WorkspaceName)
local = $(ConvertTo-TomlLiteral $workspace)
host = "adr-fpga-builder"
build = "build"
default = true

[security]
allow_eval = true
"@
Set-Content -LiteralPath $VmcpConfigPath -Value $vmcp -Encoding utf8NoBOM

$codexParent = Split-Path -Parent $CodexConfigPath
New-Item -ItemType Directory -Force -Path $codexParent | Out-Null
if (-not (Test-Path -LiteralPath $CodexConfigPath)) {
    New-Item -ItemType File -Path $CodexConfigPath | Out-Null
}
$header = "[mcp_servers.$McpServerName]"
if (Select-String -LiteralPath $CodexConfigPath -SimpleMatch $header -Quiet) {
    throw "ADR-EDA-MCP-CODEX-CONFIG: $header already exists in $CodexConfigPath; update it manually instead of creating a duplicate"
}
Copy-Item -LiteralPath $CodexConfigPath -Destination "$CodexConfigPath.bootstrap-backup" -Force
$snippet = @"

$header
command = $(ConvertTo-TomlLiteral $uv)
args = ["run", "--directory", $(ConvertTo-TomlLiteral $InstallDirectory), "vmcp-mcp"]
startup_timeout_sec = 60
tool_timeout_sec = 3600
enabled = true

[mcp_servers.$McpServerName.env]
VMCP_CONFIG = $(ConvertTo-TomlLiteral $VmcpConfigPath)
"@
Add-Content -LiteralPath $CodexConfigPath -Value $snippet -Encoding utf8NoBOM

& $uv run --directory $InstallDirectory vmcp status
if ($LASTEXITCODE -ne 0) { throw "ADR-EDA-MCP-CLIENT-STATUS: end-to-end status failed with $LASTEXITCODE" }
Write-Output "Vivado MCP configured. Restart Codex to load $McpServerName."
