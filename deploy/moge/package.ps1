$ErrorActionPreference = 'Stop'
$projectDirectory = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$runtimeDirectory = Join-Path $projectDirectory 'runtime'
New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
$archivePath = Join-Path $runtimeDirectory ('moge-release-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '.tar.gz')
$files = @('app.py', 'backend.py', 'remote_client.py', 'app_config.json', 'pyproject.toml', 'uv.lock', 'static')
foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectDirectory $file))) {
        throw "Release input missing: $file"
    }
}
& tar -czf $archivePath -C $projectDirectory @files
if ($LASTEXITCODE -ne 0) { throw 'Release archive creation failed' }
[pscustomobject]@{
    Archive = $archivePath
    SHA256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
}
