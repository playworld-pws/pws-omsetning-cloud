$ErrorActionPreference = "Continue"

$CloudUrl = $env:PWS_CLOUD_URL
$IngestKey = $env:PWS_INGEST_KEY
$SorlandetPath = $env:PWS_SORLANDET_JSON
$TriadenPath = $env:PWS_TRIADEN_JSON
$IntervalSeconds = 30

if ([string]::IsNullOrWhiteSpace($CloudUrl)) { throw "Mangler PWS_CLOUD_URL" }
if ([string]::IsNullOrWhiteSpace($IngestKey)) { throw "Mangler PWS_INGEST_KEY" }

$headers = @{ "X-PWS-Ingest-Key" = $IngestKey }
$lastHash = @{ sorlandet = ""; triaden = "" }

function Get-FileHashString {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash
}

function Send-PwsJson {
    param([string]$Park, [string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    if (-not (Test-Path $Path)) {
        Write-Host "[$Park] Fant ikke fil: $Path"
        return
    }
    try {
        $currentHash = Get-FileHashString -Path $Path
        if ($currentHash -eq $lastHash[$Park]) { return }
        $body = Get-Content $Path -Raw -Encoding UTF8
        $null = $body | ConvertFrom-Json
        $result = Invoke-RestMethod -Uri "$CloudUrl/api/ingest/$Park" -Method Post -Headers $headers -ContentType "application/json; charset=utf-8" -Body $body -TimeoutSec 20
        $lastHash[$Park] = $currentHash
        Write-Host "[$Park] Lastet opp $([System.IO.Path]::GetFileName($Path)) -> snapshot $($result.snapshot_id)"
    }
    catch {
        Write-Host "[$Park] Feil: $($_.Exception.Message)"
    }
}

Write-Host "PWS Cloud collector startet."
Write-Host "Sky: $CloudUrl"
Write-Host "Intervall: $IntervalSeconds sek"
Write-Host "Trykk Ctrl+C for å stoppe."
Write-Host ""

while ($true) {
    Send-PwsJson -Park "sorlandet" -Path $SorlandetPath
    Send-PwsJson -Park "triaden" -Path $TriadenPath
    Start-Sleep -Seconds $IntervalSeconds
}
