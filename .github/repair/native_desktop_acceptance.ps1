param(
    [Parameter(Mandatory=$true)][string]$Commit,
    [Parameter(Mandatory=$true)][string]$ExpectedTree,
    [string]$BaseCommit = 'ddabd3837862022a6053331fe3ba97cf9716b9e3'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Use the native Windows work machine' }
if ($Commit -notmatch '^[a-f0-9]{40}$' -or $ExpectedTree -notmatch '^[a-f0-9]{40}$') { throw 'Invalid source identity' }
$root = Join-Path $env:USERPROFILE 'Dev\hermes-health-repair-20261019'
$repo = Join-Path $root 'core-review-base'
$work = Join-Path $root ('native-pr93004-fixed-' + $Commit.Substring(0,8))
$out = Join-Path $work 'artifacts\hermes-repair-20261019\desktop-acceptance'
$receipt = Join-Path $out 'native-acceptance.json'
if (Test-Path $receipt) { throw 'Read the existing desktop acceptance before repeating it' }
if (-not (Test-Path $work)) {
    & git -C $repo fetch --no-tags https://github.com/the3asic/hermes-agent.git $Commit
    if ($LASTEXITCODE) { throw 'Desktop candidate fetch failed' }
    & git -C $repo worktree add --detach $work $Commit
    if ($LASTEXITCODE) { throw 'Desktop candidate creation failed' }
}
Set-Location $work
if ((git rev-parse HEAD).Trim() -ne $Commit -or (git rev-parse 'HEAD^{tree}').Trim() -ne $ExpectedTree) { throw 'Preserve changed source' }
if (git status --porcelain --untracked-files=no) { throw 'Preserve native source edits' }
[IO.Directory]::CreateDirectory($out) | Out-Null
$env:HERMES_HOME = Join-Path $out 'pm-home'
$env:HERMES_RUNTIME_DIR = Join-Path $root 'core-upstream130122\artifacts\hermes-repair-20261019\pm-runtime'
[IO.Directory]::CreateDirectory($env:HERMES_HOME) | Out-Null
$node = (Get-Command node -ErrorAction Stop).Source
$npm = Join-Path (Split-Path (Get-Command npm.cmd -ErrorAction Stop).Source -Parent) 'node_modules\npm\bin\npm-cli.js'
if (-not (Test-Path $npm)) { throw 'Inspect the installed Node/npm pair' }
$nodeVersion = (& $node --version | Out-String).Trim()
$npmVersion = (& $node $npm --version | Out-String).Trim()
$version = [Version]$nodeVersion.TrimStart('v')
if (-not (($version.Major -eq 22 -and $version -ge [Version]'22.22.0') -or ($version.Major -eq 24 -and $version -ge [Version]'24.11.0') -or $version.Major -ge 26)) { throw 'Node does not satisfy the desktop package engine' }
function Invoke-NpmStage([string]$Name, [string[]]$StageArguments) {
    $argv = @('"' + $npm + '"') + $StageArguments
    $process = Start-Process -FilePath $node -ArgumentList $argv -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $out ($Name + '.stdout')) -RedirectStandardError (Join-Path $out ($Name + '.stderr'))
    [Console]::WriteLine('DESKTOP_STAGE ' + $Name + ' exit=' + $process.ExitCode)
    return [int]$process.ExitCode
}
$installExit = Invoke-NpmStage 'install' @('ci','--no-audit','--no-fund')
$testExit = $null
$typecheckExit = $null
$lintExit = $null
$checksExit = $null
$junitPath = Join-Path $out 'native-junit.xml'
$testPath = 'apps/desktop/src/app/cron/cron-job-model.test.ts'
$counts = [ordered]@{tests=0; failures=0; errors=0; skipped=0}
if ($installExit -eq 0) {
    $testExit = Invoke-NpmStage 'test' @('run','test','--workspace','apps/desktop','--','src/app/cron/cron-job-model.test.ts','--reporter=junit',('--outputFile="' + $junitPath + '"'))
    if (Test-Path $junitPath) {
        [xml]$junit = [IO.File]::ReadAllText($junitPath)
        foreach ($suite in $junit.SelectNodes('//testsuite')) {
            foreach ($name in @('tests','failures','errors','skipped')) { $counts[$name] += [int]$suite.GetAttribute($name) }
        }
    }
    $typecheckExit = Invoke-NpmStage 'typecheck' @('run','typecheck','--workspace','apps/desktop')
    $lintPaths = @('src/app/cron/cron-job-model.ts','src/app/cron/cron-job-model.test.ts','src/app/cron/index.tsx','src/types/hermes.ts','src/i18n/en.ts','src/i18n/ja.ts','src/i18n/types.ts','src/i18n/zh-hant_command_center.ts','src/i18n/zh.ts')
    $lintExit = Invoke-NpmStage 'lint' (@('exec','--workspace','apps/desktop','--','eslint') + $lintPaths)
}
$checksStdout = ''
$checksStderr = ''
if ($testExit -eq 0 -and $typecheckExit -eq 0 -and $lintExit -eq 0) {
    $python = Join-Path $root 'core-test-messaging-ddabd383\Scripts\python.exe'
    $check = Start-Process -FilePath $python -ArgumentList @('-u','scripts/check','--base',$BaseCommit,'--commit',$Commit) -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $out 'checks.stdout') -RedirectStandardError (Join-Path $out 'checks.stderr')
    $checksExit = $check.ExitCode
    $checksStdout = [IO.File]::ReadAllText((Join-Path $out 'checks.stdout'))
    $checksStderr = [IO.File]::ReadAllText((Join-Path $out 'checks.stderr'))
}
$status = @(git status --porcelain --untracked-files=no)
$result = [ordered]@{
    case='pr93004-fixed'; commit=$Commit; tree=(git rev-parse 'HEAD^{tree}').Trim(); base_commit=$BaseCommit;
    test_path=$testPath; test_file_sha256=(Get-FileHash $testPath -Algorithm SHA256).Hash.ToLowerInvariant();
    install_exit=$installExit; test_exit=$testExit; typecheck_exit=$typecheckExit; lint_exit=$lintExit;
    checks_exit=$checksExit; counts=$counts; checks_stdout=$checksStdout; checks_stderr=$checksStderr;
    node=$nodeVersion; npm=$npmVersion; hostname=$env:COMPUTERNAME; tracked_status=$status;
    harness_file_sha256=(Get-FileHash $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant();
    completed_at=[DateTime]::UtcNow.ToString('o');
    junit_base64=if (Test-Path $junitPath) {[Convert]::ToBase64String([IO.File]::ReadAllBytes($junitPath))} else {$null};
    stage_logs=@(foreach ($name in @('install','test','typecheck','lint')) { $p=Join-Path $out ($name + '.stderr'); if (Test-Path $p) { @{name=$name; stderr_tail=(([IO.File]::ReadAllText($p)).Split("`n") | Select-Object -Last 20)} } })
}
$json = $result | ConvertTo-Json -Depth 6 -Compress
[IO.File]::WriteAllText($receipt, $json, [Text.UTF8Encoding]::new($false))
Write-Output ('NATIVE_DESKTOP_ACCEPTANCE_V1 ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
if ($installExit -ne 0 -or $testExit -ne 0 -or $typecheckExit -ne 0 -or $lintExit -ne 0 -or $checksExit -ne 0 -or $counts.tests -ne 30 -or $counts.failures -or $counts.errors -or $counts.skipped -or $status.Count) { exit 1 }
exit 0
