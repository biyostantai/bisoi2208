param(
    [int]$TimeoutSec = 900
)

$ErrorActionPreference = "Stop"

function Get-DailyStats {
    param(
        [string]$Path,
        [string]$DateText
    )

    if (-not (Test-Path $Path)) {
        return @{
            wins = 0
            trades = 0
            total_pnl = 0.0
            last = $null
        }
    }

    try {
        $raw = Get-Content -Path $Path -Raw | ConvertFrom-Json
    }
    catch {
        return @{
            wins = 0
            trades = 0
            total_pnl = 0.0
            last = $null
        }
    }

    if (-not $raw -or $raw.date -ne $DateText) {
        return @{
            wins = 0
            trades = 0
            total_pnl = 0.0
            last = $null
        }
    }

    $trades = @($raw.trades)
    $last = $null
    if ($trades.Count -gt 0) {
        $last = $trades[$trades.Count - 1]
    }

    return @{
        wins = [int]$raw.wins
        trades = $trades.Count
        total_pnl = [double]$raw.total_pnl
        last = $last
    }
}

$today = (Get-Date).ToString("yyyy-MM-dd")
$dailyPath = "daily_log.json"
$stdoutLog = "live_until_win.out.log"
$stderrLog = "live_until_win.err.log"

$baseline = Get-DailyStats -Path $dailyPath -DateText $today
$startTime = Get-Date
$deadline = $startTime.AddSeconds($TimeoutSec)

Write-Host "Baseline: wins=$($baseline.wins), trades=$($baseline.trades), total_pnl=$($baseline.total_pnl)"
Write-Host "Starting bot in DEMO mode..."

$proc = Start-Process -FilePath "py" -ArgumentList "-3", "main.py" -PassThru -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
Write-Host "Bot PID: $($proc.Id)"

$hitWin = $false

while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5

    if ($proc.HasExited) {
        Write-Host "Bot process exited before first win."
        break
    }

    $stats = Get-DailyStats -Path $dailyPath -DateText $today
    if ($stats.wins -gt $baseline.wins) {
        $hitWin = $true
        break
    }
}

if (-not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force
    Start-Sleep -Milliseconds 800
}

$final = Get-DailyStats -Path $dailyPath -DateText $today

if ($hitWin) {
    Write-Host "SUCCESS: first new winning trade detected."
    Write-Host "Final stats: wins=$($final.wins), trades=$($final.trades), total_pnl=$($final.total_pnl)"
    if ($final.last) {
        Write-Host ("Last trade: symbol={0}, pnl={1}, reason={2}" -f $final.last.symbol, $final.last.pnl, $final.last.close_reason)
    }
    exit 0
}

Write-Host "TIMEOUT/NO_WIN: no new winning trade in $TimeoutSec seconds."
Write-Host "Final stats: wins=$($final.wins), trades=$($final.trades), total_pnl=$($final.total_pnl)"
if (Test-Path $stderrLog) {
    $tail = Get-Content -Path $stderrLog -Tail 20
    if ($tail) {
        Write-Host "---- stderr tail ----"
        $tail | ForEach-Object { Write-Host $_ }
    }
}
exit 1
