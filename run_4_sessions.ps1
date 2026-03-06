$ErrorActionPreference = "Stop"

$targetWins = 3
$maxSessions = 4
$wins = 0
$losses = 0
$noTrade = 0
$results = @()

$env:AUTO_TRADE = "1"
$env:COOLDOWN_AFTER_TRADE = "0"
$env:COOLDOWN_AFTER_LOSS = "0"
$env:COIN_COOLDOWN_LOSS1_SEC = "0"
$env:COIN_COOLDOWN_LOSS2_SEC = "0"
$env:COIN_MAX_LOSSES_PER_DAY = "0"
$env:COIN_MAX_CONSECUTIVE_LOSSES = "0"
$env:MAX_ACTIVE_TRADES = "1"

for ($session = 1; $session -le $maxSessions -and $wins -lt $targetWins; $session++) {
    Write-Output "=== SESSION $session START ==="

    Get-CimInstance Win32_Process -Filter "name='python.exe'" |
        Where-Object { $_.CommandLine -match "main.py" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

    $state = Get-Content daily_log.json -Raw | ConvertFrom-Json
    $baseCount = @($state.trades).Count

    $sessionLog = "session_${session}_live.log"
    $sessionErr = "session_${session}_live.err.log"
    if (Test-Path $sessionLog) { Remove-Item $sessionLog -Force }
    if (Test-Path $sessionErr) { Remove-Item $sessionErr -Force }

    $proc = Start-Process -FilePath "py" `
        -ArgumentList @("-3", "main.py") `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $sessionLog `
        -RedirectStandardError $sessionErr

    $deadline = (Get-Date).AddMinutes(12)
    $tradeDone = $false

    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5

        $nowState = Get-Content daily_log.json -Raw | ConvertFrom-Json
        $nowCount = @($nowState.trades).Count
        if ($nowCount -gt $baseCount) {
            $tradeDone = $true
            break
        }
        if ($proc.HasExited) {
            break
        }
    }

    if (!$proc.HasExited) {
        Stop-Process -Id $proc.Id -Force
    }

    Get-CimInstance Win32_Process -Filter "name='python.exe'" |
        Where-Object { $_.CommandLine -match "main.py" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

    Start-Sleep -Seconds 2

    if ($tradeDone) {
        $endState = Get-Content daily_log.json -Raw | ConvertFrom-Json
        $last = $endState.trades[-1]
        $pnl = [double]$last.pnl
        $res = if ($pnl -ge 0) { "WIN" } else { "LOSS" }
        if ($res -eq "WIN") { $wins++ } else { $losses++ }

        $results += [pscustomobject]@{
            session      = $session
            result       = $res
            symbol       = "$($last.symbol)"
            pnl          = [math]::Round($pnl, 4)
            close_reason = "$($last.close_reason)"
            hold_seconds = [math]::Round(([double]$last.hold_seconds), 1)
        }

        Write-Output ("SESSION {0}: {1} {2} pnl={3} hold={4}s" -f $session, $res, $last.symbol, [math]::Round($pnl, 4), [math]::Round(([double]$last.hold_seconds), 1))
    }
    else {
        $noTrade++
        $results += [pscustomobject]@{
            session      = $session
            result       = "NO_TRADE"
            symbol       = ""
            pnl          = 0
            close_reason = "timeout_or_exit"
            hold_seconds = 0
        }
        Write-Output "SESSION ${session}: NO_TRADE (timeout 12m)"
    }

    Write-Output ("PROGRESS: wins={0} losses={1} no_trade={2}" -f $wins, $losses, $noTrade)
}

Write-Output "=== FINAL SUMMARY ==="
$results | Format-Table -AutoSize | Out-String | Write-Output
Write-Output ("TARGET: 3 wins | ACHIEVED wins={0}, losses={1}, no_trade={2}" -f $wins, $losses, $noTrade)
