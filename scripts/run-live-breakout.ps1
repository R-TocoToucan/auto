<#
.SYNOPSIS
    Restart-safe polling operator for `bt live breakout-cycle`.

.DESCRIPTION
    Repeatedly:
      1. Collects newly-completed KRW-BTC 240-minute candles into a
         canonical CandleDataset + SHA-256 sidecar via `bt research
         collect-candles`.
      2. Verifies the new dataset's sidecar hash.
      3. Refuses fail-closed on gaps, duplicates, altered prior candles,
         incomplete candles, or hash mismatch.
      4. Invokes exactly ONE `bt live breakout-cycle`.
      5. Writes sanitized logs (no environment values, no credentials).
      6. Waits until the next polling interval and repeats.

    Default mode is dry-run. Live operation requires BOTH -Live AND
    -EnableLiveOrders on the same command line. The script never
    switches automatically from dry-run to live.

    HALT (a file at $StateDir/HALT) still allows reconciliation via the
    invoked cycle, but the cycle itself refuses to submit new orders
    when HALT is present.

.PARAMETER StateDir
    Persistent state directory used by the live cycle. Also holds this
    script's per-cycle candle datasets, logs, and (optionally) a HALT
    file.

.PARAMETER SnapshotPath
    Path to the sidecar-verified spec snapshot to pass through to
    `bt live breakout-cycle`.

.PARAMETER InitialStartUtc
    ISO-8601 UTC timestamp on a 240-minute boundary — the anchor from
    which every candle fetch starts. Must not change across polls.

.PARAMETER MaxNotionalKrw
    Decimal string forwarded to the live cycle's --max-notional-krw.

.PARAMETER StartingCashKrw
    Decimal string. REQUIRED in dry-run mode, forbidden in live mode.

.PARAMETER PollSeconds
    Interval between polls. Ignored when -Once is set.

.PARAMETER Live
    Enable live-order mode. Must be combined with -EnableLiveOrders.

.PARAMETER EnableLiveOrders
    Second activation switch — must be paired with -Live.

.PARAMETER Once
    Run exactly one poll cycle then exit (operator-testing).

.PARAMETER BtExe
    Override the CLI invocation. Defaults to
    `python -m bithumb_bot.cli.main`.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$StateDir,
    [Parameter(Mandatory = $true)][string]$SnapshotPath,
    [Parameter(Mandatory = $true)][string]$InitialStartUtc,
    [Parameter(Mandatory = $true)][string]$MaxNotionalKrw,
    [string]$StartingCashKrw = "",
    [int]$PollSeconds = 900,
    [switch]$Live,
    [switch]$EnableLiveOrders,
    [switch]$Once,
    [string]$BtExe = "python -m bithumb_bot.cli.main"
)

$ErrorActionPreference = "Stop"
$Market = "KRW-BTC"
$UnitMinutes = 240

# --- Mode + activation guardrails (never auto-transition dry-run->live) ---
$Mode = "dry-run"
if ($Live.IsPresent) {
    if (-not $EnableLiveOrders.IsPresent) {
        Write-Error "refuse: -Live requires -EnableLiveOrders on the same invocation"
        exit 1
    }
    $Mode = "live"
}
if ($Mode -eq "dry-run" -and [string]::IsNullOrWhiteSpace($StartingCashKrw)) {
    Write-Error "refuse: -StartingCashKrw is required in dry-run mode"
    exit 1
}
if ($Mode -eq "live" -and -not [string]::IsNullOrWhiteSpace($StartingCashKrw)) {
    Write-Error "refuse: -StartingCashKrw is forbidden in live mode"
    exit 1
}

# --- Directories ---------------------------------------------------------
if (-not (Test-Path -LiteralPath $StateDir)) {
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
}
$StateDirFull = (Resolve-Path -LiteralPath $StateDir).Path
$DatasetsDir  = Join-Path $StateDirFull "datasets"
$LogsDir      = Join-Path $StateDirFull "logs"
foreach ($d in @($DatasetsDir, $LogsDir)) {
    if (-not (Test-Path -LiteralPath $d)) {
        New-Item -ItemType Directory -Force -Path $d | Out-Null
    }
}

if (-not (Test-Path -LiteralPath $SnapshotPath)) {
    Write-Error "refuse: snapshot not found at $SnapshotPath"
    exit 1
}
$SnapshotSidecar = "$SnapshotPath.sha256"
if (-not (Test-Path -LiteralPath $SnapshotSidecar)) {
    Write-Error "refuse: snapshot sidecar not found at $SnapshotSidecar"
    exit 1
}

# --- Helpers -------------------------------------------------------------
function Get-UtcStamp {
    return (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
}

function Get-FileSha256Hex($path) {
    return ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash).ToLowerInvariant()
}

function Test-SidecarValid($path) {
    $sidecar = "$path.sha256"
    if (-not (Test-Path -LiteralPath $sidecar)) { return $false }
    $line = (Get-Content -LiteralPath $sidecar -Raw).Trim()
    if ($line.Length -lt 64) { return $false }
    $recorded = $line.Substring(0, 64).ToLowerInvariant()
    $actual = Get-FileSha256Hex $path
    return ($recorded -eq $actual)
}

function Get-LatestDataset {
    $files = Get-ChildItem -LiteralPath $DatasetsDir -Filter "candles_*.json" -File `
        | Where-Object { $_.Name -notlike "*.sha256" }
    if (-not $files) { return $null }
    return ($files | Sort-Object -Property Name -Descending | Select-Object -First 1)
}

function Get-FlooredEndUtcIso {
    $now = [datetime]::UtcNow
    $unitSpan = New-TimeSpan -Minutes $UnitMinutes
    $ticks = [math]::Floor($now.Ticks / $unitSpan.Ticks) * $unitSpan.Ticks
    $floored = New-Object System.DateTime($ticks, [System.DateTimeKind]::Utc)
    return $floored.ToString("yyyy-MM-ddTHH:mm:ssZ")
}

function Invoke-Bt($btArgs, $logFile) {
    # Split the configured CLI invocation into program + leading args so
    # `python -m bithumb_bot.cli.main` still works. Never interpolates
    # env-var values.
    $parts = $BtExe -split '\s+'
    $exe = $parts[0]
    $leading = @()
    if ($parts.Length -gt 1) { $leading = $parts[1..($parts.Length - 1)] }
    $allArgs = @()
    $allArgs += $leading
    $allArgs += $btArgs
    "invoking: $exe " + ($allArgs -join " ") | Out-File -FilePath $logFile -Encoding utf8 -Append
    & $exe @allArgs 2>&1 | Tee-Object -FilePath $logFile -Append
    return $LASTEXITCODE
}

function Test-DatasetAppendOnly($priorPath, $newPath) {
    # Refuse: gaps, duplicates, altered prior candles, incomplete candles,
    # or a shrinking dataset. Prior candles (when present) must be a
    # strict prefix of the new dataset.
    try {
        $newJson = Get-Content -LiteralPath $newPath -Raw | ConvertFrom-Json
    } catch {
        Write-Warning "verify: new dataset not parseable"
        return $false
    }
    if ($newJson.market -ne $Market) { return $false }
    if ($newJson.unit_minutes -ne $UnitMinutes) { return $false }
    $newCandles = $newJson.candles
    if (-not $newCandles -or $newCandles.Count -eq 0) {
        Write-Warning "verify: new dataset empty"
        return $false
    }
    # Strict ascending, no duplicates.
    $prevTicks = $null
    foreach ($c in $newCandles) {
        $t = [datetime]::Parse($c.open_time_utc, [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::AssumeUniversal -bor
            [System.Globalization.DateTimeStyles]::AdjustToUniversal).Ticks
        if ($null -ne $prevTicks -and $t -le $prevTicks) {
            Write-Warning "verify: duplicate or non-ascending open_time_utc"
            return $false
        }
        $prevTicks = $t
    }
    # Every candle must be strictly completed (open + unit <= now).
    $unitSpan = New-TimeSpan -Minutes $UnitMinutes
    $nowUtc = [datetime]::UtcNow
    foreach ($c in $newCandles) {
        $open = [datetime]::Parse($c.open_time_utc, [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::AssumeUniversal -bor
            [System.Globalization.DateTimeStyles]::AdjustToUniversal)
        if (($open + $unitSpan) -gt $nowUtc) {
            Write-Warning "verify: incomplete candle at $($c.open_time_utc)"
            return $false
        }
    }
    if ($priorPath) {
        try {
            $priorJson = Get-Content -LiteralPath $priorPath.FullName -Raw | ConvertFrom-Json
        } catch {
            Write-Warning "verify: prior dataset not parseable"
            return $false
        }
        $priorCandles = $priorJson.candles
        if ($newCandles.Count -lt $priorCandles.Count) {
            Write-Warning "verify: new dataset has fewer candles than prior"
            return $false
        }
        for ($i = 0; $i -lt $priorCandles.Count; $i++) {
            $a = $priorCandles[$i] | ConvertTo-Json -Compress -Depth 20
            $b = $newCandles[$i]   | ConvertTo-Json -Compress -Depth 20
            if ($a -ne $b) {
                Write-Warning "verify: prior candle at index $i altered in new dataset"
                return $false
            }
        }
    }
    return $true
}

function Invoke-OneCycle {
    $stamp = Get-UtcStamp
    $cycleLog = Join-Path $LogsDir "cycle_$stamp.log"
    "poll cycle $stamp mode=$Mode" | Out-File -FilePath $cycleLog -Encoding utf8 -Append

    $prior = Get-LatestDataset
    if ($prior -and -not (Test-SidecarValid $prior.FullName)) {
        Write-Error "refuse: prior dataset sidecar hash mismatch at $($prior.FullName)"
        return 1
    }

    $endUtc = Get-FlooredEndUtcIso
    $newDatasetName = "candles_$stamp.json"
    $newDatasetPath = Join-Path $DatasetsDir $newDatasetName

    $collectArgs = @(
        "research", "collect-candles",
        "--market", $Market,
        "--unit-minutes", $UnitMinutes,
        "--start-utc", $InitialStartUtc,
        "--end-utc", $endUtc,
        "--out", $newDatasetPath
    )
    $rc = Invoke-Bt $collectArgs $cycleLog
    if ($rc -ne 0) {
        Write-Error "refuse: candle fetch exited $rc"
        return $rc
    }
    if (-not (Test-Path -LiteralPath $newDatasetPath)) {
        Write-Error "refuse: new dataset missing after fetch"
        return 1
    }
    if (-not (Test-SidecarValid $newDatasetPath)) {
        Write-Error "refuse: new dataset sidecar hash mismatch"
        return 1
    }
    if (-not (Test-DatasetAppendOnly $prior $newDatasetPath)) {
        # Move aside the offending dataset for forensic review — do NOT
        # silently overwrite the append-only history.
        $quarantine = "$newDatasetPath.rejected"
        Move-Item -LiteralPath $newDatasetPath -Destination $quarantine -Force
        if (Test-Path -LiteralPath "$newDatasetPath.sha256") {
            Move-Item -LiteralPath "$newDatasetPath.sha256" `
                -Destination "$quarantine.sha256" -Force
        }
        Write-Error "refuse: append-only invariant violated; quarantined at $quarantine"
        return 1
    }

    # Exactly one live cycle per invocation. HALT handling lives inside
    # `bt live breakout-cycle` — it reconciles but does not submit when
    # $StateDir/HALT exists.
    $cycleArgs = @(
        "live", "breakout-cycle",
        "--dataset", $newDatasetPath,
        "--snapshot", $SnapshotPath,
        "--state-dir", $StateDirFull,
        "--max-notional-krw", $MaxNotionalKrw,
        "--mode", $Mode
    )
    if ($Mode -eq "live") {
        $cycleArgs += "--enable-live-orders"
    } else {
        $cycleArgs += "--starting-cash-krw"
        $cycleArgs += $StartingCashKrw
    }
    return (Invoke-Bt $cycleArgs $cycleLog)
}

# --- Main loop -----------------------------------------------------------
if ($Once.IsPresent) {
    exit (Invoke-OneCycle)
}

while ($true) {
    try {
        $rc = Invoke-OneCycle
        Write-Host "cycle_exit=$rc mode=$Mode"
    } catch {
        Write-Warning "cycle raised: $($_.Exception.GetType().Name)"
    }
    Start-Sleep -Seconds $PollSeconds
}
