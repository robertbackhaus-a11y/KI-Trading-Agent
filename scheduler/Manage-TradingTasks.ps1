# Copyright (C) 2026 Robert Backhaus
# SPDX-License-Identifier: Apache-2.0

<#
.SYNOPSIS
  Verwaltet die geplanten Trading-Aufgaben unter C:\tools\trading.

.DESCRIPTION
  Sieben Aufgaben (Name "Trading-*"). Die vier Backfills laufen wie früher im KI-Stack bei Anmeldung plus
  täglich bzw. wöchentlich; Candidate Discovery (17:35) und Market Intelligence (08:15 und 17:45) laufen nur
  zu den festen Zeiten (kein Anmelde-Auslöser) und haben ein Zeitlimit von 2 Stunden. Alle Aufgaben holen
  verpasste Läufe nach (StartWhenAvailable) und laufen nie parallel mit sich selbst. Reihenfolge am Abend:
  MarketData 17:15, FX 17:20, EventsNews 17:25, Discovery 17:35, Market Intelligence 17:45. Sonntags: Fundamentals 10:00, danach ETF-Refresh 11:00
  (Refresh-TradingEtfData.py --write: Holdings und Sektor-Look-through von XUSE, IS3N und SPXS aus den Anbieterdaten; Exitcode ungleich 0 = Anbieter-/Parserfehler
  oder teilweise Aktualisierung, die alten gueltigen Dateien bleiben; kein Wiederholen, keine Folgejobs; Zeitlimit 1 Stunde, kein Anmelde-Ausloeser).
  Ohne -Action wird nur der Status angezeigt.

  Alle Aufgaben laufen unsichtbar: Die Aktion ist pythonw.exe (ohne Konsole) mit dem kleinen Starter app\Run-TradingTask.py <Log> <Skript> <Argumente>. Der Starter haengt stdout und
  stderr an die Logdatei an (wie das fruehere ">> log 2>&1" unter cmd.exe) und gibt den Exitcode des Skripts weiter; es oeffnet sich kein cmd-, PowerShell- oder Python-Fenster und
  der Fokus eines Spiels wird nicht gestohlen. Zeitplaene, Einstellungen, Skripte, Logs und Exitcodes bleiben wie sie sind.

  Die alten KI-Trading-* Aufgaben wurden am 02.10.2026 gelöscht. Ihre Definitionen liegen als XML unter
  scheduler\legacy\ und lassen sich mit Register-ScheduledTask -Xml wiederherstellen.

.PARAMETER Action
  Status    Zeigt die Aufgaben (Standard, ändert nichts)
  Register  Legt fehlende Aufgaben an (aktiviert)
  UpdateAction  Stellt bei vorhandenen Aufgaben NUR die Aktion auf pythonw + Starter um (Ausloeser, Einstellungen und Konto bleiben); einmalig nach dem Deploy
  Enable    Aktiviert alle
  Disable   Deaktiviert alle
  Remove    Entfernt alle

.PARAMETER DryRun
  Zeigt nur, was passieren würde.
#>
[CmdletBinding()]
param(
    [ValidateSet('Status', 'Register', 'UpdateAction', 'Enable', 'Disable', 'Remove')]
    [string]$Action = 'Status',
    [string]$Root = 'C:\tools\trading',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $Root '.venv\Scripts\python.exe'
$pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$launcher = Join-Path $Root 'app\Run-TradingTask.py'
$user = "$env:USERDOMAIN\$env:USERNAME"

# Name, Skript, Argumente, Logdatei, Zeitplan (At: eine oder mehrere Uhrzeiten)
# Optional: Logon = $false (kein Anmelde-Auslöser), WorkDir (relativ zu Root), LimitHours (Zeitlimit)
$jobs = @(
    @{ Name = 'MarketData-Backfill'; Script = 'Backfill-TradingMarketData.py';      Args = '--write'; Log = 'market-data-backfill.log';      Kind = 'Daily';  At = '17:15' },
    @{ Name = 'FXRates-Backfill';    Script = 'Backfill-TradingFXRatesECB.py';      Args = '--write --quote-currency USD,GBP,AUD,KRW'; Log = 'fx-rates-backfill.log';         Kind = 'Daily';  At = '17:20' },
    @{ Name = 'EventsNews-Backfill'; Script = 'Backfill-TradingEventsNews.py';      Args = '--write'; Log = 'events-news-backfill.log';      Kind = 'Daily';  At = '17:25' },
    @{ Name = 'Fundamentals-SEC';    Script = 'Backfill-TradingFundamentalsSEC.py'; Args = '';        Log = 'fundamentals-sec-backfill.log'; Kind = 'Weekly'; At = '10:00' },
    @{ Name = 'Candidate-Discovery'; Script = 'Discover-TradingCandidates.py';      Args = '';        Log = 'candidate-discovery.log';       Kind = 'Daily';  At = @('17:35'); Logon = $false; WorkDir = 'app'; LimitHours = 2 },
    @{ Name = 'Market-Intelligence'; Script = 'Collect-TradingMarketIntelligence.py'; Args = '';      Log = 'market-intelligence.log';       Kind = 'Daily';  At = @('08:15', '17:45'); Logon = $false; WorkDir = 'app'; LimitHours = 2 },
    @{ Name = 'ETF-Refresh';         Script = 'Refresh-TradingEtfData.py';          Args = '--write'; Log = 'etf-refresh.log';               Kind = 'Weekly'; At = '11:00'; Logon = $false; WorkDir = 'app'; LimitHours = 1 }
)

function Get-TaskLine($name) {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $t) { return "{0,-34} nicht vorhanden" -f $name }
    $i = Get-ScheduledTaskInfo -TaskName $name
    $exe = Split-Path -Leaf ([string]$t.Actions[0].Execute)
    "{0,-34} {1,-9} letzter Lauf: {2}  Ergebnis: {3}  naechster: {4}  Aktion: {5}" -f $name, $t.State, $i.LastRunTime, $i.LastTaskResult, $i.NextRunTime, $(if ($exe -ieq 'pythonw.exe') { 'unsichtbar (pythonw)' } else { "$exe (zeigt ein Fenster)" })
}

# Die Aktion einer Aufgabe: pythonw.exe (keine Konsole) + Starter + Log + Skript + Argumente; Arbeitsverzeichnis wie bisher
function Get-ActionParams($j) {
    $script = Join-Path $Root ("app\" + $j.Script)
    if (-not (Test-Path $script)) { throw "Skript fehlt: $script" }
    $log = Join-Path $Root ("logs\" + $j.Log)
    $argLine = ('"{0}" "{1}" "{2}" {3}' -f $launcher, $log, $script, $j.Args).TrimEnd()
    $params = @{ Execute = $pythonw; Argument = $argLine }
    if ($j.WorkDir) { $params.WorkingDirectory = Join-Path $Root $j.WorkDir }
    $params
}

function Assert-SilentRuntime {
    foreach ($file in @($python, $pythonw, $launcher)) { if (-not (Test-Path $file)) { throw "Fehlt: $file" } }
}

function Invoke-Step($text, [scriptblock]$block) {
    if ($DryRun) { Write-Host "[DryRun] $text" } else { Write-Host $text; & $block }
}

switch ($Action) {
    'Status' {
        foreach ($j in $jobs) { Get-TaskLine ("Trading-" + $j.Name) }
    }

    'Register' {
        Assert-SilentRuntime
        New-Item -ItemType Directory -Force (Join-Path $Root 'logs') | Out-Null
        foreach ($j in $jobs) {
            $name = "Trading-" + $j.Name
            if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { Write-Host "vorhanden: $name"; continue }
            $actionParams = Get-ActionParams $j
            Invoke-Step "Registriere $name : $($actionParams.Execute) $($actionParams.Argument)" {
                $action = New-ScheduledTaskAction @actionParams
                $triggers = @()
                if ($j.Logon -ne $false) { $triggers += New-ScheduledTaskTrigger -AtLogOn -User $user }
                foreach ($at in @($j.At)) {
                    if ($j.Kind -eq 'Daily') { $triggers += New-ScheduledTaskTrigger -Daily -At $at }
                    else { $triggers += New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $at }
                }
                $settingsParams = @{ StartWhenAvailable = $true; MultipleInstances = 'IgnoreNew' }
                if ($j.LimitHours) { $settingsParams.ExecutionTimeLimit = New-TimeSpan -Hours $j.LimitHours }
                $settings = New-ScheduledTaskSettingsSet @settingsParams
                $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
                Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description "Trading (C:\tools\trading): $($j.Script)" | Out-Null
            }
        }
    }

    'UpdateAction' {
        # nur die Aktion: Ausloeser, Einstellungen, Konto und Beschreibung der vorhandenen Aufgaben bleiben unveraendert
        Assert-SilentRuntime
        foreach ($j in $jobs) {
            $name = "Trading-" + $j.Name
            if (-not (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) { Write-Host "nicht vorhanden (Register legt sie an): $name"; continue }
            $actionParams = Get-ActionParams $j
            Invoke-Step "Aktion von $name -> $($actionParams.Execute) $($actionParams.Argument)" {
                Set-ScheduledTask -TaskName $name -Action (New-ScheduledTaskAction @actionParams) | Out-Null
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
