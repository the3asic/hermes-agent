param([switch]$Write)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:GIT_TERMINAL_PROMPT = '0'
$root = Join-Path $env:USERPROFILE 'Dev\hermes-health-repair-20261019'
$repo = Join-Path $root 'core-review-base'
$source = Join-Path $root 'core-environment-ddabd383'
$environment = Join-Path $root 'core-test-messaging-ddabd383'
$base = 'ddabd3837862022a6053331fe3ba97cf9716b9e3'
if (-not $Write) { @{source=$source; environment=$environment; base=$base; preview=$true} | ConvertTo-Json -Compress; exit 0 }
if (-not (Test-Path $repo)) { throw 'Inspect the existing native source repository' }
if (-not (Test-Path $source)) {
    & git -C $repo fetch --no-tags --filter=blob:none https://github.com/NousResearch/hermes-agent.git $base
    if ($LASTEXITCODE) { throw 'Current native base fetch failed' }
    & git -C $repo worktree add --detach $source $base
    if ($LASTEXITCODE) { throw 'Current native source creation failed' }
}
if ((git -C $source rev-parse HEAD).Trim() -ne $base -or (git -C $source status --porcelain --untracked-files=no)) { throw 'Preserve changed native source' }
$out = Join-Path $source 'artifacts\hermes-repair-20261019'
$receipt = Join-Path $out 'native-environment-acceptance.json'
if (Test-Path $receipt) { Get-Content $receipt -Raw; exit 0 }
if (Test-Path $environment) { throw 'Inspect the existing partial native environment' }
[IO.Directory]::CreateDirectory($out) | Out-Null
$env:HERMES_HOME = Join-Path $out 'pm-home'
$env:HERMES_RUNTIME_DIR = Join-Path $root 'core-upstream130122\artifacts\hermes-repair-20261019\pm-runtime'
[IO.Directory]::CreateDirectory($env:HERMES_HOME) | Out-Null
$python = Join-Path $env:USERPROFILE 'the3asic-agentkit\.venv\Scripts\python.exe'
Set-Location $source
& $python -m pm.build_env --source . --out $environment --group dev --group test --extra messaging
if ($LASTEXITCODE) { throw 'Current native dependency environment failed' }
$testPython = Join-Path $environment 'Scripts\python.exe'
& $testPython -c 'import sys, pytest, discord; print(sys.version); print(pytest.__version__); print(discord.__version__)'
if ($LASTEXITCODE) { throw 'Current native dependency import validation failed' }
$result = @{ok=$true; base=$base; source=$source; python=$testPython; hostname=$env:COMPUTERNAME; completed_at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress
[IO.File]::WriteAllText($receipt, $result, [Text.UTF8Encoding]::new($false))
Write-Output $result
