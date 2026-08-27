[CmdletBinding()]
param(
    [string]$OutputDirectory = 'output/fpga/environment',
    [string]$IncludeWebInstaller,
    [string]$VivadoMcpSource
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if (-not $VivadoMcpSource) {
    $bundledMcpSource = Join-Path $repoRoot 'client\vivado-mcp'
    $VivadoMcpSource = if (Test-Path -LiteralPath (Join-Path $bundledMcpSource 'pyproject.toml')) {
        $bundledMcpSource
    } else {
        Join-Path $env:USERPROFILE '.codex\mcp\vivado-mcp'
    }
}
$outputRoot = if ([IO.Path]::IsPathRooted($OutputDirectory)) {
    [IO.Path]::GetFullPath($OutputDirectory)
} else {
    [IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDirectory))
}
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null

$timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$bundleName = "adr-fpga-bootstrap-2024.1-$timestamp"
$stage = Join-Path $outputRoot ".$bundleName.staging"
$archive = Join-Path $outputRoot "$bundleName.zip"
if (Test-Path -LiteralPath $stage) {
    throw "ADR-EDA-BOOTSTRAP-STAGE-EXISTS: $stage"
}

$payloadFiles = @(
    'Containerfile.ubuntu22.04',
    'install_config.2024.1.txt',
    'office-vivado',
    'office-xilinx-tool',
    'office-vivado-runner',
    'office-xilinx-auth-token',
    'office-xilinx-install-2024.1',
    'office-vivado-mcp',
    'office-vivado-mcp-init.tcl',
    'office-vivado-mcp-settings64.sh'
)
$portableFiles = @(
    'install-host.sh',
    'install-codex-client.ps1',
    'smoke-test.sh',
    'vivado-mcp-config.example.toml',
    'AI-ASSISTED-WORKFLOW.md',
    'AGENTS.fpga.example.md',
    'README.md'
)
$expectedInstallerSha256 = '9a04ad206be0d9afd9d11cd7997b4e6978485eee44f47d4c08d07dbc30cb2f1e'

try {
    $payloadRoot = Join-Path $stage 'payload/tools/linux_fpga'
    $portableRoot = Join-Path $PSScriptRoot 'portable'
    New-Item -ItemType Directory -Force -Path $payloadRoot | Out-Null
    foreach ($name in $payloadFiles) {
        $source = Join-Path $PSScriptRoot $name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "ADR-EDA-BOOTSTRAP-PAYLOAD-MISSING: $source"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $payloadRoot $name)
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'smoke') -Destination $payloadRoot -Recurse
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'smoke') -Destination $stage -Recurse
    foreach ($name in $portableFiles) {
        $source = Join-Path $portableRoot $name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "ADR-EDA-BOOTSTRAP-PORTABLE-MISSING: $source"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $stage $name)
    }

    $mcpSourceRoot = (Resolve-Path -LiteralPath $VivadoMcpSource).Path
    foreach ($required in 'pyproject.toml', 'uv.lock', 'README.md', 'LICENSE', 'src') {
        if (-not (Test-Path -LiteralPath (Join-Path $mcpSourceRoot $required))) {
            throw "ADR-EDA-BOOTSTRAP-MCP-SOURCE: missing $required under $mcpSourceRoot"
        }
    }
    $mcpPayloadRoot = Join-Path $stage 'client/vivado-mcp'
    New-Item -ItemType Directory -Force -Path $mcpPayloadRoot | Out-Null
    foreach ($name in 'pyproject.toml', 'uv.lock', 'README.md', 'LICENSE') {
        Copy-Item -LiteralPath (Join-Path $mcpSourceRoot $name) -Destination $mcpPayloadRoot
    }
    Copy-Item -LiteralPath (Join-Path $mcpSourceRoot 'src') -Destination $mcpPayloadRoot -Recurse
    Get-ChildItem -LiteralPath $mcpPayloadRoot -Recurse -Directory -Filter '__pycache__' |
        Sort-Object FullName -Descending |
        Remove-Item -Recurse -Force
    Get-ChildItem -LiteralPath $mcpPayloadRoot -Recurse -File -Filter '*.pyc' |
        Remove-Item -Force

    $installerBundled = $false
    if ($IncludeWebInstaller) {
        $installerPath = (Resolve-Path -LiteralPath $IncludeWebInstaller).Path
        $actual = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expectedInstallerSha256) {
            throw 'ADR-EDA-BOOTSTRAP-INSTALLER-HASH: installer does not match the pinned AMD 2024.1 Linux web installer'
        }
        $downloads = Join-Path $stage 'downloads'
        New-Item -ItemType Directory -Force -Path $downloads | Out-Null
        Copy-Item -LiteralPath $installerPath -Destination (Join-Path $downloads ([IO.Path]::GetFileName($installerPath)))
        $installerBundled = $true
    }

    $sourceCommit = (& git -C $repoRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'ADR-EDA-BOOTSTRAP-GIT: cannot resolve source commit' }
    $dirtyOutput = @(& git -C $repoRoot status --porcelain -- tools/linux_fpga)
    $dirty = $dirtyOutput.Count -gt 0
    $mcpCommit = (& git -C $mcpSourceRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'ADR-EDA-BOOTSTRAP-MCP-GIT: cannot resolve vivado-mcp source commit' }
    $mcpDirtyOutput = @(& git -C $mcpSourceRoot status --porcelain)
    $manifest = [ordered]@{
        schema = 'adr-fpga-bootstrap-v1'
        created_utc = [DateTime]::UtcNow.ToString('o')
        source_commit = $sourceCommit
        linux_fpga_payload_dirty = $dirty
        amd_version = '2024.1'
        required_part = 'xczu47dr-ffve1156-2-i'
        runtime_image = 'localhost/office-vivado-2024.1:ubuntu22.04'
        runtime_image_rebuilt_on_target = $true
        amd_toolchain_downloaded_on_target = $true
        web_installer_bundled = $installerBundled
        expected_web_installer_sha256 = $expectedInstallerSha256
        secrets_bundled = $false
        licenses_bundled = $false
        worker_enabled_by_installer = $false
        hardware_actions = $false
        vivado_mcp_version = '0.4.0'
        vivado_mcp_source_commit = $mcpCommit
        vivado_mcp_source_dirty = ($mcpDirtyOutput.Count -gt 0)
        vivado_mcp_license = 'MIT'
    }
    $manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $stage 'manifest.json') -Encoding utf8NoBOM

    $hashLines = foreach ($file in Get-ChildItem -LiteralPath $stage -Recurse -File | Sort-Object FullName) {
        $relative = [IO.Path]::GetRelativePath($stage, $file.FullName).Replace('\', '/')
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $relative"
    }
    $hashLines | Set-Content -LiteralPath (Join-Path $stage 'SHA256SUMS') -Encoding ascii
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $archive -CompressionLevel Optimal
    $archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    "$archiveHash  $([IO.Path]::GetFileName($archive))" | Set-Content -LiteralPath "$archive.sha256" -Encoding ascii

    [pscustomobject]@{
        Archive = $archive
        Sha256 = $archiveHash
        SizeBytes = (Get-Item -LiteralPath $archive).Length
        IncludesWebInstaller = $installerBundled
    }
} finally {
    if (Test-Path -LiteralPath $stage) {
        $resolvedStage = [IO.Path]::GetFullPath($stage)
        $resolvedOutput = [IO.Path]::GetFullPath($outputRoot).TrimEnd('\') + '\'
        if (-not $resolvedStage.StartsWith($resolvedOutput, [StringComparison]::OrdinalIgnoreCase)) {
            throw "ADR-EDA-BOOTSTRAP-CLEANUP-SCOPE: refusing to remove $resolvedStage"
        }
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
