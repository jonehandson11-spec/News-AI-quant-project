param([switch]$Backfill, [switch]$ResumeOnly, [switch]$Check)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$state = 'D:\news_crawlers\ft-sync'
$python = 'D:\news_crawlers\sina\.venv\Scripts\python.exe'
$env:PYTHONUTF8 = '1'
$env:FT_REQUEST_DELAY_SECONDS = '10'
$arguments = @((Join-Path $PSScriptRoot 'ft_autorun.py'), '--root', $root,
    '--state-dir', $state, '--cookie-file', 'D:\news_crawlers\private\ft_cookie.txt',
    '--ssh-key', 'D:\news_crawlers\private\ft_github_ed25519',
    '--known-hosts', 'D:\news_crawlers\private\ft_github_known_hosts',
    '--ssh-executable', 'C:\Windows\System32\OpenSSH\ssh.exe')
if ($Backfill) { $arguments += @('--lookback-hours', '120') }
if ($ResumeOnly) { $arguments += '--resume-only' }
if ($Check) { $arguments += '--check' }
try {
    $raw = & $python @arguments 2>$null
    $runExit = $LASTEXITCODE
    $result = ($raw -join "`n") | ConvertFrom-Json
} catch {
    $runExit = 1
    $result = [pscustomobject]@{status='error'; reason='local_program_failed'}
}
$result | ConvertTo-Json -Depth 5 -Compress
if (-not $Check) {
    $noticePath = Join-Path $state 'windows-notification.json'
    $previous = if (Test-Path -LiteralPath $noticePath) { Get-Content -LiteralPath $noticePath -Raw | ConvertFrom-Json } else { $null }
    $notice = $null
    if ($result.status -in @('error', 'needs_attention', 'imported_needs_attention', 'pending_upload')) {
        $reason = if ($result.reason -match '^[a-z_]{1,64}$') { $result.reason } else { 'local_program_failed' }
        if ($previous.kind -ne 'fault' -or $previous.reason -ne $reason) {
            $notice = @{kind='fault'; reason=$reason}
        }
    } elseif ($result.status -eq 'imported' -and $previous.kind -eq 'fault') {
        $notice = @{kind='recovered'; reason='recovered'}
    } elseif ($result.reason -eq 'target_reached' -and $previous.kind -ne 'complete') {
        $notice = @{kind='complete'; reason='target_reached'}
    }
    if ($notice) {
        $notice.time = [DateTimeOffset]::UtcNow.ToString('o')
        $notice | ConvertTo-Json | Set-Content -LiteralPath $noticePath -Encoding UTF8
        $notifyScript = Join-Path $PSScriptRoot 'notify_ft_windows.ps1'
        Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $notifyScript + '"'), '-Reason', $notice.reason) | Out-Null
    }
}
exit $runExit
