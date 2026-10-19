param([switch]$Write)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Join-Path $env:USERPROFILE 'Dev\hermes-health-repair-20261019'
$python = Join-Path $root 'core-test-messaging-ddabd383\Scripts\python.exe'
if (-not $Write) { Write-Output 'Would verify the five pinned current Python candidates'; exit 0 }
if (-not (Test-Path $python)) { throw 'Complete the current PM test environment first' }
$path = Join-Path $root 'native_acceptance_d81dde7c_current_batch.ps1'
if (-not (Test-Path $path)) { Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/the3asic/hermes-agent/d81dde7cc0fd7157384abc6f188c640708abc008/.github/repair/native_acceptance.ps1' -OutFile $path }
if ((Get-FileHash $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'eeec61b273447c77004461ab3af3e2561ccd71ef5f65892e51698a2a42b7cece') { throw 'Preserve changed native harness' }
$cases = @(
    @{case='pr65566-fixed'; commit='34b950169db524206279fb4728ef95405e2660a9'; tree='6a101c0a6a5f7073cc438029c198138fb3bb9200'; test='tests/tools/live_mutations/test_native_captured_targets.py'; count=3},
    @{case='pr65576-fixed'; commit='1a269fb6de822c64bac469db5b1f7a0ce4a13c2a'; tree='a54eeb818fc0de615724544c349a4cca379bb385'; test='tests/gateway/live_reasoning/test_native_channel_policy.py'; count=3},
    @{case='pr100364-fixed'; commit='db2aa999daf7e68e7492640676a3bec41c2e7714'; tree='9cffea6632f01bb47cb332a6c1d6ec551da37d7f'; test='tests/gateway/live_footer/test_recorded_turn_accounting.py'; count=2},
    @{case='pr105273-fixed'; commit='5a39f369d723ac6c34d4bde60aa20c8401d751e5'; tree='ab98d9448f63de77ddb3671a2178d0fcafed1a1a'; test='tests/agent/live_accounting/test_recorded_responses_runtime.py'; count=4},
    @{case='pr50030-fixed'; commit='2de8804d0e9fab9c4ae196c3bdf9e94ca8992c0c'; tree='e78fe094f283466c6265e180cc38c9fa18076713'; test='tests/tools/live_recall/test_native_scope_queries.py'; count=4}
)
foreach ($case in $cases) {
    $name = 'native-' + $case.case + '-' + $case.commit.Substring(0,8)
    $work = Join-Path $root $name
    $receipt = Join-Path $work 'artifacts\hermes-repair-20261019\acceptance\native-acceptance.json'
    if (Test-Path $receipt) {
        $json = Get-Content $receipt -Raw
        $record = $json | ConvertFrom-Json
        if ($record.commit -ne $case.commit -or $record.tree -ne $case.tree -or (git -C $work rev-parse HEAD).Trim() -ne $case.commit -or (git -C $work status --porcelain --untracked-files=no)) { throw 'Preserve changed completed native source' }
        Write-Output ('NATIVE_REPAIR_ACCEPTANCE_V3 ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
        continue
    }
    $signature = $case.case + '-' + $case.commit.Substring(0,8)
    $active = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -and ($_.CommandLine.Contains($signature) -or $_.CommandLine.Contains($case.commit)) })
    if ($active.Count) { Write-Output ('NATIVE_CURRENT_CASE_RUNNING ' + $case.case); continue }
    Write-Output ('NATIVE_CURRENT_CASE_BEGIN ' + $case.case)
    & $path -CaseId $case.case -Commit $case.commit -ExpectedTree $case.tree -TestPath $case.test -ExpectedTests $case.count -TestPython $python -BaseCommit 'ddabd3837862022a6053331fe3ba97cf9716b9e3'
    Write-Output ('NATIVE_CURRENT_CASE_EXIT ' + $case.case + ' ' + $LASTEXITCODE)
}
