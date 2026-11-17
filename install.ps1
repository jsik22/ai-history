# Windows installer: copies the collectors and aih into %USERPROFILE%\.local\bin
# and adds that folder to the user PATH. Hook settings are printed, not written.
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'bin'
$target = Join-Path $HOME '.local\bin'

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw 'python 을 찾을 수 없습니다. Python 3.9 이상을 설치하세요.' }
& python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.9 이상이 필요합니다.' }

New-Item -ItemType Directory -Force $target | Out-Null
foreach ($name in 'aih', 'aih.cmd', 'ai-history-claude', 'ai-history-codex', 'ai_history_common.py') {
    Copy-Item (Join-Path $source $name) (Join-Path $target $name) -Force
}

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$entries = @($userPath -split ';' | Where-Object { $_ })
if ($entries -notcontains $target) {
    [Environment]::SetEnvironmentVariable('Path', (($entries + $target) -join ';'), 'User')
    Write-Host "PATH 에 $target 를 추가했습니다. 새 터미널에서 적용됩니다."
}

$bin = $target -replace '\\', '/'
Write-Host "설치 완료: $target"
Write-Host ''
Write-Host '~/.claude/settings.json 의 hooks 에 병합할 명령:'
Write-Host "  python `"$bin/ai-history-claude`""
Write-Host '~/.codex/config.toml 의 훅에 병합할 명령:'
Write-Host "  python `"$bin/ai-history-codex`""
