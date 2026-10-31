# Vigilant de coniMIX: si el servidor no respon, reinicia la tasca programada "coniMIX".
# L'executa la tasca "coniMIX Watchdog" cada 2 minuts (vegeu install-autostart.ps1).
# Cal perquè, després d'un tall de llum, el servidor pot quedar escoltant el port però penjat.
param(
    [string]$Url = "http://127.0.0.1:8791/",
    [string]$TaskName = "coniMIX",
    [int]$FailureThreshold = 2
)

$ErrorActionPreference = "Stop"
$Data = Join-Path $PSScriptRoot "data"
$LogPath = Join-Path $Data "watchdog.log"
$StatePath = Join-Path $Data "watchdog-failures.txt"
$mutex = [Threading.Mutex]::new($false, "Local\coniMIXWatchdog")

function Write-Log([string]$Message) {
    Add-Content -LiteralPath $LogPath -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) -Encoding UTF8
}

function Test-Server {
    $response = $null
    try {
        $request = [Net.HttpWebRequest]::Create($Url)
        $request.Timeout = 15000
        $request.ReadWriteTimeout = 15000
        $request.AllowAutoRedirect = $false
        $response = $request.GetResponse()
        return $true
    }
    catch [Net.WebException] {
        # Qualsevol resposta HTTP (p. ex. 401) vol dir que el servidor és viu.
        return ($null -ne $_.Exception.Response)
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $response) { $response.Dispose() }
    }
}

function Restart-Server {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    # El pythonw.exe del .venv és un llançador: el procés que escolta és un fill que pot sobreviure.
    $owners = Get-NetTCPConnection -LocalPort ([Uri]$Url).Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($id in $owners) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$id"
        if ($proc -and $proc.Name -like "python*") {
            Stop-Process -Id $proc.ParentProcessId -Force -ErrorAction SilentlyContinue
            Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 3
    Start-ScheduledTask -TaskName $TaskName
}

if (-not $mutex.WaitOne(0)) { exit 0 }

try {
    New-Item -ItemType Directory -Force -Path $Data | Out-Null
    $failures = 0
    if (Test-Path -LiteralPath $StatePath) { $failures = [int](Get-Content -LiteralPath $StatePath -Raw) }

    if (Test-Server) {
        if ($failures -gt 0) { Write-Log "OK: coniMIX torna a respondre" }
        Set-Content -LiteralPath $StatePath -Value 0
        exit 0
    }

    $failures++
    $running = (Get-ScheduledTask -TaskName $TaskName).State -eq "Running"
    Write-Log "ERROR: coniMIX no respon ($failures/$FailureThreshold, tasca en marxa: $running)"

    # Si la tasca ni tan sols és en marxa, no cal esperar: s'engega de seguida.
    if ($running -and $failures -lt $FailureThreshold) {
        Set-Content -LiteralPath $StatePath -Value $failures
        exit 1
    }

    Write-Log "ACCIO: reiniciant la tasca $TaskName"
    Restart-Server
    Set-Content -LiteralPath $StatePath -Value 0
    Start-Sleep -Seconds 10
    if (Test-Server) { Write-Log "OK: coniMIX respon després del reinici"; exit 0 }
    Write-Log "ERROR: coniMIX encara no respon després del reinici"
    exit 1
}
catch {
    Write-Log "ERROR inesperat: $($_.Exception.Message)"
    exit 1
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
