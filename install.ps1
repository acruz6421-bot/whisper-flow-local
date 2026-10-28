# whisper-flow-local - instalador para Windows 10/11.
# Cria o venv, instala as dependencias e (opcional) baixa o modelo.
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Cpu      # sem placa NVIDIA

param([switch]$Cpu)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) { $base = @("py", "-3") } else { $base = @("python") }
& $base[0] $base[1..9] --version
if ($LASTEXITCODE -ne 0) { throw "Python 3.10+ nao encontrado. Instale em python.org (marque 'Add to PATH')." }

if (-not (Test-Path "$root\venv\Scripts\python.exe")) {
    Write-Host "Criando venv..." -ForegroundColor Cyan
    & $base[0] $base[1..9] -m venv "$root\venv"
}
$vpy = "$root\venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip | Out-Null

$req = Get-Content "$root\requirements.txt"
if ($Cpu) { $req = $req | Where-Object { $_ -notmatch '^nvidia-' } }
$tmp = Join-Path $env:TEMP "wfl-req.txt"
$req | Set-Content $tmp -Encoding ascii
Write-Host "Instalando dependencias..." -ForegroundColor Cyan
& $vpy -m pip install -r $tmp
if ($LASTEXITCODE -ne 0) { throw "pip falhou" }

if (-not (Test-Path "$root\assets\warmup.wav")) {
    & $vpy "$root\tools\make_warmup.py"
}

Write-Host "Baixando o modelo large-v3-turbo (~1,6 GB, so na primeira vez)..." -ForegroundColor Cyan
& $vpy -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8'); print('modelo ok')"

if ($Cpu) {
    [Environment]::SetEnvironmentVariable("WHISPER_DEVICE", "cpu", "User")
    [Environment]::SetEnvironmentVariable("WHISPER_COMPUTE", "int8", "User")
    Write-Host "Modo CPU gravado nas variaveis do usuario (WHISPER_DEVICE=cpu)." -ForegroundColor Yellow
}

# Atalho "Enviar para > Transcrever com Whisper"
$sendto = [Environment]::GetFolderPath("SendTo")
$sh = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut("$sendto\Transcrever com Whisper.lnk")
$lnk.TargetPath = "$root\venv\Scripts\python.exe"
$lnk.Arguments = "`"$root\transcribe_files.py`""
$lnk.WorkingDirectory = $root
$lnk.IconLocation = "$root\whisper.ico"
$lnk.Save()

Write-Host ""
Write-Host "Pronto. Rode start.bat e abra http://127.0.0.1:9978 para configurar a IA de texto." -ForegroundColor Green
