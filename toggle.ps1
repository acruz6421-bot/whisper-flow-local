# Liga/desliga a pilha inteira (servidor Whisper + cliente de ditado).
# Chamado pelo start.bat / stop.bat. Sem argumento = alterna.
param([ValidateSet("toggle", "start", "stop")][string]$Mode = "toggle")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = if ($env:WHISPER_PORT) { $env:WHISPER_PORT } else { 9978 }
$url  = "http://127.0.0.1:$port"
$data = Join-Path $root "data"
New-Item -ItemType Directory -Force -Path $data | Out-Null

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Notify([string]$texto, [string]$tipo = "Info") {
    try {
        $ni = New-Object System.Windows.Forms.NotifyIcon
        $ico = Join-Path $root "whisper.ico"
        if (Test-Path $ico) { $ni.Icon = New-Object System.Drawing.Icon($ico) }
        else { $ni.Icon = [System.Drawing.SystemIcons]::Information }
        $ni.Visible = $true
        $ni.BalloonTipTitle = "whisper-flow-local"
        $ni.BalloonTipText = $texto
        $ni.BalloonTipIcon = $tipo
        $ni.ShowBalloonTip(5000)
        Start-Sleep -Seconds 6
        $ni.Visible = $false
        $ni.Dispose()
    } catch { }
}

# Casa pelo caminho completo do script: nao derruba outro server.py qualquer.
function Get-Procs([string]$script) {
    $full = Join-Path $root $script
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$full*" }
}
function Test-Health { try { Invoke-RestMethod -Uri "$url/health" -TimeoutSec 2 } catch { $null } }
function Get-Py {
    $p = Join-Path $root "venv\Scripts\pythonw.exe"
    if (Test-Path $p) { return $p }
    return Join-Path $root "venv\Scripts\python.exe"
}

$running = (Test-Health) -or (@(Get-Procs "server.py").Count -gt 0) -or (@(Get-Procs "dictate.py").Count -gt 0)

if ($Mode -eq "stop" -or ($Mode -eq "toggle" -and $running)) {
    foreach ($p in @(Get-Procs "server.py") + @(Get-Procs "dictate.py")) {
        try { Stop-Process -Id $p.ProcessId -Force } catch { }
    }
    Start-Sleep -Milliseconds 1200
    if ((@(Get-Procs "server.py").Count + @(Get-Procs "dictate.py").Count) -gt 0) {
        Notify "Nao consegui desligar tudo." "Warning"
    } else { Notify "Desligado. VRAM liberada." }
    return
}
if ($Mode -eq "start" -and $running -and (Test-Health)) { Start-Process $url; return }

$py = Get-Py
if (-not (Test-Path $py)) { Notify "venv nao encontrado. Rode install.ps1." "Error"; return }

Start-Process -FilePath $py -ArgumentList "`"$(Join-Path $root 'server.py')`"" `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $data "server.log") `
    -RedirectStandardError  (Join-Path $data "server.err.log")

$up = $null
for ($i = 0; $i -lt 60; $i++) { Start-Sleep -Milliseconds 500; $up = Test-Health; if ($up) { break } }
if (-not $up) { Notify "Servidor nao respondeu em 30s. Veja data\server.err.log." "Error"; return }

Start-Process -FilePath $py -ArgumentList "`"$(Join-Path $root 'dictate.py')`"" `
    -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 2
if (@(Get-Procs "dictate.py").Count -gt 0) {
    Notify "Ligado ($($up.model)). Ctrl+Alt+Space para ditar."
} else { Notify "Servidor ok, mas o ditado nao subiu. Veja data\dictate.log." "Warning" }

# Aquece em segundo plano para o 1o ditado ja ser rapido.
try { Invoke-RestMethod -Uri "$url/warmup" -Method Post -TimeoutSec 300 | Out-Null } catch { }
