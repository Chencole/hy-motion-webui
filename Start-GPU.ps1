$ErrorActionPreference = 'Stop'
$settingsPath = Join-Path $PSScriptRoot '.gpu-settings.json'
if (-not $env:MOTION_BACKEND_ROOT -and (Test-Path -LiteralPath $settingsPath)) {
    $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($settings.schema -ne 1 -or -not ($settings.backend_root -is [string]) -or -not $settings.backend_root.Trim()) {
        throw 'Invalid .gpu-settings.json; rerun the GPU installer or set MOTION_BACKEND_ROOT.'
    }
    $env:MOTION_BACKEND_ROOT = $settings.backend_root
}
if (-not $env:MOTION_BACKEND_ROOT) { $env:MOTION_BACKEND_ROOT = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\HYMotion')) }
$env:MOTION_BACKEND = 'local'
$env:MOTION_DEVICE = 'cuda:0'
Set-Location -LiteralPath $PSScriptRoot
& uv run --python 3.11 --frozen python (Join-Path $PSScriptRoot 'app.py') @args
exit $LASTEXITCODE
