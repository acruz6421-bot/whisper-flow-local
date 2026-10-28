"""
Servidor de transcricao local + dashboard.

Whisper via faster-whisper (CTranslate2) na GPU. Nao usa PyTorch.
Escuta apenas em 127.0.0.1 - nada e exposto para fora da maquina.

  GET  /                            dashboard
  GET  /health                      estado curto
  POST /v1/audio/transcriptions     compativel com a API da OpenAI
  POST /warmup
  GET/POST /api/config
  GET  /api/status  /api/devices  /api/history
  POST /api/history/clear  /api/warmup  /api/client/heartbeat

Uso:  venv\\Scripts\\python.exe server.py
"""

import os
import sys
import io
import gc
import json
import time
import threading
import subprocess
import logging

# ---------------------------------------------------------------------------
# DLLs de CUDA dos wheels nvidia-*. os.add_dll_directory NAO resolve sozinho: o
# CTranslate2 chama LoadLibrary("cublas64_12.dll") direto, e essa busca passa
# pelo PATH. Precisa vir ANTES de importar ctranslate2 / faster_whisper.
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    import site
    _bases = list(site.getsitepackages())
    _bases.append(os.path.join(sys.prefix, "Lib", "site-packages"))
    _dirs = []
    for _b in _bases:
        for _p in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            _d = os.path.join(_b, "nvidia", _p, "bin")
            if os.path.isdir(_d) and _d not in _dirs:
                _dirs.append(_d)
        _c = os.path.join(_b, "ctranslate2")
        if os.path.isdir(_c) and _c not in _dirs:
            _dirs.append(_c)
    if _dirs:
        os.environ["PATH"] = os.pathsep.join(_dirs) + os.pathsep + os.environ.get("PATH", "")
        for _d in _dirs:
            try:
                os.add_dll_directory(_d)
            except OSError:
                pass

from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
import uvicorn

from config import CFG

HERE = os.path.dirname(os.path.abspath(__file__))
from config import DATA
HISTORY = os.path.join(DATA, "history.json")
DASH = os.path.join(HERE, "dashboard.html")

HOST = os.environ.get("WHISPER_HOST", "127.0.0.1")
PORT = int(os.environ.get("WHISPER_PORT", "9978"))
DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE", "float16")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("whisper-server")

app = FastAPI(title="whisper-flow-local", docs_url=None, redoc_url=None)
# CORS extra (ex.: uma extensao de navegador que chama /v1/audio/transcriptions):
# WFL_CORS_ORIGINS="https://site1,https://site2". Vazio = so o proprio dashboard.
_origins = [o.strip() for o in os.environ.get("WFL_CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins,
                       allow_methods=["*"], allow_headers=["*"])


def cfg_model():
    return os.environ.get("WHISPER_MODEL") or CFG.get("transcription.model", "large-v3-turbo")


def cfg_hotwords():
    """Dicionario pessoal (transcription.vocabulary) vira hotwords do Whisper:
    nomes proprios e termos tecnicos param de sair com grafia errada."""
    voc = CFG.get("transcription.vocabulary", []) or []
    voc = [str(v).strip() for v in voc if str(v).strip()]
    return ", ".join(voc) or None


def cfg_idle():
    v = os.environ.get("WHISPER_IDLE_UNLOAD")
    if v is not None:
        return int(v)
    return int(CFG.get("transcription.idle_unload_s", 3600) or 0)


# ---------------------------------------------------------------------------
# Modelo. Serializado no _gpu_lock: duas transcricoes ao mesmo tempo brigam por
# VRAM e ficam as duas lentas; em fila a latencia fica previsivel.
# ---------------------------------------------------------------------------
_model = None
_model_name = None
_model_lock = threading.Lock()
_gpu_lock = threading.Lock()
_last_used = 0.0
_client = {"at": 0.0, "mic": "", "hotkeys": ""}


def get_model():
    global _model, _model_name, _last_used
    # Rele o config.json se mudou (so um stat): sem isso, mudanca feita por
    # script ou por outro processo so valia depois de reiniciar o servidor.
    try:
        CFG.reload_if_changed()
    except Exception:
        pass
    want = cfg_model()
    with _model_lock:
        # Trocar de modelo no dashboard tem efeito aqui, sem reiniciar processo.
        if _model is not None and _model_name != want:
            log.info("modelo mudou (%s -> %s), descarregando", _model_name, want)
            _model = None
            gc.collect()
        if _model is None:
            from faster_whisper import WhisperModel
            log.info("Carregando %s (%s, %s)...", want, DEVICE, COMPUTE_TYPE)
            t0 = time.time()
            # Pasta models/<nome> ao lado do servidor tem prioridade sobre o cache do HF.
            _mdir = os.environ.get("WFL_MODELS_DIR") or os.path.join(HERE, "models")
            _local = os.path.join(_mdir, want)
            _src = _local if os.path.isfile(os.path.join(_local, "model.bin")) else want
            try:
                # local_files_only: sem isso cada carga consulta o HuggingFace
                # online e a partida a frio ia de ~4s para ~12s. So vai pra
                # rede se o modelo ainda nao estiver no cache.
                try:
                    _model = WhisperModel(_src, device=DEVICE, compute_type=COMPUTE_TYPE,
                                          local_files_only=True)
                except Exception as exc_local:
                    if "cuda" in str(exc_local).lower() or "cublas" in str(exc_local).lower():
                        raise
                    log.info("modelo fora do cache local, baixando: %s", exc_local)
                    _model = WhisperModel(want, device=DEVICE, compute_type=COMPUTE_TYPE)
            except Exception as exc:
                log.error("Falha na GPU (%s). Caindo para CPU int8.", exc)
                _model = WhisperModel(want, device="cpu", compute_type="int8")
            _model_name = want
            log.info("Modelo pronto em %.1fs", time.time() - t0)
            # Aquecimento real: a 1a transcricao depois de carregar paga
            # compilacao/alocacao de kernels CUDA (~1s a mais). Roda um
            # segundo de audio sintetico agora, para o ditado nao pagar isso.
            try:
                t1 = time.time()
                # Audio de fala REAL e os mesmos parametros do ditado: com
                # silencio o VAD descarta tudo e o encoder/beam search nao
                # rodam, e a 1a transcricao continuava pagando ~1,2s a mais.
                ref = os.path.join(HERE, "assets", "warmup.wav")
                src = None
                if os.path.exists(ref):
                    from faster_whisper import decode_audio
                    # 6s de fala: o bastante para aquecer encoder + beam search,
                    # curto o bastante para nao atrasar quem acabou de apertar
                    # a tecla com o modelo frio.
                    src = decode_audio(ref)[:16000 * 6]
                use_vad = src is not None
                if src is None:
                    # Sem amostra de fala: ruido com VAD desligado, para o
                    # encoder e o decoder rodarem de verdade.
                    import numpy as _np
                    src = (_np.random.default_rng(0).standard_normal(16000 * 3) * 0.05).astype(_np.float32)
                # 1 passada de 6s basta para aquecer.
                for _ in range(int(os.environ.get("WHISPER_WARM_PASSES", "1"))):
                    _segs, _ = _model.transcribe(
                        src, language=None if (CFG.get("transcription.language") or "pt") == "auto" else (CFG.get("transcription.language") or "pt"), beam_size=int(CFG.get("transcription.beam_size", 5)),
                        vad_filter=use_vad, vad_parameters={"min_silence_duration_ms": 500},
                        condition_on_previous_text=False,
                        initial_prompt=CFG.get("transcription.initial_prompt") or None,
                        hotwords=cfg_hotwords())
                    list(_segs)
                log.info("GPU aquecida em %.2fs", time.time() - t1)
            except Exception as exc:
                log.warning("aquecimento da GPU falhou: %s", exc)
            # O relogio de ociosidade comeca no carregamento, nao na primeira
            # transcricao: senao um /warmup sem uso deixaria o modelo preso na
            # VRAM para sempre, porque o vigia pula quando _last_used e zero.
            _last_used = time.time()
        return _model


def _idle_watcher():
    """
    Ordem dos locks: aqui pegamos _gpu_lock e depois _model_lock. O caminho de
    transcricao solta _model_lock (dentro de get_model) ANTES de pegar _gpu_lock,
    entao nao existe ciclo. Quem esta transcrevendo tem referencia local ao
    modelo, que o mantem vivo mesmo limpando a global.
    """
    global _model
    while True:
        time.sleep(30)
        limit = cfg_idle()
        if limit <= 0 or _model is None or _last_used <= 0:
            continue
        idle = time.time() - _last_used
        if idle < limit:
            continue
        with _gpu_lock:
            with _model_lock:
                if _model is not None:
                    log.info("Ocioso por %.0fs: descarregando o modelo da VRAM.", idle)
                    _model = None
                    gc.collect()


threading.Thread(target=_idle_watcher, name="idle-watcher", daemon=True).start()


def _ts(sec, vtt=False):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:  # arredondamento para cima nao pode virar ':60.000'
        s, ms = s + 1, 0
    sep = "." if vtt else ","
    return "%02d:%02d:%02d%s%03d" % (h, m, s, sep, ms)


def to_srt(segs):
    out = []
    for i, s in enumerate(segs, 1):
        out.append("%d\n%s --> %s\n%s\n" % (i, _ts(s["start"]), _ts(s["end"]), s["text"]))
    return "\n".join(out)


def to_vtt(segs):
    out = ["WEBVTT", ""]
    for s in segs:
        out.append("%s --> %s" % (_ts(s["start"], True), _ts(s["end"], True)))
        out.append(s["text"])
        out.append("")
    return "\n".join(out)


def vram():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4)
        used, total = out.stdout.strip().split("\n")[0].split(",")
        return int(used), int(total)
    except Exception:
        return 0, 0


# ---------------------------------------------------------------------------
# Transcricao
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    idle = (time.time() - _last_used) if _last_used > 0 else None
    return {"status": "ok", "model": cfg_model(), "device": DEVICE,
            "compute_type": COMPUTE_TYPE, "loaded": _model is not None,
            "idle_seconds": round(idle, 1) if idle is not None else None,
            "idle_unload_after": cfg_idle() or None}


@app.post("/warmup")
@app.post("/api/warmup")
def warmup():
    get_model()
    return {"status": "ok", "loaded": True}


@app.post("/api/unload")
def unload():
    """Tira o modelo da VRAM na hora (teste de partida a frio / liberar GPU)."""
    global _model
    with _gpu_lock:
        with _model_lock:
            had = _model is not None
            _model = None
            gc.collect()
    log.info("modelo descarregado via /api/unload")
    return {"status": "ok", "was_loaded": had}


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), language: str = Form(None),
                     prompt: str = Form(None), response_format: str = Form("json"),
                     task: str = Form("transcribe")):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="arquivo de audio vazio")

    wtask = task if task in ("transcribe", "translate") else "transcribe"
    lang = language or CFG.get("transcription.language", "pt")
    if lang == "auto":
        lang = None

    model = get_model()
    t0 = time.time()
    with _gpu_lock:
        try:
            segments, info = model.transcribe(
                io.BytesIO(raw), language=lang, task=wtask,
                beam_size=int(CFG.get("transcription.beam_size", 5)),
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
                # Sem isto o Whisper entra em loop de repeticao quando o audio
                # tem ruido ou silencio longo, que e o defeito mais visivel em
                # nota de voz. Custa um pouco de coerencia entre frases.
                condition_on_previous_text=False,
                initial_prompt=prompt or CFG.get("transcription.initial_prompt") or None,
                hotwords=cfg_hotwords(),
            )
            segs = [{"start": round(s.start, 2), "end": round(s.end, 2),
                     "text": s.text.strip()} for s in segments]
        except Exception as exc:
            log.exception("Erro ao transcrever %s", file.filename)
            raise HTTPException(status_code=500, detail="falha na transcricao: %s" % exc)

    global _last_used
    _last_used = time.time()

    text = " ".join(s["text"] for s in segs).strip()
    elapsed = time.time() - t0
    duration = getattr(info, "duration", 0.0) or 0.0
    speed = (duration / elapsed) if elapsed > 0 else 0.0
    log.info("%s | %.1fs de audio em %.1fs (%.1fx) | %d chars",
             file.filename or "audio", duration, elapsed, speed, len(text))

    # Mesmos formatos que a API da OpenAI define, para qualquer cliente que ja
    # fale esse protocolo funcionar sem adaptacao.
    if response_format == "text":
        return PlainTextResponse(text)
    if response_format == "srt":
        return PlainTextResponse(to_srt(segs))
    if response_format == "vtt":
        return PlainTextResponse(to_vtt(segs))
    return {"text": text, "language": getattr(info, "language", lang),
            "duration": round(duration, 2), "elapsed": round(elapsed, 2),
            "speed": round(speed, 1), "segments": segs}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Transcricao de arquivo (local) e pasta vigiada
# ---------------------------------------------------------------------------
MEDIA_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma",
             ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".mpg", ".mpeg"}


def run_on_path(path, formats=None, language=None):
    """
    Transcreve um arquivo do disco e grava os resultados ao lado dele.

    Recebe o CAMINHO, e nao os bytes: o PyAV que o faster-whisper usa abre o
    arquivo direto, inclusive container de video, e assim um mkv de 2 GB nao
    precisa trafegar por HTTP dentro da propria maquina.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    fmts = formats or CFG.get("files.formats", ["txt", "srt"])
    lang = language or CFG.get("transcription.language", "pt")
    if lang == "auto":
        lang = None

    model = get_model()
    t0 = time.time()
    with _gpu_lock:
        segments, info = model.transcribe(
            path, language=lang,
            beam_size=int(CFG.get("transcription.beam_size", 5)),
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            initial_prompt=CFG.get("transcription.initial_prompt") or None,
            hotwords=cfg_hotwords())
        segs = [{"start": round(s.start, 2), "end": round(s.end, 2),
                 "text": s.text.strip()} for s in segments]

    global _last_used
    _last_used = time.time()

    text = " ".join(s["text"] for s in segs).strip()
    base = os.path.splitext(path)[0]
    escritos = []
    conteudo = {"txt": text, "srt": to_srt(segs), "vtt": to_vtt(segs)}
    for f in fmts:
        f = str(f).lower().lstrip(".")
        if f not in conteudo:
            continue
        dest = base + "." + f
        try:
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(conteudo[f])
            escritos.append(dest)
        except Exception as exc:
            log.warning("nao consegui gravar %s: %s", dest, exc)

    dur = getattr(info, "duration", 0.0) or 0.0
    el = time.time() - t0
    log.info("arquivo %s | %.1fs em %.1fs (%.1fx) -> %s",
             os.path.basename(path), dur, el, (dur / el) if el else 0,
             ", ".join(os.path.basename(x) for x in escritos))
    return {"text": text, "segments": segs, "written": escritos,
            "duration": round(dur, 2), "elapsed": round(el, 2),
            "speed": round((dur / el) if el else 0, 1)}


@app.post("/api/transcribe/path")
async def api_transcribe_path(req: Request):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON invalido")
    path = (body or {}).get("path")
    if not path:
        raise HTTPException(status_code=400, detail="informe 'path'")
    try:
        return run_on_path(path, body.get("formats"), body.get("language"))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="arquivo nao encontrado: %s" % path)
    except Exception as exc:
        log.exception("falha ao transcrever %s", path)
        raise HTTPException(status_code=500, detail=str(exc))


def _watch_folder():
    """
    Vigia a pasta configurada. Espera o tamanho parar de crescer antes de tocar
    no arquivo: copiar um video grande para a pasta leva tempo, e transcrever
    pela metade daria resultado truncado sem erro nenhum.
    """
    vistos = {}
    while True:
        time.sleep(4)
        folder = (CFG.get("files.watch_folder") or "").strip()
        if not folder or not os.path.isdir(folder):
            continue
        try:
            nomes = os.listdir(folder)
        except Exception:
            continue
        for nome in nomes:
            p = os.path.join(folder, nome)
            if not os.path.isfile(p):
                continue
            if os.path.splitext(nome)[1].lower() not in MEDIA_EXT:
                continue
            try:
                tam = os.path.getsize(p)
            except Exception:
                continue
            antes = vistos.get(p)
            vistos[p] = tam
            if antes is None or antes != tam:
                continue  # ainda copiando; espera estabilizar
            base = os.path.splitext(p)[0]
            if os.path.exists(base + ".txt") or os.path.exists(base + ".srt"):
                continue  # ja transcrito
            try:
                log.info("pasta vigiada: transcrevendo %s", nome)
                run_on_path(p)
                if CFG.get("files.delete_after"):
                    os.remove(p)
            except Exception:
                log.exception("pasta vigiada falhou em %s", nome)
            finally:
                vistos.pop(p, None)


threading.Thread(target=_watch_folder, name="watch-folder", daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    try:
        with open(DASH, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except Exception:
        return HTMLResponse("<h1>dashboard.html nao encontrado</h1>", status_code=404)


@app.get("/api/config")
def api_config_get():
    CFG.reload_if_changed()
    return CFG.data


@app.post("/api/config")
async def api_config_post(req: Request):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON invalido")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="esperava um objeto")
    return CFG.save(body)


@app.post("/api/client/heartbeat")
async def api_heartbeat(req: Request):
    """O cliente de ditado se anuncia: e assim que o painel sabe que esta vivo."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    _client.update({"at": time.time(), "mic": body.get("mic", ""),
                    "hotkeys": body.get("hotkeys", "")})
    return {"ok": True}


@app.get("/api/status")
def api_status():
    used, total = vram()
    idle = (time.time() - _last_used) if _last_used > 0 else None
    # Heartbeat com mais de 15s e considerado morto: o cliente manda a cada 5s.
    alive = (time.time() - _client["at"]) < 15 if _client["at"] else False
    return {"server_ok": True, "model": _model_name or cfg_model(), "device": DEVICE,
            "compute_type": COMPUTE_TYPE, "loaded": _model is not None,
            "idle_seconds": idle, "idle_unload_after": cfg_idle() or None,
            "vram_used_mb": used, "vram_total_mb": total,
            "dictation_running": alive, "mic": _client["mic"],
            "hotkeys": _client["hotkeys"]}


@app.get("/api/devices")
def api_devices():
    try:
        import sounddevice as sd
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) < 1:
                continue
            try:
                api = sd.query_hostapis(d["hostapi"])["name"]
            except Exception:
                continue
            if api not in ("Windows WASAPI", "Windows DirectSound", "MME"):
                continue
            out.append({"index": i, "name": d["name"], "api": api,
                        "rate": int(d.get("default_samplerate") or 0)})
        return {"devices": out}
    except Exception as exc:
        return {"devices": [], "error": str(exc)}


def _read_history():
    try:
        with open(HISTORY, "r", encoding="utf-8") as f:
            items = json.load(f)
        return items if isinstance(items, list) else []
    except Exception:
        return []


@app.get("/api/history")
def api_history():
    items = _read_history()
    words = sum(len((x.get("text") or "").split()) for x in items)
    audio = sum(float(x.get("duration") or 0) for x in items)
    elapsed = sum(float(x.get("elapsed") or 0) for x in items)
    return {"items": items[-200:],
            "stats": {"count": len(items), "words": words, "audio_s": audio,
                      "elapsed_s": elapsed,
                      "speed": (audio / elapsed) if elapsed > 0 else 0}}


@app.post("/api/history/clear")
def api_history_clear():
    try:
        tmp = HISTORY + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([], f)
        os.replace(tmp, HISTORY)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/llm/test")
async def api_llm_test(req: Request):
    """Testa o provedor (e o fallback) com a config enviada pelo painel, sem salvar."""
    import llm
    try:
        body = await req.json()
    except Exception:
        body = None
    cfg = (body or {}).get("llm") if isinstance(body, dict) else None
    res = llm.test(cfg or CFG.get("llm", {}))
    log.info("teste de IA pelo painel: %s", ", ".join(
        "%s/%s=%s" % (r["provider"], r.get("model"), "ok" if r["ok"] else "falhou") for r in res) or "desligado")
    return {"results": res}


@app.post("/api/llm/models")
async def api_llm_models(req: Request):
    """Lista os modelos que o provedor informa (Ollama /api/tags ou /v1/models)."""
    import llm
    try:
        body = await req.json()
    except Exception:
        body = {}
    kind = (body or {}).get("provider") or "ollama"
    prov = (body or {}).get("config") or {}
    try:
        return {"models": llm.list_models(kind, prov)}
    except Exception as exc:
        return {"models": [], "error": str(exc)[:200]}


if __name__ == "__main__":
    CFG.ensure_file()
    log.info("Whisper + dashboard em http://%s:%d  (modelo %s)", HOST, PORT, cfg_model())
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
