param(
    [Parameter(Mandatory=$true)][string]$RequirementsBase64,
    [Parameter(Mandatory=$true)][string]$ExpectedSha
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Join-Path $env:USERPROFILE 'Dev\hermes-health-repair-20261019'
$out = Join-Path $root 'artifacts\production-dependency-audit-20261020-registry'
if (Test-Path (Join-Path $out 'audit-result.json')) { throw 'Read the existing audit before repeating it' }
[IO.Directory]::CreateDirectory($out) | Out-Null
$compressed = [IO.MemoryStream]::new([Convert]::FromBase64String($RequirementsBase64))
$gzip = [IO.Compression.GzipStream]::new($compressed, [IO.Compression.CompressionMode]::Decompress)
$decoded = [IO.MemoryStream]::new()
try { $gzip.CopyTo($decoded) } finally { $gzip.Dispose(); $compressed.Dispose() }
$requirements = Join-Path $out 'requirements.txt'
[IO.File]::WriteAllBytes($requirements, $decoded.ToArray())
$decoded.Dispose()
if ((Get-FileHash $requirements -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedSha) { throw 'Inspect the production package inventory identity' }
$uv = (Get-Command uv.exe).Source
$python = (Get-Command python.exe).Source
$env:UV_CACHE_DIR = Join-Path $out 'uv-cache'
$env:UV_TOOL_DIR = Join-Path $out 'uv-tools'
$resultPath = Join-Path $out 'audit.json'
$arguments = @('tool','run','--python',('"' + $python + '"'),'--from','pip-audit==2.10.1','pip-audit','--requirement',('"' + $requirements + '"'),
    '--no-deps','--disable-pip','--strict','--format','json','--desc','on','--aliases','on',
    '--progress-spinner','off','--timeout','30','--output',('"' + $resultPath + '"'))
$process = Start-Process -FilePath $uv -ArgumentList $arguments -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $out 'worker.stdout') -RedirectStandardError (Join-Path $out 'worker.stderr')
if (-not (Test-Path $resultPath)) { throw 'Read the real dependency-audit setup error' }
if ($process.ExitCode -notin @(0,1)) { throw 'Read the failed native dependency audit' }
$audit = [IO.File]::ReadAllText($resultPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
$record = @{hostname=$env:COMPUTERNAME; python=$python; auditor='pip-audit==2.10.1'; requirements_sha256=$ExpectedSha;
    helper_sha256=(Get-FileHash $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant();
    exit_code=$process.ExitCode; completed_at=[DateTime]::UtcNow.ToString('o'); audit=$audit}
$json = $record | ConvertTo-Json -Depth 20 -Compress
[IO.File]::WriteAllText((Join-Path $out 'audit-result.json'), $json, [Text.UTF8Encoding]::new($false))
Write-Output ('PRODUCTION_DEPENDENCY_AUDIT_V1 ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
exit $process.ExitCode
