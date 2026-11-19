# Packaged for future Windows qualification; setup currently rejects Windows.
$ErrorActionPreference = 'Stop'
try {
  $node = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'node-path')).TrimEnd("`r", "`n")
  $payload = [Console]::In.ReadToEnd()
  $payload | & $node (Join-Path $PSScriptRoot '../runtime/hook.cjs') (Join-Path $PSScriptRoot 'registration.json') *> $null
} catch {}
exit 0
