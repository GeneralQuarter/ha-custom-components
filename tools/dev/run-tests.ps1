#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Run the Jackery BLE test suite in the local Linux-free (Windows) setup.

.DESCRIPTION
    Home Assistant targets Linux. The shims in tools/dev/windows_shims satisfy
    the handful of Unix-only imports the test harness performs at load time.
    This script wires them in and runs pytest from the repository venv.

.EXAMPLE
    .\tools\dev\run-tests.ps1
    .\tools\dev\run-tests.ps1 tests/test_protocol.py -k encryption
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $PytestArgs = @("tests")
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$shims = Join-Path $repoRoot "tools\dev\windows_shims"

if (-not (Test-Path $python)) {
    throw "No venv at $python. See tools/dev/README.md for the setup commands."
}

if ($PytestArgs.Count -eq 0) {
    $PytestArgs = @("tests")
}

$env:PYTHONPATH = $shims

Push-Location $repoRoot
try {
    & $python -m pytest @PytestArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
