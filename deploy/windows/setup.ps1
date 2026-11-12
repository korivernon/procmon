# ----------------------------------------------------------------------------
# procmon/deploy/windows/setup.ps1
# Sets up a Windows machine (VSERVER) to run procmon and the projects it controls, for the signed-in user only:
# nothing needs an administrator, and no Windows security or firewall setting is touched.
#
#   Python 3.11  per-user install (LOCALAPPDATA\Programs\Python\Python311)
#   Chrome       per-user install, for projects that drive a browser (Amtrakker)
#   Postgres 16  the portable zip, in LOCALAPPDATA\procmon-node, listening on localhost:<PgPort> only
#   procmon      %USERPROFILE%\Development\procmon with its own venv, started at login from the Startup folder
#
# Re-running is safe: every step checks first. Usage (PowerShell):
#   .\setup.ps1 -Bundle <url or path of bundle.zip> [-PgPort 5433]
# The bundle (made on the Mac by deploy/make_bundle.py) holds procmon, the projects being moved, and a SQL dump of
# their schemas. Secrets are not in it: each project's .env is written separately.
# ----------------------------------------------------------------------------
param(
    [string]$Bundle = "",
    [int]$PgPort = 5433,
    [string]$Root = (Join-Path $env:USERPROFILE "Development")
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"          # Invoke-WebRequest is 10x slower with the progress bar
$Base = Join-Path $env:LOCALAPPDATA "procmon-node"
New-Item -ItemType Directory -Force -Path $Base, $Root | Out-Null
function Step($t) { Write-Output "== $t" }

# ---- Python -------------------------------------------------------------------------------------------------------
$Py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"
if (-not (Test-Path $Py)) {
    Step "Installing Python 3.11 (per user)"
    $exe = Join-Path $Base "python-3.11.9-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $exe
    Start-Process $exe -Wait -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_launcher=0"
}
if (-not (Test-Path $Py)) { throw "Python did not install at $Py" }
Step "Python: $(& $Py --version)"

# ---- Chrome -------------------------------------------------------------------------------------------------------
$ChromePaths = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe"))
if (-not ($ChromePaths | Where-Object { Test-Path $_ })) {
    Step "Installing Chrome (per user)"
    $exe = Join-Path $Base "chrome_installer.exe"
    Invoke-WebRequest "https://dl.google.com/chrome/install/latest/chrome_installer.exe" -OutFile $exe
    Start-Process $exe -Wait -ArgumentList "/silent /install"
}
Step "Chrome: $(($ChromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1))"

# ---- Postgres (portable) ------------------------------------------------------------------------------------------
$PgBin = Join-Path $Base "pgsql\bin"
$PgData = Join-Path $Base "pgdata"
if (-not (Test-Path (Join-Path $PgBin "pg_ctl.exe"))) {
    Step "Downloading Postgres 16"
    $zip = Join-Path $Base "pgsql.zip"
    Invoke-WebRequest "https://get.enterprisedb.com/postgresql/postgresql-16.10-1-windows-x64-binaries.zip" -OutFile $zip
    Expand-Archive $zip -DestinationPath $Base -Force
    Remove-Item $zip
}
if (-not (Test-Path (Join-Path $PgData "PG_VERSION"))) {
    Step "Creating the database cluster (user admin, localhost only, port $PgPort)"
    & (Join-Path $PgBin "initdb.exe") -D $PgData -U admin -A trust -E UTF8 --locale=C | Out-Null
    Add-Content (Join-Path $PgData "postgresql.conf") "`nlisten_addresses = 'localhost'`nport = $PgPort`nmax_connections = 60`n"
}
$env:PGPORT = "$PgPort"; $env:PGHOST = "127.0.0.1"; $env:PGUSER = "admin"
& (Join-Path $PgBin "pg_ctl.exe") -D $PgData status | Out-Null
if ($LASTEXITCODE -ne 0) {
    Step "Starting Postgres"
    & (Join-Path $PgBin "pg_ctl.exe") -D $PgData -l (Join-Path $Base "postgres.log") -w start | Out-Null
}
Step "Postgres: $(& (Join-Path $PgBin 'psql.exe') -d postgres -Atc 'select version()')"

# ---- the bundle: procmon + projects + their data ------------------------------------------------------------------
if ($Bundle) {
    Step "Unpacking the bundle"
    $zip = Join-Path $Base "bundle.zip"
    if ($Bundle -match '^https?://') { Invoke-WebRequest $Bundle -OutFile $zip } else { Copy-Item $Bundle $zip -Force }
    $stage = Join-Path $Base "bundle"
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    Expand-Archive $zip -DestinationPath $stage -Force
    Remove-Item $zip
    foreach ($dir in Get-ChildItem (Join-Path $stage "projects") -Directory) {
        $dest = Join-Path $Root $dir.Name
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        # Code is replaced; .env, .venv and data\ already on this machine are kept.
        robocopy $dir.FullName $dest /E /NFL /NDL /NJH /NJS /NP /XD .venv /XF .env | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "copying $($dir.Name) failed ($LASTEXITCODE)" }
    }
    foreach ($sql in Get-ChildItem (Join-Path $stage "db") -Filter *.sql -ErrorAction SilentlyContinue) {
        $schema = $sql.BaseName
        $exists = & (Join-Path $PgBin "psql.exe") -d postgres -Atc "select 1 from pg_namespace where nspname='$schema'"
        if ($exists -eq "1") { Step "Schema $schema already here: left as it is" ; continue }
        Step "Loading schema $schema"
        & (Join-Path $PgBin "psql.exe") -d postgres -v ON_ERROR_STOP=1 -q -f $sql.FullName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "loading $schema failed" }
    }
}

# ---- a venv per Python project ------------------------------------------------------------------------------------
foreach ($req in Get-ChildItem $Root -Filter requirements.txt -Depth 1 -File) {
    $proj = $req.Directory.FullName
    $venvPy = Join-Path $proj ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPy)) {
        Step "Making the venv for $($req.Directory.Name)"
        & $Py -m venv (Join-Path $proj ".venv")
    }
    & $venvPy -m pip install -q --disable-pip-version-check -r $req.FullName
    if ($LASTEXITCODE -ne 0) { throw "pip install failed for $($req.Directory.Name)" }
}

# ---- start at login, and now ---------------------------------------------------------------------------------------
# The Startup folder is per user and needs no administrator; procmon then starts the projects in this user's
# desktop session, so a browser a project opens shows on the desktop.
$start = Join-Path $Base "start-node.ps1"
@"
`$env:PGPORT = '$PgPort'
& '$(Join-Path $PgBin "pg_ctl.exe")' -D '$PgData' status *> `$null
if (`$LASTEXITCODE -ne 0) { & '$(Join-Path $PgBin "pg_ctl.exe")' -D '$PgData' -l '$(Join-Path $Base "postgres.log")' -w start *> `$null }
`$procmonPy = '$(Join-Path $Root "procmon\.venv\Scripts\pythonw.exe")'
`$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { `$_.CommandLine -like '*-m procmon.app*' }
if (-not `$running -and (Test-Path `$procmonPy)) {
    Start-Process `$procmonPy -ArgumentList '-m procmon.app' -WorkingDirectory '$Root' -WindowStyle Hidden ``
        -RedirectStandardOutput '$(Join-Path $Base "procmon.out.log")' -RedirectStandardError '$(Join-Path $Base "procmon.err.log")'
}
"@ | Set-Content -Encoding UTF8 $start
$startup = [Environment]::GetFolderPath("Startup")
$cmd = Join-Path $startup "procmon-node.cmd"
"@powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$start`"" | Set-Content -Encoding ASCII $cmd
Step "Starts at login: $cmd"

# Run it now through Task Scheduler (a one-off task in this user's name, no administrator needed), so procmon is not
# a child of whatever ran this script and keeps running after it ends.
schtasks /Create /F /TN "procmon-node-start" /SC ONCE /ST 00:00 /RL LIMITED `
    /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$start`"" | Out-Null
schtasks /Run /TN "procmon-node-start" | Out-Null
Step "Done"
