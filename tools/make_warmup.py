"""
Gera assets/warmup.wav: alguns segundos de fala sintetica usados para aquecer
a GPU logo depois de carregar o Whisper (a 1a transcricao paga ~1 s a mais de
compilacao de kernels; com esse aquecimento o 1o ditado ja sai rapido).

Usa a voz offline do proprio Windows (SAPI, sem rede). E opcional: sem o
arquivo, o servidor aquece com ruido, o que funciona quase tao bem.

    python tools/make_warmup.py
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(os.path.join(HERE, "..", "assets", "warmup.wav"))
TEXT = ("Olá, este é um teste de aquecimento do ditado por voz. "
        "Ele roda uma vez quando o modelo carrega, para a primeira transcrição sair rápida.")

PS = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'pt*' } | Select-Object -First 1
if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($env:WFL_OUT, $fmt)
$s.Speak($env:WFL_TEXT)
$s.Dispose()
Write-Output ($(if ($v) { $v.VoiceInfo.Name } else { 'voz padrao' }))
"""


def main():
    if sys.platform != "win32":
        sys.exit("Este gerador usa a voz do Windows. Em outro sistema, grave 5-10 s "
                 "de fala sua em assets/warmup.wav (16 kHz mono).")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    script = os.path.join(tempfile.gettempdir(), "wfl_warmup.ps1")
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(PS)
    env = dict(os.environ, WFL_OUT=OUT, WFL_TEXT=TEXT)
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", script], env=env, capture_output=True, text=True)
    os.remove(script)
    if r.returncode != 0 or not os.path.exists(OUT):
        sys.exit("falhou: " + (r.stderr or r.stdout)[:300])
    print("ok: %s (%s)" % (OUT, r.stdout.strip()))


if __name__ == "__main__":
    main()
