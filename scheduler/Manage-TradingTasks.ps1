<#
.SYNOPSIS
  Verwaltet die geplanten Trading-Aufgaben unter C:\tools\trading.

.DESCRIPTION
  Vier Aufgaben (Name "Trading-*"), gleiche Auslöser wie früher im KI-Stack:
  bei Anmeldung plus täglich bzw. wöchentlich, mit Nachholen (StartWhenAvailable), wenn der Rechner aus war,
  und ohne parallele Instanz. Ohne -Action wird nur der Status angezeigt.

  Die alten KI-Trading-* Aufgaben wurden am 02.10.2026 gelöscht. Ihre Definitionen liegen als XML unter
  scheduler\legacy\ und lassen sich mit Register-ScheduledTask -Xml wiederherstellen.

.PARAMETER Action
  Status    Zeigt die Aufgaben (Standard, ändert nichts)
  Register  Legt fehlende Aufgaben an (aktiviert)
  Enable    Aktiviert alle
  Disable   Deaktiviert alle
  Remove    Entfernt alle

.PARAMETER DryRun
  Zeigt nur, was passieren würde.
#>
[CmdletBinding()]
param(
    [ValidateSet('Status', 'Register', 'Enable', 'Disable', 'Remove')]
    [string]$Action = 'Status',
    [string]$Root = 'C:\tools\trading',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $Root '.venv\Scripts\python.exe'
$user = "$env:USERDOMAIN\$env:USERNAME"

# Name, Skript, Argumente, Logdatei, Zeitplan
$jobs = @(
    @{ Name = 'MarketData-Backfill'; Script = 'Backfill-TradingMarketData.py';      Args = '--write'; Log = 'market-data-backfill.log';      Kind = 'Daily';  At = '17:15' },
    @{ Name = 'FXRates-Backfill';    Script = 'Backfill-TradingFXRatesECB.py';      Args = '--write --quote-currency USD,GBP,AUD,KRW'; Log = 'fx-rates-backfill.log';         Kind = 'Daily';  At = '17:20' },
    @{ Name = 'EventsNews-Backfill'; Script = 'Backfill-TradingEventsNews.py';      Args = '--write'; Log = 'events-news-backfill.log';      Kind = 'Daily';  At = '17:25' },
    @{ Name = 'Fundamentals-SEC';    Script = 'Backfill-TradingFundamentalsSEC.py'; Args = '';        Log = 'fundamentals-sec-backfill.log'; Kind = 'Weekly'; At = '10:00' }
)

function Get-TaskLine($name) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $t) { return "{0,-34} nicht vorhanden" -f $name }
    $i = Get-ScheduledTaskInfo -TaskName $name
    "{0,-34} {1,-9} letzter Lauf: {2}  Ergebnis: {3}  naechster: {4}" -f $name, $t.State, $i.LastRunTime, $i.LastTaskResult, $i.NextRunTime
}

function Invoke-Step($text, [scriptblock]$block) {
    if ($DryRun) { Write-Host "[DryRun] $text" } else { Write-Host $text; & $block }
}

switch ($Action) {
    'Status' {
        foreach ($j in $jobs) { Get-TaskLine ("Trading-" + $j.Name) }
    }

    'Register' {
        if (-not (Test-Path $python)) { throw "Python-Umgebung fehlt: $python" }
        New-Item -ItemType Directory -Force (Join-Path $Root 'logs') | Out-Null
        foreach ($j in $jobs) {
            $name = "Trading-" + $j.Name
            if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { Write-Host "vorhanden: $name"; continue }
            $script = Join-Path $Root ("app\" + $j.Script)
            if (-not (Test-Path $script)) { throw "Skript fehlt: $script" }
            $log = Join-Path $Root ("logs\" + $j.Log)
            $argLine = ('/c ""{0}" "{1}" {2} >> "{3}" 2>&1"' -f $python, $script, $j.Args, $log) -replace '\s+>>', ' >>'
            Invoke-Step "Registriere $name : cmd.exe $argLine" {
                $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $argLine
                $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
                if ($j.Kind -eq 'Daily') { $time = New-ScheduledTaskTrigger -Daily -At $j.At }
                else { $time = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $j.At }
                $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
                $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
                Register-ScheduledTask -TaskName $name -Action $action -Trigger @($logon, $time) -Settings $settings -Principal $principal -Description "Trading (C:\tools\trading): $($j.Script)" | Out-Null
            }
        }
    }

    'Enable' {
        foreach ($j in $jobs) { Invoke-Step ("Aktiviere Trading-" + $j.Name) { Enable-ScheduledTask -TaskName ("Trading-" + $j.Name) | Out-Null } }
    }

    'Disable' {
        foreach ($j in $jobs) { Invoke-Step ("Deaktiviere Trading-" + $j.Name) { Disable-ScheduledTask -TaskName ("Trading-" + $j.Name) | Out-Null } }
    }

    'Remove' {
        foreach ($j in $jobs) {
            $n = "Trading-" + $j.Name
            if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) { Invoke-Step "Entferne $n" { Unregister-ScheduledTask -TaskName $n -Confirm:$false } }
        }
    }
}
