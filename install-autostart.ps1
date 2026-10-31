# Registra (o actualitza) l'arrencada automàtica de coniMIX a Windows:
#  - "coniMIX": servidor en iniciar el sistema, abans d'entrar a Windows (S4U, sense contrasenya desada).
#    Espera 45 s perquè la xarxa estigui a punt: arrencat massa d'hora, queda penjat.
#  - "coniMIX Watchdog": cada 2 minuts comprova que respon i, si no, el reinicia (watchdog.ps1).
# Executar des d'aquesta carpeta:  powershell -ExecutionPolicy Bypass -File install-autostart.ps1
$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited

$serverTrigger = New-ScheduledTaskTrigger -AtStartup
$serverTrigger.Delay = "PT45S"
$serverSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$serverAction = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\pythonw.exe" -Argument "server.py" -WorkingDirectory $dir
Register-ScheduledTask -TaskName "coniMIX" -Action $serverAction -Trigger $serverTrigger -Principal $principal `
    -Settings $serverSettings -Force | Out-Null

$watchTrigger = New-ScheduledTaskTrigger -AtStartup
$watchTrigger.Delay = "PT3M"
$watchTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2)).Repetition
$watchSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$watchAction = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $dir `
    -Argument "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dir\watchdog.ps1`""
Register-ScheduledTask -TaskName "coniMIX Watchdog" -Action $watchAction -Trigger $watchTrigger -Principal $principal `
    -Settings $watchSettings -Force | Out-Null

"Tasques registrades: coniMIX (arrencada +45 s) i coniMIX Watchdog (cada 2 min)."
