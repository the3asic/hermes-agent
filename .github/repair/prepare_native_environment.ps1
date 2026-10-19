param([switch]$Write, [switch]$ResumePartial)
$ErrorActionPreference = 'Stop'
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'never'
$root = Join-Path $env:USERPROFILE 'Dev\hermes-health-repair-20261019'
$owner = Join-Path $root 'core-browser'
$repo = Join-Path $root 'core-review-base'
$base = 'ddabd3837862022a6053331fe3ba97cf9716b9e3'
$environment = Join-Path $root 'core-test-messaging-ddabd383'
$python = Join-Path $env:USERPROFILE 'the3asic-agentkit\.venv\Scripts\python.exe'
if (-not (Test-Path $python) -or -not (Test-Path $owner)) { throw 'Inspect the existing native source and AgentKit Python' }
if (-not $Write) {
    [ordered]@{ preview=$true; source=$repo; base=$base; environment=$environment } | ConvertTo-Json -Compress
    exit 0
}
if (-not (Test-Path $repo)) {
    & git -C $owner fetch --no-tags --filter=blob:none https://github.com/NousResearch/hermes-agent.git $base
    if ($LASTEXITCODE) { throw 'Native base fetch failed' }
    & git -C $owner worktree add --detach $repo $base
    if ($LASTEXITCODE) { throw 'Native base worktree creation failed' }
}
if ((& git -C $repo rev-parse HEAD).Trim() -ne $base) { throw 'Preserve the changed base checkout' }
if (& git -C $repo status --porcelain --untracked-files=no) { throw 'Preserve native base edits' }
$out = Join-Path $repo 'artifacts\hermes-repair-20261019'
if (Test-Path $environment) {
    if (-not $ResumePartial) { throw 'Inspect the existing PM environment before repeating its build' }
    if (Test-Path (Join-Path $out 'native-environment-acceptance.json')) { throw 'Preserve an accepted environment' }
    if ((Get-Item $environment).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Preserve a redirected environment' }
    $active = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($environment + '\', [StringComparison]::OrdinalIgnoreCase) })
    if ($active.Count) { throw 'Preserve the in-use environment' }
    $retained = Join-Path $out 'unused-partial-environment'
    if (Test-Path $retained) { throw 'Inspect the retained partial environment' }
    [IO.Directory]::CreateDirectory($out) | Out-Null
    Move-Item -LiteralPath $environment -Destination $retained
}
$env:HERMES_HOME = Join-Path $out 'pm-home'
$env:HERMES_RUNTIME_DIR = Join-Path $root 'core-upstream130122\artifacts\hermes-repair-20261019\pm-runtime'
[IO.Directory]::CreateDirectory($out) | Out-Null
[IO.Directory]::CreateDirectory($env:HERMES_HOME) | Out-Null
[IO.Directory]::CreateDirectory($env:HERMES_RUNTIME_DIR) | Out-Null
Push-Location $repo
try {
    & $python -m pm.build_env --source . --out $environment --group dev --group test --extra messaging
    if ($LASTEXITCODE) { throw 'Native PM messaging environment build failed' }
    $testPython = Join-Path $environment 'Scripts\python.exe'
    & $testPython -c 'import sys, pytest, discord; print(sys.version); print(pytest.__version__); print(discord.__version__)'
    if ($LASTEXITCODE) { throw 'Native PM dependency verification failed' }
    $result = [ordered]@{ ok=$true; base=$base; source=$repo; python=$testPython; hostname=$env:COMPUTERNAME; completed_at=[DateTime]::UtcNow.ToString('o') }
    $json = $result | ConvertTo-Json -Compress
    [IO.File]::WriteAllText((Join-Path $out 'native-environment-acceptance.json'), $json, [Text.UTF8Encoding]::new($false))
    Write-Output $json
} finally { Pop-Location }
