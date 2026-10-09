param(
    [string]$BackendRoot = 'E:\dev\HYMotion',
    [string]$PythonVersion = '3.11.14',
    [switch]$DownloadModels
)
$ErrorActionPreference = 'Stop'
$uiRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$backend = [IO.Path]::GetFullPath($BackendRoot)
$payload = Join-Path $PSScriptRoot 'backend'

# Reuse uv and its managed Python. Install uv normally before using this script.
$uv = (Get-Command uv -ErrorAction Stop).Source
& $uv --version
& $uv python install $PythonVersion
if ($LASTEXITCODE -ne 0) { throw 'uv-managed Python installation failed' }
& $uv python update-shell
if ($LASTEXITCODE -ne 0) { throw 'Python command-path configuration failed' }
if (-not (Test-Path -LiteralPath (Join-Path $backend 'repo\hymotion\pipeline\motion_diffusion.py'))) {
    throw 'Official HY-Motion source is absent. Extract the reviewed source bundle or clone the pinned upstream repo into HYMotion\repo first.'
}
if (Test-Path -LiteralPath (Join-Path $backend '.venv\pyvenv.cfg')) {
    $existingProject = Join-Path $backend 'pyproject.toml'
    if (-not (Test-Path -LiteralPath $existingProject) -or
        -not (Select-String -LiteralPath $existingProject -Pattern '^name = "hy-motion-staged-gpu"$' -Quiet)) {
        throw 'An existing non-GPU backend environment was found. Use a separate new backend folder instead of replacing it.'
    }
}
# Only the reviewed adapter files are copied; weights, caches and outputs stay in place.
Get-ChildItem -LiteralPath $payload -Recurse -File | Where-Object {
    $_.FullName -notmatch '[\\/](__pycache__|\.venv)[\\/]' -and $_.Extension -ne '.pyc'
} | ForEach-Object {
    $relative = $_.FullName.Substring($payload.Length).TrimStart('\', '/')
    $destination = Join-Path $backend $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
}
& $uv sync --project $backend --python $PythonVersion --frozen --no-dev
if ($LASTEXITCODE -ne 0) { throw 'HY-Motion CUDA environment installation failed' }
& $uv sync --project $uiRoot --python $PythonVersion --frozen --no-dev
if ($LASTEXITCODE -ne 0) { throw 'HY-Motion WebUI environment installation failed' }
$python = Join-Path $backend '.venv\Scripts\python.exe'
if ($DownloadModels) {
    & $python (Join-Path $backend 'scripts\download_official_models.py')
    if ($LASTEXITCODE -ne 0) { throw 'Official model download/verification failed' }
}
& $python (Join-Path $backend 'src\hy_motion_cpu\cli.py') --check --device cuda:0
if ($LASTEXITCODE -ne 0) { throw 'CUDA or model installation check failed' }
$settings = @{schema=1; backend_root=$backend} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $uiRoot '.gpu-settings.json'), $settings, (New-Object Text.UTF8Encoding($false)))
Write-Output 'GPU environment and assets checked. Run a real generation before claiming inference success.'
Write-Output ('Launch: ' + (Join-Path $uiRoot 'Start-GPU.cmd'))
