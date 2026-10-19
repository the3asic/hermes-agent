param([switch]$Write)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:GIT_TERMINAL_PROMPT = '0'
$root = Join-Path $env:USERPROFILE 'Dev\hermes-health-repair-20261019'
$repo = Join-Path $root 'core-review-base'
$commit = 'fae61adf03951649b6e54087328fbb458cedbd5a'
$work = Join-Path $root 'native-pr86879-fixed-fae61adf'
if (-not $Write) { @{work=$work; commit=$commit; preview=$true} | ConvertTo-Json -Compress; exit 0 }
if (-not (Test-Path $work)) {
    & git -C $repo fetch --no-tags https://github.com/the3asic/hermes-agent.git $commit
    if ($LASTEXITCODE) { throw 'Browser candidate fetch failed' }
    & git -C $repo worktree add --detach $work $commit
    if ($LASTEXITCODE) { throw 'Browser candidate creation failed' }
}
if ((git -C $work rev-parse HEAD).Trim() -ne $commit -or (git -C $work status --porcelain --untracked-files=no)) { throw 'Preserve changed browser candidate source' }
Set-Location $work
$out = Join-Path $work 'artifacts\hermes-repair-20261019'
[IO.Directory]::CreateDirectory($out) | Out-Null
$env:HERMES_HOME = Join-Path $out 'browser-preparation-home'
[IO.Directory]::CreateDirectory($env:HERMES_HOME) | Out-Null
$env:HERMES_RUNTIME_DIR = Join-Path $root 'core-upstream130122\artifacts\hermes-repair-20261019\pm-runtime'
$path = Join-Path $out 'native_browser_prepare.py'
Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/the3asic/hermes-agent/0d8735563de7130e3722202c8a41495600b4d57b/.github/repair/native_browser_prepare.py' -OutFile $path
if ((Get-FileHash $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'b22b9aed8e7fff39e8f34199b8959c4f6156d5f6834497eed78edea696bb12dc') { throw 'Inspect the browser preparation helper' }
$env:PYTHONPATH = $work
& (Join-Path $root 'core-test-messaging-ddabd383\Scripts\python.exe') -u $path --write
exit $LASTEXITCODE
