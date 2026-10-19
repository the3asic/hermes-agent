param(
    [Parameter(Mandatory=$true)][string]$CaseId,
    [Parameter(Mandatory=$true)][string]$Commit,
    [Parameter(Mandatory=$true)][string]$ExpectedTree,
    [Parameter(Mandatory=$true)][string]$TestPath,
    [Parameter(Mandatory=$true)][int]$ExpectedTests,
    [Parameter(Mandatory=$true)][string]$TestPython,
    [switch]$Baseline
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Native Windows is required' }
if ($CaseId -notmatch '^pr[0-9]+-(baseline|fixed)$') { throw 'Invalid case identity' }
if ($Commit -notmatch '^[a-f0-9]{40}$' -or $ExpectedTree -notmatch '^[a-f0-9]{40}$') { throw 'Invalid source identity' }
if ($TestPath -notmatch '^tests/[a-zA-Z0-9_/]+\.py$' -or $ExpectedTests -lt 1) { throw 'Invalid test selection' }
if (-not (Test-Path -LiteralPath $TestPython)) { throw 'Prepare the declared PM test environment' }
$root = Join-Path $env:USERPROFILE 'Dev\hermes-health-repair-20261019'
$repo = Join-Path $root 'core-review-base'
$work = Join-Path $root ('native-' + $CaseId)
$out = Join-Path $work 'artifacts\hermes-repair-20261019'
$receipt = Join-Path $out 'native-acceptance.json'
if (Test-Path -LiteralPath $receipt) { throw 'Inspect the existing acceptance receipt before repeating this case' }
if (-not (Test-Path -LiteralPath $work)) {
    & git -C $repo fetch --no-tags https://github.com/the3asic/hermes-agent.git $Commit
    if ($LASTEXITCODE) { throw 'Candidate fetch failed' }
    & git -C $repo worktree add --detach $work $Commit
    if ($LASTEXITCODE) { throw 'Candidate worktree creation failed' }
}
Set-Location $work
if ((git rev-parse HEAD).Trim() -ne $Commit) { throw 'Candidate commit changed' }
if ((git rev-parse 'HEAD^{tree}').Trim() -ne $ExpectedTree) { throw 'Candidate tree changed' }
if (git status --porcelain --untracked-files=no) { throw 'Preserve the edited candidate files' }
[IO.Directory]::CreateDirectory($out) | Out-Null
$env:HERMES_PYTHON = $TestPython.Replace('\','/')
$env:__HERMES_ACTIVATED = ''
$env:PYTHONUNBUFFERED = '1'
$env:HERMES_HOME = Join-Path $out 'pm-home'
$env:HERMES_RUNTIME_DIR = Join-Path $root 'core-upstream130122\artifacts\hermes-repair-20261019\pm-runtime'
[IO.Directory]::CreateDirectory($env:HERMES_HOME) | Out-Null
$cases = (Join-Path (Split-Path $env:HERMES_RUNTIME_DIR -Parent) ($CaseId + '-cases')).Replace('\','/')
$junitPath = Join-Path $out 'native-junit.xml'
& (Join-Path $env:ProgramFiles 'Git\bin\bash.exe') scripts/run_tests.sh $TestPath --file-timeout 300 -- --noconftest --basetemp=$cases --junitxml=$junitPath -q
$testExit = $LASTEXITCODE
[xml]$junit = [IO.File]::ReadAllText($junitPath)
$counts = [ordered]@{ tests=0; failures=0; errors=0; skipped=0 }
foreach ($suite in $junit.SelectNodes('//testsuite')) {
    foreach ($name in @('tests','failures','errors','skipped')) { $counts[$name] += [int]$suite.GetAttribute($name) }
}
$checkExit = $null
if (-not $Baseline -and $testExit -eq 0) {
    & $TestPython -u scripts/check --base 5fc490e2b34123212d73ef316086f939567a725a --commit $Commit
    $checkExit = $LASTEXITCODE
}
$status = @(git status --porcelain --untracked-files=no)
$result = [ordered]@{
    case=$CaseId; commit=$Commit; tree=(git rev-parse 'HEAD^{tree}').Trim();
    test_path=$TestPath; test_file_sha256=(Get-FileHash $TestPath -Algorithm SHA256).Hash.ToLowerInvariant();
    test_exit=$testExit; counts=$counts; checks_exit=$checkExit; baseline=[bool]$Baseline;
    python=$TestPython; hostname=$env:COMPUTERNAME; os=[Environment]::OSVersion.VersionString;
    tracked_status=$status; junit_base64=[Convert]::ToBase64String([IO.File]::ReadAllBytes($junitPath))
}
$json = $result | ConvertTo-Json -Depth 6 -Compress
[IO.File]::WriteAllText($receipt, $json, [Text.UTF8Encoding]::new($false))
Write-Output ('NATIVE_REPAIR_ACCEPTANCE_V1 ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
if ($counts.tests -ne $ExpectedTests -or $counts.errors -or $counts.skipped -or $status.Count) { throw 'Native acceptance is incomplete' }
if ($testExit -ne 0) { exit $testExit }
if (-not $Baseline -and $checkExit -ne 0) { exit $checkExit }
exit 0
