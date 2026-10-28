"""
Transcreve arquivos de audio/video pela linha de comando (ou pelo menu
"Enviar para" do Windows, ver install.ps1). Grava .txt e .srt ao lado do
arquivo. O servidor precisa estar rodando.

    python transcribe_files.py aula.mp4 podcast.mp3
"""
import json
import os
import sys
import urllib.request

SERVER = os.environ.get("WFL_SERVER", "http://127.0.0.1:9978").rstrip("/")


def main(paths):
    if not paths:
        print(__doc__)
        return 1
    try:
        urllib.request.urlopen(SERVER + "/health", timeout=4).close()
    except Exception:
        print("servidor fora do ar em %s. Rode start.bat primeiro." % SERVER)
        return 2
    rc = 0
    for p in paths:
        full = os.path.abspath(p)
        if not os.path.isfile(full):
            print("nao existe:", p)
            rc = 1
            continue
        body = json.dumps({"path": full}).encode("utf-8")
        req = urllib.request.Request(SERVER + "/api/transcribe/path", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=7200) as r:
                res = json.loads(r.read().decode("utf-8"))
            print("%s: %.1fs de audio em %.1fs (%.1fx) -> %s" % (
                os.path.basename(full), res["duration"], res["elapsed"], res["speed"],
                ", ".join(os.path.basename(w) for w in res["written"])))
        except Exception as exc:
            print("falhou %s: %s" % (os.path.basename(full), exc))
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
