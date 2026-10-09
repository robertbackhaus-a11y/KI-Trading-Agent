<#
.SYNOPSIS
  Small, explicit deploy of the Trading Agent runtime files from this repo to the production root.

.DESCRIPTION
  Only the files listed in deploy-manifest.json are ever copied (no wildcard copy of the repo).
  Never touched: tests, docs, .git, the database, logs, data, existing backups, the scheduled tasks and the private
  local files listed under "protected" (app\universe\sector_map_local.json).
  No schema migration, no collector run, no task registration, no orders.

.PARAMETER Action
  Check     Read-only checks (default). Changes nothing.
  Deploy    Preflight, backup, copy, verify, smoke test. Prints what to do next.
  Rollback  Copies the files saved in -BackupPath back. The database is restored only with -RestoreDb.

.PARAMETER Root        Production root (default C:\tools\trading).
.PARAMETER Manifest    Manifest file (default: deploy-manifest.json next to this script).
.PARAMETER BackupPath  Rollback only: the backup folder, e.g. C:\tools\trading\backup\deploy-20261010-093000.
.PARAMETER RestoreDb   Rollback only: also restore the database backup recorded in that folder (explicit, never automatic).
.PARAMETER DryRun      Deploy/Rollback: show the plan, change nothing.
.PARAMETER SkipSmoke   Skip the MCP smoke test (needs the production venv with the mcp package).
.PARAMETER Python      Python used for helpers and smoke test (default: <Root>\.venv\Scripts\python.exe).

.EXAMPLE
  powershell -File deploy\Deploy-TradingAgent.ps1 -Action Check
  powershell -File deploy\Deploy-TradingAgent.ps1 -Action Deploy
  powershell -File deploy\Deploy-TradingAgent.ps1 -Action Rollback -BackupPath C:\tools\trading\backup\deploy-20261010-093000
#>
[CmdletBinding()]
param(
    [ValidateSet('Check', 'Deploy', 'Rollback')]
    [string]$Action = 'Check',
    [string]$Root = 'C:\tools\trading',
    [string]$Manifest,
    [string]$BackupPath,
    [switch]$RestoreDb,
    [switch]$DryRun,
    [switch]$SkipSmoke,
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$env:PYTHONDONTWRITEBYTECODE = '1'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path   # $PSScriptRoot is empty inside param() on Windows PowerShell 5.1
$repo = Split-Path -Parent $scriptDir
$helpers = Join-Path $scriptDir 'deploy_helpers.py'
if (-not $Manifest) { $Manifest = Join-Path $scriptDir 'deploy-manifest.json' }
if (-not $Python) { $Python = Join-Path $Root '.venv\Scripts\python.exe' }
$dbPath = Join-Path $Root 'data\trading.db'
$allowedTargets = @('app', 'app/universe', 'mcp', 'scheduler')
$forbiddenName = '(\.(db|sqlite3?|csv|xlsx?|log|bak|pem|key|env)$)|(^\.env)|latest\.json|_local\.|secret|credential|backup'

$script:errors = New-Object System.Collections.ArrayList
$script:warnings = New-Object System.Collections.ArrayList
function Add-Err([string]$m) { [void]$script:errors.Add($m); Write-Host "  ERROR  $m" }
function Add-Warn([string]$m) { [void]$script:warnings.Add($m); Write-Host "  WARN   $m" }
function Write-Ok([string]$m) { Write-Host "  ok     $m" }
function Get-Sha([string]$p) { (Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash }
function Get-RelText([string]$t, [string]$f) { ($t + '\' + $f).Replace('/', '\') }

function Invoke-Helper {
    param([string[]]$HelperArgs)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # Windows PowerShell 5.1 turns native stderr output into a terminating error otherwise
    try { $out = & $Python $helpers @HelperArgs 2>$null; $code = $LASTEXITCODE } finally { $ErrorActionPreference = $previous }
    $last = @($out | Where-Object { $_ -match '^\s*\{' }) | Select-Object -Last 1
    $json = $null
    if ($last) { try { $json = $last | ConvertFrom-Json } catch { $json = $null } }
    [pscustomobject]@{ Code = $code; Json = $json; Raw = ($out -join ' ') }
}

function Test-PsSyntax([string]$path) {
    $errs = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$errs)
    return (@($errs).Count -eq 0)
}

function Read-Manifest {
    if (-not (Test-Path -LiteralPath $Manifest)) { Add-Err "manifest not found: $Manifest"; return $null }
    try { $m = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json } catch { Add-Err "manifest is not valid JSON: $($_.Exception.Message)"; return $null }
    if ($m.manifest_version -ne 1) { Add-Err "unsupported manifest_version: $($m.manifest_version)"; return $null }
    return $m
}

function Get-Entries($m) {
    $protected = @($m.protected | ForEach-Object { (Join-Path $Root $_).ToLowerInvariant() })
    $seen = @{}
    $list = New-Object System.Collections.ArrayList
    foreach ($g in $m.groups) {
        if ($allowedTargets -notcontains $g.target) { Add-Err "group '$($g.name)': target '$($g.target)' is not allowed"; continue }
        foreach ($f in $g.files) {
            if ($f -match '[\\/\*\?:]' ) { Add-Err "manifest entry '$f' must be a plain file name"; continue }
            if ($f -match $forbiddenName) { Add-Err "manifest entry '$f' looks like data/private/log/backup"; continue }
            $source = Join-Path $repo (Join-Path $g.source $f)
            $target = Join-Path $Root (Join-Path $g.target $f)
            if ($protected -contains $target.ToLowerInvariant()) { Add-Err "manifest entry '$f' targets a protected file"; continue }
            $key = $target.ToLowerInvariant()
            if ($seen.ContainsKey($key)) { Add-Err "duplicate manifest entry: $(Get-RelText $g.target $f)"; continue }
            $seen[$key] = $true
            [void]$list.Add([pscustomobject]@{ Rel = (Get-RelText $g.target $f); Source = $source; Target = $target; State = '' })
        }
    }
    return , $list
}

# ---------------------------------------------------------------- Check (read-only)
function Invoke-Check {
    Write-Host "== Check  (repo: $repo | root: $Root)"
    Write-Host "[repo]"
    foreach ($d in 'tools\trading', 'mcp-tools', 'scheduler', 'deploy') {
        if (Test-Path -LiteralPath (Join-Path $repo $d)) { Write-Ok "repo folder $d" } else { Add-Err "repo folder missing: $d" }
    }
    Write-Host "[production]"
    if (-not (Test-Path -LiteralPath $Root)) { Add-Err "production root missing: $Root" }
    foreach ($d in 'app', 'app\universe', 'mcp', 'scheduler', 'data', 'backup') {
        if (Test-Path -LiteralPath (Join-Path $Root $d)) { Write-Ok "folder $d" } else { Add-Err "production folder missing: $d" }
    }
    if (Test-Path -LiteralPath $Python) { Write-Ok "python $Python" } else { Add-Err "python missing: $Python" }
    if (-not (Test-Path -LiteralPath $helpers)) { Add-Err "helper missing: $helpers" }

    Write-Host "[manifest]"
    $m = Read-Manifest
    if (-not $m) { return $null }
    $entries = Get-Entries $m
    if ($script:errors.Count -eq 0) { Write-Ok "$($entries.Count) runtime files listed in $(Split-Path -Leaf $Manifest)" }
    foreach ($e in $entries) { if (-not (Test-Path -LiteralPath $e.Source)) { Add-Err "source file missing: $($e.Rel)" } }

    if ($script:errors.Count -eq 0) {
        Write-Host "[syntax]"
        $py = @($entries | Where-Object { $_.Source -like '*.py' } | ForEach-Object { $_.Source })
        $js = @($entries | Where-Object { $_.Source -like '*.json' } | ForEach-Object { $_.Source })
        $ps = @($entries | Where-Object { $_.Source -like '*.ps1' } | ForEach-Object { $_.Source })
        $r = Invoke-Helper (@('compile') + $py)
        if ($r.Code -eq 0) { Write-Ok "$($py.Count) python files compile" } else { Add-Err "python compile: $($r.Raw)" }
        if ($js.Count) { $r = Invoke-Helper (@('json') + $js); if ($r.Code -eq 0) { Write-Ok "$($js.Count) json files valid" } else { Add-Err "json: $($r.Raw)" } }
        foreach ($p in $ps) { if (Test-PsSyntax $p) { Write-Ok "powershell syntax $(Split-Path -Leaf $p)" } else { Add-Err "powershell syntax error: $p" } }
        $scriptSelf = Join-Path $scriptDir 'Deploy-TradingAgent.ps1'
        if (Test-PsSyntax $scriptSelf) { Write-Ok "powershell syntax Deploy-TradingAgent.ps1" } else { Add-Err "powershell syntax error: Deploy-TradingAgent.ps1" }
    }

    Write-Host "[database]"
    if (Test-Path -LiteralPath $dbPath) {
        Write-Ok "database present ($([math]::Round((Get-Item -LiteralPath $dbPath).Length / 1MB, 1)) MB)"
        $r = Invoke-Helper @('integrity', $dbPath)
        if ($r.Code -eq 0) { Write-Ok "integrity_check ok" } else { Add-Err "integrity_check failed: $($r.Raw)" }
    } else { Add-Err "database missing: $dbPath" }

    Write-Host "[plan]"
    $new = 0; $upd = 0; $same = 0
    foreach ($e in $entries) {
        if (-not (Test-Path -LiteralPath $e.Target)) { $e.State = 'new'; $new++ }
        elseif ((Test-Path -LiteralPath $e.Source) -and ((Get-Sha $e.Source) -eq (Get-Sha $e.Target))) { $e.State = 'identical'; $same++ }
        else { $e.State = 'update'; $upd++ }
    }
    Write-Ok "identical: $same | to update: $upd | new: $new"
    foreach ($e in $entries) { if ($e.State -ne 'identical') { Write-Host ("         {0,-7} {1}" -f $e.State, $e.Rel) } }

    Write-Host "[access]"
    $blocked = 0
    foreach ($e in $entries) {
        if ($e.State -eq 'update') {
            try { $fs = [System.IO.File]::Open($e.Target, 'Open', 'Write', 'ReadWrite'); $fs.Close() } catch { $blocked++; Add-Err "target not writable (in use?): $($e.Rel)" }
        }
    }
    if ($blocked -eq 0) { Write-Ok "existing target files can be opened for writing" }
    if (Test-Path -LiteralPath $Root) {
        $drive = (Get-Item -LiteralPath $Root).PSDrive
        $need = 0; if (Test-Path -LiteralPath $dbPath) { $need = [long]((Get-Item -LiteralPath $dbPath).Length * 2.5) }
        $need += 100MB
        if ($drive -and $drive.Free -ne $null -and $drive.Free -lt $need) { Add-Err "not enough free space on $($drive.Name): (need ~$([math]::Round($need / 1MB)) MB)" } else { Write-Ok "free space sufficient for backups" }
    }

    Write-Host "[protected private files]"
    foreach ($p in $m.protected) {
        $full = Join-Path $Root $p
        if (Test-Path -LiteralPath $full) { Write-Ok "present and never touched: $p" } else { Add-Warn "protected file missing (not created from the repo): $p" }
    }
    $managed = @($entries | ForEach-Object { $_.Target.ToLowerInvariant() })
    $extra = @()
    foreach ($d in 'app', 'mcp', 'scheduler') {
        $dirPath = Join-Path $Root $d
        if (Test-Path -LiteralPath $dirPath) {
            $extra += @(Get-ChildItem -LiteralPath $dirPath -File | Where-Object { $managed -notcontains $_.FullName.ToLowerInvariant() } | ForEach-Object { "$d\$($_.Name)" })
        }
    }
    if ($extra.Count) { Write-Host "  info   production-only files (not managed, never touched): $($extra -join ', ')" }
    return [pscustomobject]@{ Manifest = $m; Entries = $entries }
}

# ---------------------------------------------------------------- shared verification / smoke
function Test-Deployed($entries, $m) {
    $ok = $true
    Write-Host "[verify]"
    foreach ($e in $entries) {
        if (-not (Test-Path -LiteralPath $e.Target) -or ((Get-Sha $e.Source) -ne (Get-Sha $e.Target))) { Add-Err "hash mismatch after copy: $($e.Rel)"; $ok = $false }
    }
    if ($ok) { Write-Ok "all $($entries.Count) manifest files: repo hash == production hash" }
    return (Test-Runtime $entries $m)
}

function Test-Runtime($entries, $m) {
    $ok = $true
    $py = @($entries | Where-Object { $_.Target -like '*.py' } | ForEach-Object { $_.Target })
    $js = @($entries | Where-Object { $_.Target -like '*.json' } | ForEach-Object { $_.Target })
    if ($py.Count) {
        $r = Invoke-Helper (@('compile') + $py)
        if ($r.Code -eq 0) { Write-Ok "py_compile: $($py.Count) production python files" } else { Add-Err "py_compile failed: $($r.Raw)"; $ok = $false }
    }
    if ($js.Count) { $r = Invoke-Helper (@('json') + $js); if ($r.Code -eq 0) { Write-Ok "json valid" } else { Add-Err "json invalid: $($r.Raw)"; $ok = $false } }
    foreach ($e in @($entries | Where-Object { $_.Target -like '*.ps1' })) { if (Test-PsSyntax $e.Target) { Write-Ok "powershell syntax $($e.Rel)" } else { Add-Err "powershell syntax error: $($e.Rel)"; $ok = $false } }
    $r = Invoke-Helper @('integrity', $dbPath)
    if ($r.Code -eq 0) { Write-Ok "database integrity_check ok" } else { Add-Err "database integrity_check failed: $($r.Raw)"; $ok = $false }
    if (-not (Invoke-Smoke $m)) { $ok = $false }
    return $ok
}

function Invoke-Smoke($m) {
    Write-Host "[mcp smoke test]"
    if ($SkipSmoke) { Add-Warn "smoke test skipped (-SkipSmoke)"; return $true }
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("trading-smoke-" + [guid]::NewGuid().ToString('N') + '.db')
    try {
        $b = Invoke-Helper @('backup', $dbPath, $tmp)
        if ($b.Code -ne 0) { Add-Err "smoke: could not copy the database: $($b.Raw)"; return $false }
        $s = Invoke-Helper @('smoke', '--server', (Join-Path $Root 'mcp\server.py'), '--db', $tmp, '--expect-tools', [string]$m.expected_tool_count)
        if ($s.Code -eq 0) { Write-Ok "server started, $($s.Json.tool_count) tools (expected $($m.expected_tool_count)), database_status ok (run on a temporary database copy)"; return $true }
        $detail = if ($s.Json) { ($s.Json | ConvertTo-Json -Compress) } else { $s.Raw }
        Add-Err "smoke test failed: $detail"
        return $false
    } finally { Remove-Item -Path ($tmp + '*') -Force -ErrorAction SilentlyContinue }
}

function Write-NextSteps {
    Write-Host ""
    Write-Host "NEXT STEPS (not done by this script):"
    Write-Host "  - Restart every running MCP process / front end (Python modules are cached per process)."
    Write-Host "  - Scheduled tasks were NOT re-registered and no collector was started."
}

# ---------------------------------------------------------------- Deploy
function Invoke-Deploy {
    $c = Invoke-Check
    if ($script:errors.Count -gt 0 -or -not $c) { Write-Host ""; Write-Host "DEPLOY ABORTED: preflight failed, nothing was changed."; exit 1 }
    $changes = @($c.Entries | Where-Object { $_.State -ne 'identical' })
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupDir = Join-Path $Root "backup\deploy-$stamp"
    $dbBackup = Join-Path $Root "data\trading.db.bak-deploy-$stamp"
    Write-Host ""
    if ($changes.Count -eq 0) { Write-Host "NOTHING TO DEPLOY: production already equals the manifest files. No backup written."; exit 0 }
    if ($DryRun) {
        Write-Host "DRY RUN - would do (nothing is changed):"
        Write-Host "  backup folder : $backupDir  (existing files that will be replaced: $(@($changes | Where-Object { $_.State -eq 'update' }).Count))"
        Write-Host "  database copy: $dbBackup"
        foreach ($e in $changes) { Write-Host ("  copy {0,-7} {1}" -f $e.State, $e.Rel) }
        Write-Host "  then: hash compare, py_compile, powershell syntax, integrity_check, mcp smoke test"
        exit 0
    }
    Write-Host "== Deploy $stamp"
    Write-Host "[backup]"
    $record = [ordered]@{ created = $stamp; root = $Root; files = @(); db_backup = $null }
    foreach ($e in $changes) {
        $item = [ordered]@{ rel = $e.Rel; existed = $false; sha256_before = $null; sha256_new = (Get-Sha $e.Source) }
        if ($e.State -eq 'update') {
            $dest = Join-Path $backupDir $e.Rel
            New-Item -ItemType Directory -Force (Split-Path -Parent $dest) | Out-Null
            Copy-Item -LiteralPath $e.Target -Destination $dest
            $item.existed = $true; $item.sha256_before = Get-Sha $e.Target
        }
        $record.files += $item
    }
    New-Item -ItemType Directory -Force $backupDir | Out-Null
    Write-Ok "files backed up to $backupDir"
    $b = Invoke-Helper @('backup', $dbPath, $dbBackup)
    if ($b.Code -ne 0) { Add-Err "database backup failed: $($b.Raw)"; Write-Host "DEPLOY ABORTED before copying: nothing was changed in production."; exit 1 }
    $record.db_backup = $dbBackup
    Write-Ok "database backup (SQLite backup API, integrity ok): $dbBackup"
    ($record | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath (Join-Path $backupDir 'backup-manifest.json') -Encoding UTF8
    $hashesBefore = @{}
    foreach ($e in $c.Entries) { if (Test-Path -LiteralPath $e.Target) { $hashesBefore[$e.Rel] = Get-Sha $e.Target } }
    Write-Ok "hashes before deploy recorded for $($hashesBefore.Count) existing files (backup-manifest.json lists those being replaced)"

    Write-Host "[copy]"
    foreach ($e in $changes) { Copy-Item -LiteralPath $e.Source -Destination $e.Target -Force; Write-Host ("  copied {0,-7} {1}" -f $e.State, $e.Rel) }

    $ok = Test-Deployed $c.Entries $c.Manifest
    foreach ($p in $c.Manifest.protected) { if (-not (Test-Path -LiteralPath (Join-Path $Root $p))) { Add-Warn "protected file is missing in production (not created from the repo): $p" } }
    Write-Host ""
    if ($ok) {
        Write-Host "DEPLOY OK  (backup: $backupDir | database copy: $dbBackup)"
        Write-Host "  Roll back with: powershell -File deploy\Deploy-TradingAgent.ps1 -Action Rollback -BackupPath `"$backupDir`""
        Write-NextSteps
        exit 0
    }
    Write-Host "DEPLOY FAILED after copying - files are in place but verification reported errors (see above)."
    Write-Host "  Roll back with: powershell -File deploy\Deploy-TradingAgent.ps1 -Action Rollback -BackupPath `"$backupDir`""
    exit 1
}

# ---------------------------------------------------------------- Rollback
function Invoke-Rollback {
    if (-not $BackupPath) { Write-Host "ROLLBACK needs an explicit -BackupPath <backup folder>. Nothing was changed."; exit 1 }
    $recFile = Join-Path $BackupPath 'backup-manifest.json'
    if (-not (Test-Path -LiteralPath $recFile)) { Write-Host "ROLLBACK ABORTED: no backup-manifest.json in $BackupPath"; exit 1 }
    $rec = Get-Content -LiteralPath $recFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $m = Read-Manifest
    if (-not $m) { exit 1 }
    Write-Host "== Rollback from $BackupPath"
    $restore = @($rec.files | Where-Object { $_.existed })
    $created = @($rec.files | Where-Object { -not $_.existed })
    foreach ($f in $restore) {
        $src = Join-Path $BackupPath $f.rel
        if (-not (Test-Path -LiteralPath $src)) { Add-Err "backup file missing: $($f.rel)" }
        elseif ((Get-Sha $src) -ne $f.sha256_before) { Add-Err "backup file is corrupt (hash differs from backup-manifest): $($f.rel)" }
        # only runtime files that this manifest manages may be restored
        if (-not ($m.groups | ForEach-Object { foreach ($n in $_.files) { Get-RelText $_.target $n } } | Where-Object { $_ -eq $f.rel.Replace('/', '\') })) { Add-Err "not a manifest file, will not restore: $($f.rel)" }
    }
    if ($RestoreDb) {
        if (-not $rec.db_backup -or -not (Test-Path -LiteralPath $rec.db_backup)) { Add-Err "database backup recorded in the backup folder is missing: $($rec.db_backup)" }
    }
    if ($script:errors.Count -gt 0) { Write-Host "ROLLBACK ABORTED: nothing was changed."; exit 1 }
    if ($DryRun) {
        Write-Host "DRY RUN - would restore $($restore.Count) files (nothing is changed):"
        foreach ($f in $restore) { Write-Host "  restore $($f.rel)" }
        if ($created.Count) { Write-Host "  files created by that deploy stay in place: $(($created | ForEach-Object { $_.rel }) -join ', ')" }
        if ($RestoreDb) { Write-Host "  database would be restored from $($rec.db_backup) (current database copied to trading.db.bak-pre-rollback-<stamp> first)" } else { Write-Host "  database is NOT restored (needs -RestoreDb)" }
        exit 0
    }
    foreach ($f in $restore) {
        $dest = Join-Path $Root $f.rel
        Copy-Item -LiteralPath (Join-Path $BackupPath $f.rel) -Destination $dest -Force
        if ((Get-Sha $dest) -ne $f.sha256_before) { Add-Err "hash mismatch after restore: $($f.rel)" } else { Write-Host "  restored $($f.rel)" }
    }
    if ($created.Count) { Write-Host "  note: files created by that deploy were not removed: $(($created | ForEach-Object { $_.rel }) -join ', ')" }
    if ($RestoreDb) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $safety = Join-Path $Root "data\trading.db.bak-pre-rollback-$stamp"
        $b = Invoke-Helper @('backup', $dbPath, $safety)
        if ($b.Code -ne 0) { Add-Err "could not copy the current database before restoring: $($b.Raw)" }
        else {
            Write-Ok "current database copied to $safety"
            $r = Invoke-Helper @('restore', $rec.db_backup, $dbPath)
            if ($r.Code -eq 0) { Write-Ok "database restored from $($rec.db_backup)" } else { Add-Err "database restore failed: $($r.Raw)" }
        }
    } else { Write-Host "  database left untouched (restore only with -RestoreDb)" }
    $entries = New-Object System.Collections.ArrayList
    foreach ($f in $restore) { [void]$entries.Add([pscustomobject]@{ Rel = $f.rel; Source = $null; Target = (Join-Path $Root $f.rel) }) }
    $ok = ($script:errors.Count -eq 0) -and (Test-Runtime $entries $m)
    Write-Host ""
    if ($ok) { Write-Host "ROLLBACK OK"; Write-NextSteps; exit 0 }
    Write-Host "ROLLBACK FAILED (see above)"; exit 1
}

switch ($Action) {
    'Check' {
        $c = Invoke-Check
        Write-Host ""
        if ($script:errors.Count -eq 0 -and $c) { Write-Host "CHECK OK ($($script:warnings.Count) warning(s)). Nothing was changed."; exit 0 }
        Write-Host "CHECK FAILED ($($script:errors.Count) error(s)). Nothing was changed."; exit 1
    }
    'Deploy' { Invoke-Deploy }
    'Rollback' { Invoke-Rollback }
}
