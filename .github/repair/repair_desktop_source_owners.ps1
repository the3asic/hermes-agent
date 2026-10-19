param([switch]$Write)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Join-Path $env:USERPROFILE 'Dev\hermes-health-repair-20261019'
$work = Join-Path $root 'native-pr93004-fixed-2a5f1a8b'
if (-not $Write) { Write-Output 'Would extract the failed desktop candidate locale owners'; exit 0 }
Set-Location $work
$out = Join-Path $work 'artifacts\hermes-repair-20261019'
$export = Join-Path $out 'native-desktop-source-edits.json'
if (Test-Path $export) { throw 'Read the existing native source edit receipt' }
if ((git rev-parse HEAD).Trim() -ne '2a5f1a8bf7b6060ca0d3d70ce08d9ec91546c92a' -or (git status --porcelain --untracked-files=no)) { throw 'Preserve changed desktop source' }
$accepted = Get-Content (Join-Path $out 'desktop-acceptance\native-acceptance.json') -Raw | ConvertFrom-Json
if ($accepted.test_exit -ne 0 -or $accepted.typecheck_exit -ne 0 -or $accepted.lint_exit -ne 0 -or $accepted.checks_exit -ne 1) { throw 'Inspect the actual desktop validation result' }
$path = Join-Path $out 'native_desktop_source_owners.mjs'
Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/the3asic/hermes-agent/0d8735563de7130e3722202c8a41495600b4d57b/.github/repair/native_desktop_source_owners.mjs' -OutFile $path
if ((Get-FileHash $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne '3d19efd940a7ef3a320e18b94d522f065eff15348ea8898b9fb2ec23575f2c1b') { throw 'Inspect the source editing helper' }
& node $path
if ($LASTEXITCODE) { throw 'Desktop locale extraction failed' }
$paths = (Get-Content (Join-Path $out 'desktop-source-owner-paths.json') -Raw | ConvertFrom-Json).files
$prettier = Join-Path $work 'node_modules\prettier\bin\prettier.cjs'
if (-not (Test-Path $prettier)) { throw 'Use the declared desktop formatter' }
& node $prettier --write @paths
if ($LASTEXITCODE) { throw 'Desktop owner formatting failed' }
$record = @{commit=$accepted.commit; tree=$accepted.tree; files=@(foreach ($name in $paths) { @{path=$name; sha256=(Get-FileHash $name -Algorithm SHA256).Hash.ToLowerInvariant(); data=[Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $work $name)))} })} | ConvertTo-Json -Depth 5 -Compress
[IO.File]::WriteAllText($export, $record, [Text.UTF8Encoding]::new($false))
Write-Output ('NATIVE_DESKTOP_SOURCE_EDITS_V1 ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($record)))
