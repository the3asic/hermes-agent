param(
    [Parameter(Mandatory=$true)][string]$PlanPath,
    [Parameter(Mandatory=$true)][ValidateSet('nuc','4090pc')][string]$HostTag,
    [Parameter(Mandatory=$true)][string]$ExpectedPlanSha
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Native Windows is required' }
if ((Get-FileHash $PlanPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ExpectedPlanSha) { throw 'Inspect the changed integration plan' }
$plan = [IO.File]::ReadAllText($PlanPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
if ($plan.base -ne '493208879a3248c6a4b844981d2c31c78ae00929') { throw 'Inspect the integration base' }
$root = Join-Path $env:USERPROFILE 'Dev\hermes-health-repair-20261019'
$repo = Join-Path $root 'core-review-base'
$harness = Join-Path $root 'native_acceptance_git_d81dde7c.ps1'
$harnessSha = 'eeec61b273447c77004461ab3af3e2561ccd71ef5f65892e51698a2a42b7cece'
if (-not (Test-Path $harness)) {
    & git -C $repo fetch --no-tags https://github.com/the3asic/hermes-agent.git d81dde7cc0fd7157384abc6f188c640708abc008
    if ($LASTEXITCODE) { throw 'Acceptance helper fetch failed' }
    $read = Start-Process -FilePath (Get-Command git.exe).Source -WorkingDirectory $repo -ArgumentList @('show','d81dde7cc0fd7157384abc6f188c640708abc008:.github/repair/native_acceptance.ps1') -NoNewWindow -Wait -PassThru -RedirectStandardOutput $harness -RedirectStandardError ($harness + '.stderr')
    if ($read.ExitCode) { throw 'Acceptance helper extraction failed' }
}
if ((Get-FileHash $harness -Algorithm SHA256).Hash.ToLowerInvariant() -ne $harnessSha) { throw 'Inspect the acceptance helper identity' }
$python = Join-Path $root 'core-test-messaging-ddabd383\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Prepare the declared current dependency environment' }
$out = Join-Path $root ('artifacts\current-base-49320887-' + $HostTag)
[IO.Directory]::CreateDirectory($out) | Out-Null
$cases = @($plan.cases | Where-Object { $_.host -eq $HostTag })
if ($cases.Count -ne 11) { throw 'Inspect the host case assignment' }
$results = @()
$failed = 0
foreach ($case in $cases) {
    $signature = $case.case + '-' + $case.commit.Substring(0,8)
    $work = Join-Path $root ('native-' + $signature)
    $receipt = Join-Path $work 'artifacts\hermes-repair-20261019\current-49320887\native-acceptance.json'
    $log = Join-Path $out $signature
    [IO.Directory]::CreateDirectory($log) | Out-Null
    $exitPath = Join-Path $log 'worker-exit.json'
    if (-not (Test-Path $receipt)) {
        $arguments = @('-NoProfile','-NonInteractive','-File',('"' + $harness + '"'),'-CaseId',$case.case,
            '-Commit',$case.commit,'-ExpectedTree',$case.tree,'-TestPath',$case.test_path,
            '-ExpectedTests',([string]$case.expected_tests),'-TestPython',('"' + $python + '"'),
            '-BaseCommit',$plan.base,'-RunId','current-49320887')
        $worker = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -WorkingDirectory $root -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $log 'worker.stdout') -RedirectStandardError (Join-Path $log 'worker.stderr')
        [IO.File]::WriteAllText($exitPath, (@{exit_code=$worker.ExitCode; commit=$case.commit; pid=$worker.Id} | ConvertTo-Json -Compress))
    }
    if (-not (Test-Path $receipt) -or -not (Test-Path $exitPath)) { throw ('Read the incomplete real case logs: ' + $signature) }
    $record = [IO.File]::ReadAllText($receipt, [Text.Encoding]::UTF8) | ConvertFrom-Json
    $actualExit = [IO.File]::ReadAllText($exitPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
    if ($record.commit -ne $case.commit -or $record.tree -ne $case.tree -or $record.base_commit -ne $plan.base -or $record.harness_file_sha256 -ne $harnessSha) { throw 'Inspect the case source identity' }
    if ($actualExit.commit -ne $case.commit -or $record.test_file_sha256 -ne $case.test_file_sha256) { throw 'Inspect the actual test identity' }
    if ((git -C $work rev-parse HEAD).Trim() -ne $case.commit -or (git -C $work status --porcelain --untracked-files=no)) { throw 'Preserve edited native source' }
    $accepted = $actualExit.exit_code -eq 0 -and $record.test_exit -eq 0 -and $record.checks_exit -eq 0 -and $record.counts.tests -eq $case.expected_tests -and $record.counts.failures -eq 0 -and $record.counts.errors -eq 0 -and $record.counts.skipped -eq 0 -and $record.checks_stdout.Contains('scripts/check: 12 checks, ok')
    if (-not $accepted) { $failed++ }
    $results += @{case=$case.case; commit=$case.commit; tree=$case.tree; source_head=$case.source_head; accepted=$accepted; worker_exit=$actualExit.exit_code; native=$record}
    $summary = @{case=$case.case; commit=$case.commit; accepted=$accepted; counts=$record.counts; test_exit=$record.test_exit; checks_exit=$record.checks_exit}
    Write-Output ('CURRENT_BASE_CASE_COMPLETED ' + ($summary | ConvertTo-Json -Depth 5 -Compress))
    [IO.File]::WriteAllText((Join-Path $out 'progress.json'), (@{host=$HostTag; completed=$results.Count; failed=$failed; summaries=@($results | ForEach-Object { @{case=$_.case; commit=$_.commit; accepted=$_.accepted} })} | ConvertTo-Json -Depth 6 -Compress))
}
$result = @{host=$HostTag; hostname=$env:COMPUTERNAME; base=$plan.base; plan_sha256=$ExpectedPlanSha; batch_file_sha256=(Get-FileHash $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant(); results=$results; failed=$failed}
$json = $result | ConvertTo-Json -Depth 12 -Compress
[IO.File]::WriteAllText((Join-Path $out 'result.json'), $json, [Text.UTF8Encoding]::new($false))
Write-Output ('NATIVE_CURRENT_BASE_BATCH_V1 ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
if ($failed) { exit 1 }
exit 0
