# GoldBot installer for Windows (PowerShell 5.1 or newer).
#
# Run it in an ADMINISTRATOR PowerShell. It is safe to run again later: that
# updates the program and keeps your config.json, secrets.env, data and logs.
#
# What it does:
#   1. downloads the bot into C:\goldbot
#   2. installs Python 3.11 (signature-checked) and the bot's libraries
#   3. helps you install MetaTrader 5 (you log in to your broker yourself)
#   4. runs the setup wizard (keys, Telegram, dashboard token)
#   5. stops Windows from sleeping and makes the bot start at every logon
#   6. runs the doctor (a full health check) and starts the bot
#
# Optional overrides (environment variables): GOLDBOT_DIR, GOLDBOT_REPO, GOLDBOT_BRANCH

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # makes downloads much faster on PowerShell 5.1
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$InstallDir = if ($env:GOLDBOT_DIR) { $env:GOLDBOT_DIR } else { "C:\goldbot" }
$Repo = if ($env:GOLDBOT_REPO) { $env:GOLDBOT_REPO } else { "hussein-zreik/fxbot" }
$Branch = if ($env:GOLDBOT_BRANCH) { $env:GOLDBOT_BRANCH } else { "claude/mt5-gold-trading-system-dhlftm" }
$PythonVersion = "3.11.9"
$PythonDir = "C:\Python311"
$TaskName = "GoldBot"
$Venv = Join-Path $InstallDir ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "   $text" }
function Warn($text) { Write-Host "   WARNING: $text" -ForegroundColor Yellow }
function Die($text) { Write-Host ""; Write-Host "STOPPED: $text" -ForegroundColor Red; exit 1 }
function Ask-YesNo($question, $defaultYes) {
    $suffix = if ($defaultYes) { "[Y/n]" } else { "[y/N]" }
    $answer = (Read-Host "   $question $suffix").Trim().ToLower()
    if ($answer -eq "") { return $defaultYes }
    return $answer.StartsWith("y")
}
function Secure-ToPlain($secure) {
    return (New-Object System.Net.NetworkCredential("", $secure)).Password
}
function Check-Exit($what) {
    if ($LASTEXITCODE -ne 0) { Die "$what failed (exit code $LASTEXITCODE). Scroll up for the reason." }
}

# ---------------------------------------------------------------- checks
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Die "Please run this in an ADMINISTRATOR PowerShell (right-click PowerShell, 'Run as administrator')."
}
if (-not [Environment]::Is64BitOperatingSystem) { Die "A 64-bit Windows is required." }

Write-Host ""
Write-Host "GoldBot installer" -ForegroundColor Green
Write-Host "Install folder: $InstallDir"

# ---------------------------------------------------------------- 1. code
Step "1/6  Downloading the bot"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue   # updating: stop the old copy
$zip = Join-Path $env:TEMP "goldbot.zip"
$extract = Join-Path $env:TEMP "goldbot-src"
if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }
$publicUrl = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
try {
    Invoke-WebRequest -Uri $publicUrl -OutFile $zip -UseBasicParsing
} catch {
    Info "The code is not public, so GitHub needs a read-only access token."
    Info "Create one at github.com > Settings > Developer settings > Fine-grained tokens"
    Info "(only this repository, permission 'Contents: Read-only')."
    $secure = Read-Host "   Paste the token (hidden)" -AsSecureString
    $token = Secure-ToPlain $secure
    $headers = @{ Authorization = "Bearer $token"; "User-Agent" = "goldbot-installer" }
    try {
        Invoke-WebRequest -Uri "https://api.github.com/repos/$Repo/zipball/$Branch" `
            -Headers $headers -OutFile $zip -UseBasicParsing
    } catch {
        Die "Download failed. Check the token and that branch '$Branch' exists."
    }
}
Expand-Archive -Path $zip -DestinationPath $extract -Force
$source = (Get-ChildItem $extract -Directory | Select-Object -First 1).FullName
if (-not (Test-Path (Join-Path $source "run_bot.py"))) { Die "The download did not contain the bot." }
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
# Copy everything except your own files and folders.
robocopy $source $InstallDir /E /XD .git .venv data logs tests docs __pycache__ /XF config.json secrets.env | Out-Null
if ($LASTEXITCODE -ge 8) { Die "Copying files failed (robocopy code $LASTEXITCODE)." }
Info "Bot files are in $InstallDir"

# ---------------------------------------------------------------- 2. python
Step "2/6  Python and libraries"
$python = Join-Path $PythonDir "python.exe"
if (-not (Test-Path $python)) {
    $installer = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
    Info "Downloading Python $PythonVersion ..."
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" `
        -OutFile $installer -UseBasicParsing
    $sig = Get-AuthenticodeSignature $installer
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notlike "*Python Software Foundation*") {
        Die "The Python installer's signature is not valid, so it was NOT run."
    }
    Info "Signature OK (Python Software Foundation). Installing ..."
    Start-Process -FilePath $installer -Wait `
        -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0 TargetDir=$PythonDir"
    if (-not (Test-Path $python)) { Die "Python did not install." }
} else {
    Info "Python already installed."
}
if (-not (Test-Path $VenvPython)) {
    & $python -m venv $Venv
    Check-Exit "Creating the Python environment"
}
Info "Installing libraries (takes a few minutes the first time) ..."
& $VenvPython -m pip install --quiet --upgrade pip
Check-Exit "Upgrading pip"
& $VenvPython -m pip install --quiet -r (Join-Path $InstallDir "requirements.txt")
Check-Exit "Installing libraries"

# ---------------------------------------------------------------- 3. MetaTrader 5
Step "3/6  MetaTrader 5"
function Find-Mt5 {
    $roots = @("C:\Program Files", "C:\Program Files (x86)") | Where-Object { Test-Path $_ }
    return Get-ChildItem -Path $roots -Filter "terminal64.exe" -Recurse -Depth 1 -ErrorAction SilentlyContinue |
        Select-Object -First 1
}
$mt5 = Find-Mt5
if ($mt5) {
    Info "Found MetaTrader 5: $($mt5.FullName)"
} else {
    Info "MetaTrader 5 is not installed. Install YOUR BROKER'S version (their website,"
    Info "'Download MetaTrader 5 for Windows'), so their servers are preconfigured."
    $url = (Read-Host "   Paste the broker's MT5 download link, or press Enter to install it yourself").Trim()
    if ($url) {
        $mt5Setup = Join-Path $env:TEMP "mt5setup.exe"
        Invoke-WebRequest -Uri $url -OutFile $mt5Setup -UseBasicParsing
        $sig = Get-AuthenticodeSignature $mt5Setup
        Info "Installer signed by: $($sig.SignerCertificate.Subject)"
        if (Ask-YesNo "Run this installer?" $true) {
            Info "Follow the installer windows, then come back here."
            Start-Process -FilePath $mt5Setup -Wait
        }
    } else {
        Read-Host "   Install it now, then press Enter here"
    }
    $mt5 = Find-Mt5
    if (-not $mt5) { Warn "MT5 still not found. You can set mt5.terminal_path in config.json later." }
}
Info ""
Info "In MetaTrader 5, do these 3 things (the installer cannot do them for you):"
Info "  1. File > Login to Trade Account: log in to your DEMO account, tick 'Save password'"
Info "  2. Click the 'Algo Trading' button in the toolbar until it turns GREEN"
Info "  3. Tools > Options > Expert Advisors: tick 'Allow algorithmic trading'"
Read-Host "   Press Enter when that is done"

# ---------------------------------------------------------------- 4. wizard
Step "4/6  Setup wizard (keys, Telegram, dashboard token)"
Push-Location $InstallDir
try {
    & $VenvPython -m goldbot.setup_wizard --dir $InstallDir
    Check-Exit "The setup wizard"
} finally {
    Pop-Location
}
$secretsFile = Join-Path $InstallDir "secrets.env"
if (Test-Path $secretsFile) {
    # Only your own Windows account may read the keys.
    icacls $secretsFile /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(F)" | Out-Null
    Info "secrets.env is now readable only by $($env:USERNAME)."
}

# ---------------------------------------------------------------- 5. always on
Step "5/6  Keeping it running"
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 0
Info "Windows will no longer sleep."

$user = "$($env:USERDOMAIN)\$($env:USERNAME)"
$action = New-ScheduledTaskAction -Execute (Join-Path $InstallDir "start_bot.bat") -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $taskPrincipal -Force | Out-Null
Info "The bot will start automatically every time $($env:USERNAME) logs on."

Info ""
Info "If Windows ever restarts (for example after an update), nobody is logged on, so"
Info "the bot would wait. 'Auto-logon' fixes that, but stores this account's password"
Info "on the server (Microsoft's own Sysinternals tool, using Windows' protected storage)."
if (Ask-YesNo "Set up auto-logon?" $false) {
    try {
        $zipAuto = Join-Path $env:TEMP "AutoLogon.zip"
        $dirAuto = Join-Path $env:TEMP "AutoLogon"
        Invoke-WebRequest -Uri "https://download.sysinternals.com/files/AutoLogon.zip" -OutFile $zipAuto -UseBasicParsing
        Expand-Archive -Path $zipAuto -DestinationPath $dirAuto -Force
        $exe = Join-Path $dirAuto "Autologon64.exe"
        $sig = Get-AuthenticodeSignature $exe
        if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notlike "*Microsoft*") {
            throw "signature check failed"
        }
        $pw = Secure-ToPlain (Read-Host "   Windows password of $($env:USERNAME) (hidden)" -AsSecureString)
        & $exe /accepteula $env:USERNAME $env:USERDOMAIN $pw | Out-Null
        Info "Auto-logon configured."
    } catch {
        Warn "Auto-logon could not be set up automatically ($($_.Exception.Message))."
        Warn "You can do it later with Sysinternals Autologon (see the README)."
    }
}

# ---------------------------------------------------------------- 6. doctor + start
Step "6/6  Health check and start"
Push-Location $InstallDir
$doctorOk = $true
try {
    & $VenvPython -m goldbot.doctor --config config.json --secrets secrets.env
    if ($LASTEXITCODE -ne 0) { $doctorOk = $false }
} finally {
    Pop-Location
}
if (-not $doctorOk) {
    Warn "The health check found problems (see [FAIL] lines above)."
    if (-not (Ask-YesNo "Start the bot anyway?" $false)) {
        Info "Fix the problems, then run:  $VenvPython -m goldbot.doctor"
        Info "and start the bot with:      Start-ScheduledTask -TaskName $TaskName"
        exit 1
    }
}
Start-ScheduledTask -TaskName $TaskName
Info "Bot started (a console window titled GoldBot opens on this desktop)."

$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if ($ts) {
    & tailscale serve --bg 8765
    Info "Dashboard published on your private Tailscale network (see the address above)."
} else {
    Info ""
    Info "Last step for the phone dashboard: install Tailscale (tailscale.com/download),"
    Info "sign in, then run:   tailscale serve --bg 8765"
}

Write-Host ""
Write-Host "DONE." -ForegroundColor Green
Write-Host "  - The bot is in DRY-RUN mode: it logs what it would do, without trading."
Write-Host "  - To trade on the DEMO account, set execution.dry_run to false in $InstallDir\config.json"
Write-Host "  - Logs: $InstallDir\logs\goldbot.log      Stop:  Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  - Update later: run this same installer again (your settings are kept)."
Write-Host "  - You can now close this window and the remote desktop. The bot keeps running."
