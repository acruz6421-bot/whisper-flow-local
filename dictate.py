"""
whisper-flow-local: ditado global por voz, 100% na sua maquina.

Atalhos (todos configuraveis no dashboard, http://127.0.0.1:9978):
  Ctrl+Alt+Space         ditar e colar no cursor
  Ctrl+Alt+Shift+Space   ditar e so copiar (saida para janela elevada)
  Ctrl+Alt+E             ditar forcando ingles
  Ctrl+Alt+Z             desfazer o que acabou de ser colado
  Ctrl+Alt+R             colar de novo o ultimo texto
  Ctrl+Alt+L             reformatar o ultimo texto por LLM (desligado por padrao)
  Esc                    cancelar a gravacao em andamento

Hold e toggle sao detectados automaticamente: segurou mais que tap_ms e
push-to-talk; toque rapido comeca a gravar e o proximo toque encerra. Isso existe
porque macro de mouse manda a combinacao como toque instantaneo.

Desenho, e o motivo:

  O hook de teclado do Windows tem timeout (300 ms) e excecao que sobe por ele
  mata o listener. Entao o hook NAO trabalha: le modificador, empilha na fila e
  volta. Uma thread trabalhadora cuida de audio, HTTP e injecao.

  O stream de audio fica aberto de forma persistente, aberto testando candidato
  por candidato de verdade - passar no check_input_settings nao garante o start().

  Tkinter (overlay) exige a thread principal, entao o mainloop fica la e o
  listener de teclado roda na thread dele.
"""

import io
import os
import re
import sys
import json
import time
import wave
import queue
import ctypes
import logging
import threading
import traceback
import winsound
import urllib.request
from ctypes import wintypes

import numpy as np
import sounddevice as sd
from pynput import keyboard

import textproc
import sysaudio
import volume
import llm
from config import CFG, vk_of, describe

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("WFL_DATA") or os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
LOG = os.path.join(DATA, "dictate.log")
HISTORY = os.path.join(DATA, "history.json")

# encoding explicito: sem isto o Python usa a codificacao local (cp1252 no
# Windows brasileiro) para o arquivo de log, e uma transcricao com caractere
# fora dessa tabela faz o log falhar calado - justamente nas linhas que a gente
# mais precisa quando algo da errado.
logging.basicConfig(
    filename=LOG, filemode="a", level=logging.INFO, encoding="utf-8",
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
)
log = logging.getLogger("ditado")

# Escape hatch para testar sem colar nada na janela ativa de quem esta usando.
NO_INJECT = os.environ.get("DITADO_NO_INJECT", "") not in ("", "0")

API_PREFERENCE = ["Windows WASAPI", "Windows DirectSound", "MME"]

# ---------------------------------------------------------------------------
# Win32
# ---------------------------------------------------------------------------
u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

u32.OpenClipboard.argtypes = [wintypes.HWND]
u32.OpenClipboard.restype = wintypes.BOOL
u32.GetClipboardData.argtypes = [wintypes.UINT]
u32.GetClipboardData.restype = wintypes.HANDLE
u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
u32.SetClipboardData.restype = wintypes.HANDLE
u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
u32.GetAsyncKeyState.restype = ctypes.c_short
u32.GetForegroundWindow.restype = wintypes.HWND
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE
k32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
k32.GlobalAlloc.restype = wintypes.HGLOBAL
k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
k32.GlobalLock.restype = wintypes.LPVOID
k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]


def _open_clipboard(tries=12):
    for _ in range(tries):
        if u32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def clipboard_get():
    if not _open_clipboard():
        return None
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = k32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


def clipboard_set(text):
    if not _open_clipboard():
        return False
    try:
        u32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        h = k32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return False
        p = k32.GlobalLock(h)
        ctypes.memmove(p, buf, size)
        k32.GlobalUnlock(h)
        return bool(u32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        u32.CloseClipboard()


def foreground_app():
    """(hwnd, nome-do-executavel) da janela em foco. Usado pelos perfis."""
    try:
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return None, ""
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return hwnd, ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return hwnd, os.path.basename(buf.value)
        finally:
            k32.CloseHandle(h)
        return hwnd, ""
    except Exception:
        return None, ""


def window_title(hwnd):
    try:
        if not hwnd:
            return ""
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


def wait_mods_released(timeout=2.0):
    """
    Em push-to-talk o usuario solta a tecla final mas ainda segura Ctrl+Alt.
    Colar nesse instante faria o app receber Ctrl+Alt+V em vez de Ctrl+V.
    """
    vks = (0x11, 0x12, 0x10)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not any(u32.GetAsyncKeyState(v) & 0x8000 for v in vks):
            return True
        time.sleep(0.02)
    return False


def beep(kind):
    if not CFG.get("audio.beeps", True):
        return

    def run():
        try:
            if kind == "start":
                winsound.Beep(880, 70)
            elif kind == "stop":
                winsound.Beep(1320, 60)
            elif kind == "ok":
                winsound.Beep(1560, 50)
            elif kind == "cancel":
                winsound.Beep(600, 60)
                winsound.Beep(450, 60)
            else:
                winsound.Beep(400, 130)
                winsound.Beep(300, 160)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
def candidate_mics(name):
    out = []
    try:
        devices = list(enumerate(sd.query_devices()))
    except Exception as exc:
        log.error("nao consegui listar dispositivos: %s", exc)
        return out

    def collect(require_name):
        for i, d in devices:
            if d.get("max_input_channels", 0) < 1:
                continue
            try:
                api = sd.query_hostapis(d["hostapi"])["name"]
            except Exception:
                continue
            if api not in API_PREFERENCE:
                continue
            if require_name and name and name.lower() not in d["name"].lower():
                continue
            out.append((API_PREFERENCE.index(api), i, d["name"], api, d))

    collect(True)
    if not out:
        collect(False)  # nome nao achado: aceita qualquer entrada boa
    out.sort(key=lambda x: (x[0], x[1]))
    return out


class AudioEngine:
    """
    Stream de entrada persistente; acumula frames so quando gravando.
    Abrir no acionamento custaria os primeiros milissegundos da fala e podia
    falhar - foi o que derrubou a primeira versao.
    """

    def __init__(self):
        self.stream = None
        self.rate = 16000
        self.channels = 1
        self.desc = "nenhum"
        self.recording = False
        self.buf = []
        self.lock = threading.RLock()
        self.last_use = time.time()
        self.level = 0.0

    def _cb(self, indata, frames, time_info, status):
        if status:
            log.debug("status do stream: %s", status)
        if self.recording:
            self.buf.append(indata.copy())
            try:
                self.level = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
            except Exception:
                pass

    def open(self):
        with self.lock:
            if self.stream is not None:
                return True
            tried = []
            want = CFG.get("audio.mic_name", "")
            for _, idx, name, api, dev in candidate_mics(want):
                native = int(dev.get("default_samplerate") or 48000)
                maxch = int(dev.get("max_input_channels") or 1)
                for rate in dict.fromkeys([native, 48000, 44100, 16000]):
                    for ch in dict.fromkeys([1, min(2, maxch)]):
                        st = None
                        try:
                            st = sd.InputStream(
                                samplerate=rate, channels=ch, dtype="int16",
                                device=idx, callback=self._cb, blocksize=0)
                            st.start()
                        except Exception as exc:
                            tried.append("[%s]%s@%d/%dch: %s" % (
                                idx, api, rate, ch, str(exc).split("\n")[0][:60]))
                            try:
                                if st is not None:
                                    st.close()
                            except Exception:
                                pass
                            continue
                        self.stream, self.rate, self.channels = st, rate, ch
                        self.desc = "[%d] %s (%s) @ %d Hz, %d ch" % (
                            idx, name, api, rate, ch)
                        log.info("mic aberto: %s", self.desc)
                        if tried:
                            log.info("falhas antes: %s", " | ".join(tried[:5]))
                        self.last_use = time.time()
                        return True
            log.error("nenhuma entrada abriu. Tentativas: %s",
                      " | ".join(tried[:10]) or "nenhuma candidata")
            return False

    def close(self):
        with self.lock:
            if self.stream is None:
                return
            try:
                self.stream.stop()
                self.stream.close()
            except Exception as exc:
                log.warning("erro ao fechar o mic: %s", exc)
            self.stream = None
            self.recording = False
            self.buf = []
            log.info("mic fechado por ociosidade")

    def begin(self):
        with self.lock:
            if not self.open():
                return False
            self.buf = []
            self.recording = True
            self.last_use = time.time()
            return True

    def end(self):
        with self.lock:
            if not self.recording:
                return None
            self.recording = False
            frames, self.buf = self.buf, []
            self.last_use = time.time()
        if not frames:
            return None
        data = np.concatenate(frames, axis=0)
        if data.ndim > 1 and data.shape[1] > 1:
            data = data.astype(np.int32).mean(axis=1).astype(np.int16)
        # Sempre 1-D. Com um unico canal a forma sai (N,1), e isso passou batido
        # por muito tempo porque tobytes() achata sozinho ao montar o WAV - mas
        # misturar com outra fonte em (N,) viraria broadcast (N,N).
        return np.asarray(data).reshape(-1)

    def discard(self):
        with self.lock:
            self.recording = False
            self.buf = []
            self.last_use = time.time()

    def idle_watch(self):
        while True:
            time.sleep(30)
            try:
                limit = int(CFG.get("audio.mic_idle_close_s", 300) or 0)
                with self.lock:
                    if (self.stream is not None and not self.recording
                            and limit > 0 and time.time() - self.last_use > limit):
                        self.close()
            except Exception:
                log.error("idle_watch: %s", traceback.format_exc())


def to_wav_bytes(data, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(data.tobytes())
    return buf.getvalue()


def transcribe(wav_bytes, language, prompt="", task="transcribe"):
    boundary = "----ditado"
    body = io.BytesIO()

    def field(name, value):
        body.write(("--%s\r\n" % boundary).encode())
        body.write(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode())
        body.write(str(value).encode("utf-8"))
        body.write(b"\r\n")

    body.write(("--%s\r\n" % boundary).encode())
    body.write(b'Content-Disposition: form-data; name="file"; filename="ditado.wav"\r\n')
    body.write(b"Content-Type: audio/wav\r\n\r\n")
    body.write(wav_bytes)
    body.write(b"\r\n")
    field("language", language or "pt")
    field("task", task)
    if prompt:
        field("prompt", prompt)
    body.write(("--%s--\r\n" % boundary).encode())

    server = CFG.get("transcription.server", "http://127.0.0.1:9978").rstrip("/")
    req = urllib.request.Request(
        server + "/v1/audio/transcriptions", data=body.getvalue(),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Historico
# ---------------------------------------------------------------------------
_hist_lock = threading.Lock()


def history_add(entry):
    if not CFG.get("history.enabled", True):
        return
    cap = int(CFG.get("history.max", 200) or 200)
    with _hist_lock:
        items = []
        try:
            with open(HISTORY, "r", encoding="utf-8") as f:
                items = json.load(f)
            if not isinstance(items, list):
                items = []
        except Exception:
            items = []
        items.append(entry)
        items = items[-cap:]
        try:
            tmp = HISTORY + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
            os.replace(tmp, HISTORY)
        except Exception:
            log.warning("nao consegui gravar o historico")


# ---------------------------------------------------------------------------
# LLM (qualquer IA de texto: Ollama local ou endpoint compativel com OpenAI)
# ---------------------------------------------------------------------------
def _llm_cfg():
    return CFG.get("llm", {}) or {}


def llm_polish(text):
    cfg = _llm_cfg()
    return llm.chat(cfg, cfg.get("prompt") or "", text, task="polish",
                    timeout=float(cfg.get("command_timeout_s", 30) or 30))


def llm_translate(text):
    """Traduz o ditado para ingles usando o LLM configurado."""
    cfg = _llm_cfg()
    return llm.chat(cfg, "Translate the following text to English. Output ONLY the "
                    "translated text, nothing else.", text, task="translate",
                    timeout=float(cfg.get("command_timeout_s", 30) or 30))

# ---------------------------------------------------------------------------
# Limpeza automatica (estilo "auto-edit" do Wispr Flow)
# ---------------------------------------------------------------------------
_warm = {"at": 0.0}


def warmup_async():
    """Manda o servidor carregar o modelo enquanto a pessoa ainda fala. Sem
    isso, o 1o ditado depois de 1h parado esperava ~12s de carga da VRAM."""
    if time.time() - _warm["at"] < 20:
        return
    _warm["at"] = time.time()

    def run():
        try:
            srv = CFG.get("transcription.server", "http://127.0.0.1:9978").rstrip("/")
            req = urllib.request.Request(srv + "/warmup", data=b"", method="POST")
            urllib.request.urlopen(req, timeout=60).close()
        except Exception as exc:
            log.debug("warmup falhou: %s", exc)
    threading.Thread(target=run, daemon=True).start()
    # Ollama tambem: carrega o modelo de limpeza enquanto a pessoa ainda fala.
    threading.Thread(target=llm.warm, args=(_llm_cfg(),), daemon=True).start()


def auto_clean(text, prof=None):
    """
    Passa o ditado pelo LLM antes de colar. Devolve o texto limpo, ou None
    para usar o original. Nunca pode atrasar nem estragar o ditado:
      - timeout curto: estourou, cola o bruto;
      - frase curta demais nao tem o que limpar, nem gasta rede;
      - texto embrulhado em <ditado>: o modelo nao confunde fala com pedido;
      - guarda de tamanho: se a saida inchou ou encolheu demais, o modelo
        respondeu em vez de limpar, e o bruto e mais seguro.
    """
    cfg = _llm_cfg()
    if not cfg.get("auto_clean") or not llm.enabled(cfg):
        return None
    if prof is not None and prof.get("llm_clean") is False:
        return None
    if len(text.split()) < int(cfg.get("auto_min_words", 4) or 0):
        return None
    prompt = cfg.get("auto_prompt") or cfg.get("prompt") or ""
    voc = [str(v).strip() for v in (CFG.get("transcription.vocabulary", []) or []) if str(v).strip()]
    if voc:
        prompt += "\n\nGrafias corretas de nomes e termos desta pessoa: %s." % ", ".join(voc)
    style = (prof or {}).get("llm_style")
    if style:
        prompt += "\n\nEstilo deste aplicativo: %s" % style
    t0 = time.time()
    try:
        # O fallback (se configurado) e tentado dentro do llm.chat.
        out = llm.chat(cfg, prompt, "<ditado>\n%s\n</ditado>" % text, task="clean",
                       timeout=float(cfg.get("auto_timeout_s", 4) or 4),
                       temperature=0.1, max_tokens=2000)
    except Exception as exc:
        log.warning("limpeza LLM pulada (%.1fs): %s", time.time() - t0, str(exc)[:160])
        return None
    out = out.replace("<ditado>", "").replace("</ditado>", "").strip().strip('"').strip()
    if not out:
        return None
    if len(out) > len(text) * 1.5 + 40 or len(out) < len(text) * 0.35:
        log.warning("limpeza LLM rejeitada (tamanho %d -> %d): %r", len(text), len(out), out[:100])
        return None
    # Trava anti-corte: a limpeza so tira hesitacao e autocorrecao, entao o
    # texto nao pode perder metade das palavras. Se perdeu, o modelo resumiu,
    # e colar o bruto e melhor que sumir com o que a pessoa disse.
    nin, nout = len(text.split()), len(out.split())
    if nin >= 12 and nout < nin * float(cfg.get("auto_min_keep", 0.55) or 0):
        log.warning("limpeza LLM rejeitada (cortou %d -> %d palavras): %r", nin, nout, out[:100])
        return None
    # Trava anti-invencao: o LLM tende a "consertar" conta que parece errada
    # ("fica R$ 25 a menos"), mudando o sentido de um orcamento. Expressao de
    # quantia/conta que aparece na saida e nao estava no ditado = rejeita.
    m_inv = invented_phrase(text, out)
    if m_inv:
        log.warning("limpeza LLM rejeitada (inventou %r): %r", m_inv, out[:120])
        return None
    if not numbers_preserved(text, out):
        log.warning("limpeza LLM rejeitada (numero novo): %r", out[:120])
        return None
    log.info("limpeza LLM em %.2fs", time.time() - t0)
    return out


def llm_command(instruction, selection):
    """Command Mode: aplica a instrucao falada no texto selecionado (ou gera
    texto novo se nao houver selecao). Usa llm.models.command se definido:
    aqui qualidade pesa mais que velocidade."""
    cfg = _llm_cfg()
    system = (
        "Você é uma ferramenta de edição de texto acionada por voz. "
        "Recebe uma INSTRUÇÃO falada e, opcionalmente, um TEXTO selecionado. "
        "Com TEXTO: aplique a instrução ao texto e devolva o texto resultante completo "
        "(ex.: 'deixa mais curto', 'traduz pro inglês', 'transforma em lista', 'mais educado'). "
        "Sem TEXTO: escreva o texto que a instrução pede. "
        "Mantenha o idioma do texto original salvo se a instrução pedir tradução. "
        "Responda SOMENTE com o texto final, pronto para colar: sem aspas, sem comentários, "
        "sem 'Aqui está', sem markdown de bloco de código, a menos que a instrução peça código.")
    user = "<instrucao>\n%s\n</instrucao>" % instruction
    if selection:
        user += "\n<texto>\n%s\n</texto>" % selection
    out = llm.chat(cfg, system, user, task="command",
                   timeout=float(cfg.get("command_timeout_s", 30) or 30),
                   temperature=0.3, max_tokens=8000)
    for tag in ("<texto>", "</texto>", "<instrucao>", "</instrucao>"):
        out = out.replace(tag, "")
    return out.strip()


u32.GetClipboardSequenceNumber.restype = wintypes.DWORD


def _llm_chat(system, user, model=None, timeout=30, temperature=0.3):
    out = llm.chat(_llm_cfg(), system, user, task="command", timeout=timeout,
                   temperature=temperature, max_tokens=8000)
    for tag in ("<texto>", "</texto>"):
        out = out.replace(tag, "")
    return out.strip()


_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_INVENTA = ("a menos", "a mais", "de desconto", "desconto", "de diferença", "economia",
            "economiza", "adicional", "acréscimo", "acrescimo")


def invented_phrase(src, out):
    """Expressao de conta/valor que o LLM colocou e nao estava no original.
    Mesmo com proibicao no prompt, flash e flash-lite 'consertam' conta que
    parece errada em boa parte das vezes."""
    a, b = (src or "").lower(), (out or "").lower()
    for m in _INVENTA:
        if m in b and m not in a:
            return m
    return None


_NUM_WORDS = re.compile(r"\b(um|uma|dois|duas|tr[eê]s|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|treze|"
                        r"catorze|quatorze|quinze|dezesseis|dezessete|dezoito|dezenove|vinte|trinta|quarenta|"
                        r"cinquenta|sessenta|setenta|oitenta|noventa|cem|cento|duzentos|trezentos|quatrocentos|"
                        r"quinhentos|seiscentos|setecentos|oitocentos|novecentos|mil|meio-dia|meio dia|meia-noite|meia noite)\b", re.IGNORECASE)


def numbers_preserved(src, out):
    """Todo numero da saida ja existia na entrada (R$ 350,00 == 350).
    Se a entrada tem numero por extenso ('trezentos e cinquenta'), virar
    algarismo e legitimo: nesse caso so a trava de expressoes vale."""
    if _NUM_WORDS.search(src or ""):
        return True
    norm = lambda s: {n.replace(".", "").replace(",00", "") for n in _NUM.findall(s)}
    ins = norm(src)
    return not ins or norm(out) <= ins


def llm_transform(instruction, text):
    system = ("Você é uma ferramenta de reescrita de texto em português do Brasil. "
              "Aplique a INSTRUÇÃO ao texto entre <texto> e </texto> e devolva SOMENTE o "
              "texto final pronto para colar: sem aspas, sem comentários, sem 'Aqui está', sem colchetes.\n"
              "Valores, preços e prazos: copie exatamente como estão. Nunca calcule, nunca interprete "
              "relação entre valores e nunca acrescente 'a menos', 'a mais', 'adicional' ou 'desconto'.\n\n"
              "INSTRUÇÃO: " + instruction)
    cfg = CFG.get("llm", {}) or {}
    return _llm_chat(system, "<texto>\n%s\n</texto>" % text,
                     timeout=float(cfg.get("command_timeout_s", 30) or 30))


def grab_selection(timeout=0.6):
    """
    Copia o que estiver selecionado na janela em foco (Ctrl+C) e devolve o
    clipboard do usuario como estava. Usa o numero de sequencia do clipboard
    para saber se o Ctrl+C copiou algo: sem selecao, muitos apps nao mexem no
    clipboard, e ler o conteudo antigo faria o comando agir no texto errado.
    """
    if NO_INJECT:
        return ""
    saved = clipboard_get()
    if not wait_mods_released():
        log.warning("command: modificadores presos antes do Ctrl+C")
    seq = u32.GetClipboardSequenceNumber()
    kb = keyboard.Controller()
    time.sleep(0.03)
    with kb.pressed(keyboard.Key.ctrl):
        kb.press("c")
        kb.release("c")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if u32.GetClipboardSequenceNumber() != seq:
            break
        time.sleep(0.02)
    if u32.GetClipboardSequenceNumber() == seq:
        return ""  # nada selecionado
    time.sleep(0.03)
    sel = clipboard_get() or ""
    if saved is not None:
        clipboard_set(saved)
    return sel


# ---------------------------------------------------------------------------
# Injecao
# ---------------------------------------------------------------------------
_last = {"text": "", "hwnd": None, "at": 0.0}


def _needs_leading_space(text, hwnd):
    """
    Nao existe API para saber se o cursor esta no inicio da linha. O caso que
    realmente incomoda e ditar duas vezes seguidas na mesma janela e as palavras
    grudarem, entao a heuristica cobre exatamente esse: mesma janela, pouco tempo
    atras, e nenhum dos lados ja tem separador.
    """
    if not CFG.get("text.smart_spacing", True):
        return False
    if hwnd is None or _last["hwnd"] != hwnd:
        return False
    if time.time() - _last["at"] > 120:
        return False
    prev = _last["text"]
    if not prev or prev.endswith(("\n", " ")):
        return False
    if text[:1] in (" ", "\n", ",", ".", ";", ":", "!", "?", ")"):
        return False
    return True


_clip = {"saved": None, "pending": 0, "lock": threading.Lock()}


def paste(text, hwnd=None):
    if NO_INJECT:
        log.info("[NO_INJECT] texto seria: %r", text)
        return True
    # Com a fila, dois ditados podem colar em menos de 1,2s. So o primeiro
    # salva o clipboard do usuario; o ultimo a terminar e quem devolve.
    with _clip["lock"]:
        if _clip["pending"] == 0:
            _clip["saved"] = clipboard_get()
        _clip["pending"] += 1
    if not clipboard_set(text):
        log.error("falha ao escrever no clipboard")
        with _clip["lock"]:
            _clip["pending"] -= 1
        return False
    if not wait_mods_released():
        log.warning("modificadores ainda pressionados; colando mesmo assim")
    kb = keyboard.Controller()
    time.sleep(0.05)
    with kb.pressed(keyboard.Key.ctrl):
        kb.press("v")
        kb.release("v")

    def restore():
        time.sleep(1.2)
        with _clip["lock"]:
            _clip["pending"] -= 1
            if _clip["pending"] == 0 and _clip["saved"] is not None:
                clipboard_set(_clip["saved"])
    threading.Thread(target=restore, daemon=True).start()
    return True


def send_undo():
    if not wait_mods_released():
        log.warning("modificadores presos; mandando undo mesmo assim")
    kb = keyboard.Controller()
    time.sleep(0.05)
    with kb.pressed(keyboard.Key.ctrl):
        kb.press("z")
        kb.release("z")


# ---------------------------------------------------------------------------
# Trabalhadora
# ---------------------------------------------------------------------------
ST_IDLE, ST_HOLDING, ST_TOGGLE, ST_ENDING = "idle", "holding", "toggle", "ending"


class Chunker:
    """
    Transcricao em pedacos ENQUANTO a pessoa fala.

    Antes, a transcricao so comecava ao soltar a tecla: ditado de 30s = ~3s
    de Whisper esperando no fim. Aqui uma thread olha o audio que ja chegou a
    cada 250 ms, acha uma pausa natural (respiracao entre frases) depois de
    pelo menos min_chunk_s e manda aquele trecho pro servidor na hora. Ao
    soltar a tecla so falta o ultimo pedaco.

    Corta SEMPRE em silencio, nunca no meio de palavra. Fala corrida sem
    pausa por max_chunk_s: corta no ponto mais baixo de energia do trecho.
    Qualquer falha (pedaco com erro, servidor lento) -> finalize() devolve
    None e o Worker transcreve o audio inteiro como antes: o pior caso e o
    comportamento antigo, nunca texto perdido.
    """

    FR = 0.03  # janela de analise de energia, em segundos

    def __init__(self, audio, lang, prompt):
        cfg = CFG.get("streaming", {}) or {}
        self.audio = audio
        self.rate = int(audio.rate)
        self.lang = lang
        self.prompt = prompt or ""
        self.min_s = float(cfg.get("min_chunk_s", 4))
        self.max_s = float(cfg.get("max_chunk_s", 20))
        self.pause_s = float(cfg.get("pause_s", 0.35))
        self.sil = float(CFG.get("audio.silence_rms", 120))
        self.pcm = np.zeros(0, dtype=np.int16)
        self.seen = 0          # blocos de audio.buf ja copiados
        self.committed = 0     # amostras ja despachadas em pedacos
        self.texts = []
        self.failed = False
        self.n_chunks = 0
        self.gpu_s = 0.0
        self.stop_ev = threading.Event()
        self.work = queue.Queue()
        self.t_pump = threading.Thread(target=self._pump, daemon=True)
        self.t_send = threading.Thread(target=self._send, daemon=True)
        self.t_pump.start()
        self.t_send.start()

    # ---- captura ---------------------------------------------------------
    def _grab(self):
        buf = self.audio.buf
        n = len(buf)
        if n <= self.seen:
            return
        new = buf[self.seen:n]
        self.seen = n
        arr = np.concatenate(new, axis=0)
        if arr.ndim > 1 and arr.shape[1] > 1:
            arr = arr.astype(np.int32).mean(axis=1).astype(np.int16)
        self.pcm = np.concatenate([self.pcm, np.asarray(arr).reshape(-1)])

    def _find_cut(self):
        rate, start = self.rate, self.committed
        avail = len(self.pcm) - start
        if avail < self.min_s * rate:
            return None
        fr = int(self.FR * rate)
        seg = self.pcm[start:].astype(np.float32)
        nfr = len(seg) // fr
        e = np.sqrt(np.mean(seg[:nfr * fr].reshape(nfr, fr) ** 2, axis=1))
        floor = float(np.percentile(e, 15))
        thr = max(self.sil * 1.2, floor * 2.5)
        need = max(2, int(round(self.pause_s / self.FR)))
        min_fr = int(self.min_s / self.FR)
        quiet = e < thr
        run = 0
        for i in range(min_fr, nfr):
            run = run + 1 if quiet[i] else 0
            if run >= need:
                # meio da pausa: nao come o fim da palavra anterior nem o
                # comeco da proxima
                return start + (i - run // 2) * fr
        if avail >= self.max_s * rate:
            i = min_fr + int(np.argmin(e[min_fr:]))
            log.info("chunker: fala corrida %.0fs sem pausa, corte forcado", avail / rate)
            return start + i * fr
        return None

    def _pump(self):
        while not self.stop_ev.wait(0.25):
            try:
                self._grab()
                cut = self._find_cut()
                if cut:
                    piece = self.pcm[self.committed:cut]
                    self.committed = cut
                    self.work.put(piece)
            except Exception:
                log.warning("chunker pump: %s", traceback.format_exc())
                self.failed = True
                return

    # ---- envio (uma thread: pedacos saem na ordem) ------------------------
    def _send(self):
        while True:
            piece = self.work.get()
            if piece is None:
                return
            if self.failed or not len(piece):
                continue
            rms = float(np.sqrt(np.mean(piece.astype(np.float32) ** 2)))
            if rms < self.sil:
                continue  # trecho mudo: Whisper inventaria "Hum."
            try:
                ctx = " ".join(t for t in self.texts if t)[-200:]
                prompt = (self.prompt + " " + ctx).strip()
                t0 = time.time()
                r = transcribe(to_wav_bytes(piece, self.rate), self.lang, prompt)
                self.gpu_s += time.time() - t0
                log.info("chunker: pedaco %d (%.1fs de audio) em %.2fs", self.n_chunks + 1,
                         len(piece) / float(self.rate), time.time() - t0)
                self.texts.append((r.get("text") or "").strip())
                self.n_chunks += 1
            except Exception as exc:
                log.warning("chunker: pedaco falhou (%s); vai o audio inteiro", exc)
                self.failed = True

    # ---- API para o Worker ------------------------------------------------
    def stop_capture(self):
        """Chamar ANTES de audio.end(): para de ler o buffer."""
        self.stop_ev.set()
        self.t_pump.join(1.0)

    def abort(self):
        self.failed = True
        self.stop_ev.set()
        self.work.put(None)

    def finalize(self, data):
        """data = audio completo da gravacao. Devolve dict no formato de
        transcribe() ou None (o chamador cai no audio inteiro)."""
        t0 = time.time()
        self.stop_capture()
        data = np.asarray(data).reshape(-1)
        if self.committed > len(data):
            self.failed = True
        else:
            self.work.put(data[self.committed:])
        self.work.put(None)
        self.t_send.join(timeout=90)
        if self.failed or self.t_send.is_alive():
            return None
        text = " ".join(t for t in self.texts if t).strip()
        dur = len(data) / float(self.rate)
        wait = time.time() - t0
        return {"text": text, "duration": round(dur, 2), "elapsed": round(wait, 2),
                "speed": round(dur / wait, 1) if wait > 0 else 0,
                "chunks": self.n_chunks, "gpu_s": round(self.gpu_s, 2)}


class Worker(threading.Thread):
    def __init__(self, audio, overlay=None, sysengine=None):
        super().__init__(daemon=True)
        self.audio = audio
        self.sys = sysengine
        self.ov = overlay
        self.q = queue.Queue()
        self.state = ST_IDLE
        self.action = "dictate"     # qual atalho iniciou a gravacao
        self.pressed_at = 0.0
        self.busy = False
        self.deadline = 0.0
        # Fila de transcricao: gravar o proximo ditado nao espera o anterior
        # terminar. Um consumidor so, entao os textos colam na ordem falada.
        self.jobs = queue.Queue()
        self.chunker = None
        threading.Thread(target=self._consume, daemon=True).start()

    def post(self, kind, ts, action="dictate"):
        self.q.put((kind, ts, action))

    def run(self):
        while True:
            try:
                kind, ts, action = self.q.get(timeout=1.0)
            except queue.Empty:
                if self.deadline and time.time() > self.deadline:
                    log.warning("limite de %ss atingido, encerrando",
                                CFG.get("audio.max_s", 180))
                    self.state = ST_IDLE
                    self._finish()
                continue
            try:
                if kind == "down":
                    self._down(ts, action)
                elif kind == "up":
                    self._up(ts)
                elif kind == "cancel":
                    self._cancel()
                elif kind == "undo":
                    self._undo()
                elif kind == "reinject":
                    self._reinject()
                elif kind == "polish":
                    self._polish()
                elif kind == "revert":
                    self._revert_raw()
                elif kind == "learn":
                    self._learn_word()
                elif kind.startswith("transform"):
                    self._transform(int(kind[-1]) - 1)
            except Exception:
                log.error("erro tratando %s: %s", kind, traceback.format_exc())
                beep("erro")
                # Falha nao pode deixar o PC mudo para sempre.
                try:
                    volume.restore()
                except Exception:
                    pass
                self.state = ST_IDLE
                self.busy = False
                self.deadline = 0.0

    # ---- gravacao -------------------------------------------------------
    def _needs(self, action):
        """(usa microfone, usa saida do sistema) para cada acao."""
        if action == "system":
            return False, True
        if action == "both":
            return True, True
        return True, False

    def _down(self, ts, action):
        if self.state == ST_IDLE:
            if self.jobs.qsize() >= 3:
                log.info("fila cheia (%d na espera); acionamento ignorado", self.jobs.qsize())
                beep("erro")
                return
            warmup_async()
            usa_mic, usa_sys = self._needs(action)
            if usa_sys and self.sys is None:
                log.error("captura de sistema indisponivel nesta maquina")
                beep("erro")
                self._ov("error")
                self._ov("hide", 1500)
                return
            if usa_mic and not self.audio.begin():
                beep("erro")
                self._ov("error")
                self._ov("hide", 1500)
                return
            if usa_sys and not self.sys.begin():
                if usa_mic:
                    self.audio.discard()
                beep("erro")
                self._ov("error")
                self._ov("hide", 1500)
                return

            # Silencia a saida do PC enquanto grava, para o som nao vazar pelo
            # microfone. So quando a fonte e o microfone: nas capturas de
            # sistema isso mataria exatamente o audio que se quer gravar.
            if (not usa_sys and CFG.get("audio.mute_while_dictating", True)):
                volume.silence(int(CFG.get("audio.mute_duck_percent", 0) or 0))

            self.action = action
            self.pressed_at = ts
            self.state = ST_HOLDING
            # Conversa e reuniao sao muito mais longas que um ditado.
            limite = float(CFG.get("audio.max_s_system", 1800) if usa_sys
                           else CFG.get("audio.max_s", 180))
            self.deadline = time.time() + limite
            beep("start")
            self._ov("recording")
            self.chunker = None
            if (usa_mic and not usa_sys and action in ("dictate", "clipboard", "english")
                    and (CFG.get("streaming", {}) or {}).get("enabled", True)):
                try:
                    hwnd0, exe0 = foreground_app()
                    prof0 = textproc.profile_for(exe0, CFG.get("profiles", []), window_title(hwnd0))
                    lang0 = (prof0 or {}).get("language", CFG.get("transcription.language", "pt"))
                    self.chunker = Chunker(self.audio, lang0, CFG.get("transcription.initial_prompt", ""))
                except Exception:
                    log.warning("chunker nao iniciou: %s", traceback.format_exc())
                    self.chunker = None
            log.info("gravando (%s, ate %.0fs)...", action, limite)
        elif self.state == ST_TOGGLE:
            self.state = ST_ENDING
            self._finish()

    def _up(self, ts):
        if self.state == ST_HOLDING:
            held_ms = (ts - self.pressed_at) * 1000
            if held_ms < float(CFG.get("audio.tap_ms", 300)):
                self.state = ST_TOGGLE
                log.info("toque rapido (%.0fms) -> modo toggle, gravando", held_ms)
            else:
                self.state = ST_IDLE
                self._finish()
        elif self.state == ST_ENDING:
            self.state = ST_IDLE

    def _cancel(self):
        if self.state in (ST_HOLDING, ST_TOGGLE):
            if self.chunker is not None:
                self.chunker.abort()
                self.chunker = None
            self.audio.discard()
            if self.sys is not None:
                self.sys.discard()
            volume.restore()
            self.state = ST_IDLE
            self.deadline = 0.0
            beep("cancel")
            self._ov("hide")
            log.info("gravacao cancelada pelo usuario")

    def _ov(self, what, arg=None):
        if self.ov is None:
            return
        try:
            {"recording": self.ov.recording,
             "transcribing": self.ov.transcribing,
             "error": self.ov.error}.get(what, lambda: None)()
            if what == "hide":
                self.ov.hide(arg or 0)
        except Exception:
            log.warning("overlay: %s", traceback.format_exc())

    def _finish(self):
        self.deadline = 0.0
        beep("stop")
        # Devolve o som assim que voce PARA de falar, sem esperar a transcricao:
        # o audio ja foi capturado, nao ha mais o que vazar.
        volume.restore()
        usa_mic, usa_sys = self._needs(self.action)

        ch, self.chunker = self.chunker, None
        if ch is not None:
            ch.stop_capture()  # para de ler o buffer antes do end() zerar
        mic_data = self.audio.end() if usa_mic else None
        sys_data = self.sys.end() if (usa_sys and self.sys is not None) else None

        if self.action == "system":
            data, rate = sys_data, (self.sys.rate if self.sys else 48000)
        elif self.action == "both":
            data, rate = sysaudio.mix(mic_data, self.audio.rate,
                                      sys_data, self.sys.rate if self.sys else 48000)
        else:
            data, rate = mic_data, self.audio.rate

        if data is None or not len(data):
            log.info("nada gravado")
            if ch is not None:
                ch.abort()
            self._ov("hide")
            return
        secs = len(data) / float(rate)
        if secs * 1000 < float(CFG.get("audio.min_ms", 350)):
            log.info("curto demais (%.0fms), ignorando", secs * 1000)
            if ch is not None:
                ch.abort()
            self._ov("hide")
            return
        # Porta de silencio: com audio quase mudo o Whisper inventa interjeicao
        # ("Hum, hum."), e injetar isso e pior que nao fazer nada.
        rms = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
        limiar = float(CFG.get("audio.silence_rms", 120))
        if rms < limiar:
            log.info("silencio (RMS=%.1f < %.0f), ignorando", rms, limiar)
            if ch is not None:
                ch.abort()
            self._ov("hide")
            return
        self._ov("transcribing")
        hwnd, exe = foreground_app()
        sel = grab_selection() if self.action == "command" else None
        self.jobs.put((data, rate, self.action, hwnd, exe, sel, ch))
        if self.jobs.qsize() > 1:
            log.info("na fila: %d ditado(s) aguardando", self.jobs.qsize())

    def _consume(self):
        while True:
            job = self.jobs.get()
            self.busy = True
            try:
                self._work(*job)
            finally:
                self.busy = not self.jobs.empty()

    def _ov_done(self, what, arg=None):
        """Overlay de fim de transcricao. Se ja comecou outra gravacao, nao
        pode esconder nem pintar de erro a barra que esta mostrando 'ouvindo'."""
        if self.state in (ST_HOLDING, ST_TOGGLE):
            return
        if what == "hide" and not self.jobs.empty():
            return  # ainda tem ditado na fila: continua 'transcrevendo'
        self._ov(what, arg)

    # ---- pos-processamento e entrega ------------------------------------
    def _work(self, data, rate, action, hwnd=None, exe="", sel=None, ch=None):
        try:
            t0 = time.time()
            if hwnd is None:
                hwnd, exe = foreground_app()

            opts = dict(CFG.get("text", {}) or {})
            lang = CFG.get("transcription.language", "pt")
            prof = textproc.profile_for(exe, CFG.get("profiles", []), window_title(hwnd))
            if prof:
                for k in ("capitalize", "voice_commands", "smart_spacing",
                          "strip_trailing_period"):
                    if k in prof:
                        opts[k] = prof[k]
                lang = prof.get("language", lang)
                log.info("perfil '%s' aplicado (app=%s)", prof.get("name") or prof.get("match") or prof.get("title"), exe)
            if action == "command":
                self._command_finish(data, rate, lang, hwnd, exe, t0, sel)
                return
            res = None
            if ch is not None:
                res = ch.finalize(data)
                if res is None:
                    log.warning("chunker falhou; transcrevendo o audio inteiro")
                elif res.get("chunks", 0) > 1:
                    log.info("chunker: %d pedacos, GPU total %.2fs, espera apos soltar %.2fs",
                             res["chunks"], res["gpu_s"], res["elapsed"])
            if res is None:
                res = transcribe(to_wav_bytes(data, rate), lang,
                                 CFG.get("transcription.initial_prompt", ""))
            t_asr = time.time() - t0

            raw = (res.get("text") or "").strip()
            if not raw:
                # Registra nivel e duracao: sem isso nao da para distinguir
                # "capturou o dispositivo errado" de "capturou musica, que nao
                # tem fala" - foi exatamente a duvida no teste do Ctrl+Alt+S.
                try:
                    rms_dbg = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
                except Exception:
                    rms_dbg = -1
                log.info("sem fala reconhecivel (%s: %.1fs, RMS=%.1f, %d Hz)",
                         action, len(data) / float(rate), rms_dbg, rate)
                beep("erro")
                self._ov_done("error")
                self._ov_done("hide", 1200)
                return

            # "..., envia" no fim = colar e apertar Enter. Detectado no BRUTO,
            # antes da limpeza, para o LLM nunca ver (nem apagar) o comando.
            send = False
            if action == "dictate" and CFG.get("text.send_command", True):
                raw_sem, send = textproc.split_send(raw)
                if send:
                    log.info("comando de envio detectado")
                    raw = raw_sem
                    if not raw.strip():
                        # so falou "envia": aperta Enter no que ja esta escrito
                        self._press_enter(hwnd)
                        beep("ok")
                        self._ov_done("hide", 250)
                        return

            # Snippet: fala inteira = gatilho -> texto salvo, exatamente como
            # foi cadastrado (sem limpeza, sem LLM, sem capitalizacao).
            snip = None
            if action in ("dictate", "clipboard"):
                snip = textproc.match_snippet(raw, CFG.get("snippets", []))
            if snip is not None:
                text = snip.get("text", "")
                cleaned = False
                log.info("snippet '%s' (%d chars)", snip.get("trigger"), len(text))
            else:
                text = textproc.process(raw, opts, CFG.get("replacements", []),
                                        CFG.get("voice_commands_list"))
                cleaned = False
            if snip is not None:
                pass
            elif action == "english":
                try:
                    text = llm_translate(text)
                    log.info("traduzido para ingles via LLM: %s", text[:80])
                except Exception as exc:
                    log.warning("traducao LLM falhou: %s - usando texto original", exc)
            elif action in ("dictate", "clipboard"):
                novo = auto_clean(text, prof)
                if novo is not None:
                    cleaned = novo != text
                    text = novo
                    # comando de voz/capitalizacao de novo: o LLM pode ter
                    # mexido na 1a letra ou desfeito "nova linha"
                    if opts.get("capitalize", True):
                        text = textproc.capitalize_first(text)
                    if opts.get("strip_trailing_period"):
                        text = textproc.strip_final_period(text)
            log.info("%.2fs de audio em %.2fs (%.1fx) | asr+rede %.2fs | llm %s | %d chars | app=%s",
                     res.get("duration", 0), res.get("elapsed", 0),
                     res.get("speed", 0), t_asr,
                     "limpou" if cleaned else "igual/pulado", len(text), exe or "?")

            history_add({"at": time.time(), "text": text, "raw": raw,
                         "app": exe, "action": action, "llm": cleaned,
                         "snippet": snip.get("trigger") if snip else None, "send": send,
                         "duration": res.get("duration", 0),
                         "elapsed": res.get("elapsed", 0)})

            if action == "clipboard":
                if clipboard_set(text):
                    _last.update({"text": text, "hwnd": hwnd, "at": time.time(), "raw": raw})
                    log.info("copiado para o clipboard (nao colado)")
                    beep("ok")
                    self._ov_done("hide", 250)
                else:
                    beep("erro")
                    self._ov_done("error")
                    self._ov_done("hide", 1500)
                return

            out = (" " + text) if (snip is None and _needs_leading_space(text, hwnd)) else text
            if paste(out, hwnd):
                _last.update({"text": out, "hwnd": hwnd, "at": time.time(),
                              "raw": raw if snip is None else "", "cleaned": cleaned})
                if send:
                    self._press_enter(hwnd)
                log.info("total ate injetar: %.2fs%s | %s", time.time() - t0,
                         " +Enter" if send else "", text[:80])
                beep("ok")
                self._ov_done("hide", 250)
            else:
                beep("erro")
                self._ov_done("error")
                self._ov_done("hide", 1500)
        except Exception:
            log.error("falha no ditado: %s", traceback.format_exc())
            beep("erro")
            self._ov_done("error")
            self._ov_done("hide", 1500)

    # ---- Command Mode ----------------------------------------------------
    def _command_finish(self, data, rate, lang, hwnd, exe, t0, sel=None):
        """
        Selecionou texto, apertou o atalho, falou a instrucao. O texto
        selecionado foi copiado no _down (antes de gravar), entao aqui so
        junta instrucao + selecao, manda pro LLM e cola por cima.
        Sem selecao, a instrucao vira um pedido de texto novo ("escreve uma
        mensagem dizendo que o pedido saiu hoje").
        """
        sel = sel or ""
        res = transcribe(to_wav_bytes(data, rate), lang, "")
        instr = (res.get("text") or "").strip()
        if not instr:
            log.info("command: instrucao vazia")
            beep("erro")
            self._ov_done("error")
            self._ov_done("hide", 1200)
            return
        log.info("command: %r sobre %d chars selecionados", instr[:80], len(sel))
        try:
            novo = llm_command(instr, sel)
        except Exception as exc:
            log.error("command: LLM falhou: %s", exc)
            beep("erro")
            self._ov_done("error")
            self._ov_done("hide", 1500)
            return
        if not novo:
            beep("erro")
            self._ov_done("error")
            self._ov_done("hide", 1500)
            return
        # Foco mudou durante o LLM: nao cola em janela errada, deixa no clipboard.
        cur, _ = foreground_app()
        if hwnd is not None and cur != hwnd:
            clipboard_set(novo)
            log.warning("command: janela mudou; resultado ficou no clipboard")
            beep("cancel")
            self._ov_done("hide")
            return
        history_add({"at": time.time(), "text": novo, "raw": instr, "app": exe,
                     "action": "command", "selection": sel[:2000],
                     "duration": res.get("duration", 0), "elapsed": res.get("elapsed", 0)})
        # Com selecao ativa, colar substitui o trecho selecionado.
        if paste(novo, hwnd):
            _last.update({"text": novo, "hwnd": hwnd, "at": time.time(), "raw": instr})
            log.info("command: colado em %.2fs | %s", time.time() - t0, novo[:80])
            beep("ok")
            self._ov_done("hide", 250)
        else:
            beep("erro")
            self._ov_done("error")
            self._ov_done("hide", 1500)

    def _transform(self, idx):
        """Ctrl+Alt+1/2/3: seleciona texto, aperta, e ele e reescrito no lugar
        pela instrucao fixa do transform (sem falar nada)."""
        tr = (CFG.get("transforms", []) or [])
        if idx >= len(tr) or not tr[idx].get("prompt"):
            beep("erro")
            return
        tr = tr[idx]
        hwnd, exe = foreground_app()
        sel = grab_selection()
        if not sel.strip():
            log.info("transform '%s': nada selecionado", tr.get("name"))
            beep("erro")
            return
        self._ov("transcribing")
        t0 = time.time()
        try:
            novo = llm_transform(tr["prompt"], sel)
        except Exception as exc:
            log.error("transform '%s' falhou: %s", tr.get("name"), exc)
            beep("erro")
            self._ov_done("error")
            self._ov_done("hide", 1500)
            return
        if not novo:
            beep("erro")
            self._ov_done("hide")
            return
        # Mesma trava da limpeza: valor ou conta que nao estava no texto = rejeita.
        inv = invented_phrase(sel, novo)
        if inv or (tr.get("guard_numbers", True) and not numbers_preserved(sel, novo)):
            log.warning("transform '%s' rejeitado (%s) | %r", tr.get("name"),
                        ("inventou %r" % inv) if inv else "numero novo", novo[:120])
            beep("erro")
            self._ov_done("error")
            self._ov_done("hide", 1500)
            return
        cur, _ = foreground_app()
        if hwnd is not None and cur != hwnd:
            clipboard_set(novo)
            log.warning("transform: janela mudou; resultado ficou no clipboard")
            beep("cancel")
            self._ov_done("hide")
            return
        history_add({"at": time.time(), "text": novo, "raw": sel[:4000], "app": exe,
                     "action": "transform:%s" % tr.get("name"), "duration": 0, "elapsed": 0})
        if paste(novo, hwnd):
            _last.update({"text": novo, "hwnd": hwnd, "at": time.time(), "raw": sel})
            log.info("transform '%s' em %.2fs: %s", tr.get("name"), time.time() - t0, novo[:80])
            beep("ok")
            self._ov_done("hide", 250)
        else:
            beep("erro")
            self._ov_done("error")
            self._ov_done("hide", 1500)

    def _learn_word(self):
        """Seleciona a palavra/nome certo, aperta, e entra no vocabulario do
        Whisper (transcription.vocabulary). Vale ja no proximo ditado."""
        sel = (grab_selection() or "").strip().strip(".,;:!?\"'()")
        if not sel or len(sel) > 60 or "\n" in sel or len(sel.split()) > 4:
            log.info("dicionario: selecao invalida %r", sel[:80])
            beep("erro")
            return
        data = json.loads(json.dumps(CFG.data))
        voc = data.setdefault("transcription", {}).setdefault("vocabulary", [])
        if any(v.lower() == sel.lower() for v in voc):
            log.info("dicionario: '%s' ja existe", sel)
            beep("ok")
            return
        voc.append(sel)
        CFG.save(data)
        log.info("dicionario: '%s' adicionado (%d termos)", sel, len(voc))
        beep("ok")

    # ---- acoes sobre o ultimo texto -------------------------------------
    def _press_enter(self, hwnd):
        """Enter depois da colagem. Confere a janela: se o foco mudou, nao
        envia nada (Enter na janela errada pode disparar outra coisa)."""
        if NO_INJECT:
            log.info("[NO_INJECT] Enter seria enviado")
            return
        time.sleep(0.25)  # o app termina de processar o Ctrl+V antes
        cur, _ = foreground_app()
        if hwnd is not None and cur != hwnd:
            log.warning("envio cancelado: a janela mudou")
            return
        wait_mods_released()
        kb = keyboard.Controller()
        kb.press(keyboard.Key.enter)
        kb.release(keyboard.Key.enter)
        log.info("Enter enviado")

    def _revert_raw(self):
        """Troca o ultimo texto limpo pelo que voce falou de verdade (bruto,
        so com pontuacao do Whisper). Garantia contra limpeza que mexeu demais."""
        raw = _last.get("raw") or ""
        if not _last["text"] or not raw:
            beep("erro")
            return
        hwnd, _exe = foreground_app()
        if _last["hwnd"] is not None and hwnd != _last["hwnd"]:
            clipboard_set(raw)
            log.warning("reverter: janela mudou; texto bruto ficou no clipboard")
            beep("cancel")
            return
        send_undo()
        time.sleep(0.15)
        bruto = textproc.capitalize_first(raw)
        if _last["text"].startswith(" "):
            bruto = " " + bruto
        if paste(bruto, hwnd):
            _last.update({"text": bruto, "at": time.time(), "cleaned": False})
            log.info("revertido para o bruto: %s", bruto[:80])
            beep("ok")
        else:
            beep("erro")

    def _undo(self):
        if not _last["text"]:
            beep("erro")
            return
        # Ctrl+Z do proprio aplicativo. Em alguns apps o undo pode desfazer mais
        # que a colagem; nao existe jeito confiavel de limitar isso de fora.
        send_undo()
        log.info("undo enviado")
        beep("cancel")

    def _reinject(self):
        if not _last["text"]:
            beep("erro")
            return
        hwnd, _exe = foreground_app()
        if paste(_last["text"], hwnd):
            _last.update({"hwnd": hwnd, "at": time.time()})
            log.info("reinjetado: %s", _last["text"][:60])
            beep("ok")
        else:
            beep("erro")

    def _polish(self):
        if not _last["text"]:
            beep("erro")
            return
        alvo = _last["hwnd"]
        self._ov("transcribing")
        try:
            t0 = time.time()
            novo = llm_polish(_last["text"].strip())
            if not novo:
                raise RuntimeError("LLM devolveu vazio")
            log.info("polido em %.2fs: %s", time.time() - t0, novo[:80])

            # O LLM leva ~2s, tempo de sobra para o usuario clicar em outro
            # lugar. Mandar Ctrl+Z e colar na janela errada seria destrutivo,
            # entao aqui confirma que o foco nao mudou; se mudou, entrega pelo
            # clipboard e nao encosta em nada.
            hwnd, _exe = foreground_app()
            if alvo is not None and hwnd != alvo:
                clipboard_set(novo)
                log.warning("janela mudou durante o polimento; texto no clipboard")
                beep("cancel")
                self._ov("hide")
                return

            send_undo()
            time.sleep(0.15)
            if paste(novo, hwnd):
                _last.update({"text": novo, "hwnd": hwnd, "at": time.time()})
                beep("ok")
                self._ov("hide", 250)
                return
            raise RuntimeError("falha ao colar o texto polido")
        except Exception as exc:
            log.error("polimento falhou: %s", exc)
            beep("erro")
            self._ov("error")
            self._ov("hide", 1500)


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
def selftest():
    ok = True
    print("=" * 64)
    print("[1] config")
    CFG.ensure_file()
    print("    arquivo: %s" % CFG.path)
    for name in ("dictate", "clipboard", "english", "undo", "reinject", "polish", "cancel"):
        spec = CFG.get("hotkeys.%s" % name, {})
        print("    %-10s %-22s %s" % (name, describe(spec),
              "ativo" if spec.get("enabled", True) else "desligado"))

    print("\n[2] microfone: abrindo de verdade e gravando 2s")
    eng = AudioEngine()
    if not eng.open():
        print("    FALHOU (veja ditado.log)")
        ok = False
    else:
        print("    %s" % eng.desc)
        eng.begin()
        time.sleep(2.0)
        data = eng.end()
        if data is None or not len(data):
            print("    FALHOU: nada capturado")
            ok = False
        else:
            rms = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
            print("    RMS=%.1f pico=%d -> %s" % (
                rms, int(np.max(np.abs(data))),
                "captando" if int(np.max(np.abs(data))) >= 30 else "SILENCIO?"))
        eng.close()

    print("\n[3] transcricao")
    ref = os.path.join(HERE, "assets", "warmup.wav")
    try:
        if os.path.exists(ref):
            with open(ref, "rb") as f:
                res = transcribe(f.read(), "pt")
            print("    %.2fs em %.2fs (%.1fx)" % (res.get("duration", 0),
                  res.get("elapsed", 0), res.get("speed", 0)))
            print("    %s" % (res.get("text") or "")[:100])
        else:
            print("    assets/warmup.wav ausente (rode tools/make_warmup.py)")
    except Exception as exc:
        print("    FALHOU: %s" % exc)
        ok = False

    print("\n[4] processamento de texto")
    amostra = "teste de vocabulario nova linha segunda frase"
    saida = textproc.process(amostra, CFG.get("text", {}),
                             CFG.get("replacements", []), None)
    print("    entrada: %r" % amostra)
    print("    saida:   %r" % saida)
    if "\n" not in saida:
        print("    ATENCAO: 'nova linha' nao virou quebra")

    print("\n[5] clipboard e janela em foco")
    try:
        orig = clipboard_get()
        clipboard_set("ditado-selftest-ção")
        print("    round-trip: %s" % ("OK" if clipboard_get() == "ditado-selftest-ção" else "FALHOU"))
        clipboard_set(orig if orig is not None else "")
    except Exception as exc:
        print("    FALHOU: %s" % exc)
        ok = False
    _h, exe = foreground_app()
    print("    app em foco: %s" % (exe or "desconhecido"))
    prof = textproc.profile_for(exe, CFG.get("profiles", []))
    print("    perfil casado: %s" % (prof.get("match") if prof else "nenhum"))

    print("\n" + "=" * 64)
    print("RESULTADO: %s" % ("tudo ok" if ok else "tem falha acima"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    CFG.ensure_file()
    audio = AudioEngine()

    ov = None
    if CFG.get("overlay.enabled", True):
        try:
            from overlay import Overlay
            ov = Overlay(lambda: audio.level)
        except Exception:
            log.error("overlay indisponivel: %s", traceback.format_exc())

    # Captura da saida do sistema (o outro lado da ligacao). Se nao houver
    # caminho disponivel, o ditado segue funcionando sem essas duas teclas.
    sysengine = sysaudio.SystemEngine()
    if sysengine.probe():
        log.info("captura de sistema: %s", sysengine.desc)
    else:
        log.warning("captura de sistema indisponivel; Ctrl+Alt+S e +D nao vao funcionar")
        sysengine = None

    worker = Worker(audio, ov, sysengine)
    worker.start()
    threading.Thread(target=audio.idle_watch, daemon=True).start()

    def watch_config():
        while True:
            time.sleep(2)
            try:
                if CFG.reload_if_changed():
                    log.info("config recarregada do disco")
            except Exception:
                pass
    threading.Thread(target=watch_config, daemon=True).start()

    if not audio.open():
        log.error("mic nao abriu agora; tenta de novo no 1o uso")

    def heartbeat():
        """Anuncia-se ao servidor a cada 5s: e assim que o painel sabe que o
        ditado esta vivo, sem precisar varrer processos. Sobe DEPOIS do
        audio.open() para nao anunciar 'mic: nenhum' no primeiro batimento."""
        while True:
            try:
                srv = CFG.get("transcription.server", "http://127.0.0.1:9978").rstrip("/")
                body = json.dumps({
                    "mic": audio.desc,
                    "hotkeys": "%s ditar | %s clipboard | %s ingles" % (
                        describe(CFG.get("hotkeys.dictate")),
                        describe(CFG.get("hotkeys.clipboard")),
                        describe(CFG.get("hotkeys.english"))),
                }).encode("utf-8")
                req = urllib.request.Request(
                    srv + "/api/client/heartbeat", data=body,
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=3).close()
            except Exception:
                pass  # servidor fora e situacao normal, nao vale poluir o log
            time.sleep(5)
    threading.Thread(target=heartbeat, daemon=True).start()

    try:
        server = CFG.get("transcription.server", "http://127.0.0.1:9978").rstrip("/")
        with urllib.request.urlopen(server + "/health", timeout=4) as r:
            h = json.loads(r.read().decode())
        log.info("servidor ok: %s (%s)", h.get("model"), h.get("device"))
    except Exception:
        log.warning("servidor nao respondeu. Ligue pelo atalho da area de trabalho.")

    # Bandeja: pausar/retomar, abrir dashboard, sair.
    tray_state = None
    try:
        import tray as tray_mod
        tray_state = tray_mod.TrayState()
        srv = CFG.get("transcription.server", "http://127.0.0.1:9978").rstrip("/")
        tray_mod.start(
            tray_state, srv,
            on_quit=lambda: os._exit(0),
            describe_fn=lambda: "%s ditar  ·  %s copiar" % (
                describe(CFG.get("hotkeys.dictate")),
                describe(CFG.get("hotkeys.clipboard"))))
    except Exception:
        log.warning("bandeja nao subiu: %s", traceback.format_exc())

    state = {"ctrl": False, "alt": False, "shift": False}
    held = {"action": None}
    VK_CTRL = (0xA2, 0xA3, 0x11)
    # 0xA5 (RMENU/AltGr) DE PROPOSITO fora da lista: em teclados ABNT2 o AltGr
    # e entregue pelo Windows como Ctrl esquerdo + Alt direito. Com ele aqui,
    # toda a camada de acentuacao (AltGr+W, AltGr+E, AltGr+Space...) casava com
    # os atalhos Ctrl+Alt+X e disparava o ditado durante a digitacao normal.
    VK_ALT = (0xA4, 0x12)
    VK_SHIFT = (0xA0, 0xA1, 0x10)
    VK_ESC = 0x1B

    # Atalhos que gravam audio (hold/toggle) e os que agem na hora.
    REC_ACTIONS = ("dictate", "clipboard", "english", "system", "both", "command")
    ONESHOT = ("undo", "reinject", "polish", "revert", "learn",
               "transform1", "transform2", "transform3")

    def match(spec):
        """Casamento exato dos modificadores: assim Ctrl+Alt+Space e
        Ctrl+Alt+Shift+Space nunca sao confundidos entre si."""
        return (spec.get("enabled", True)
                and bool(spec.get("ctrl")) == state["ctrl"]
                and bool(spec.get("alt")) == state["alt"]
                and bool(spec.get("shift")) == state["shift"])

    def filt(msg, data):
        # Rapido e sem deixar excecao propria subir. A supressao do pynput
        # funciona lancando excecao de dentro do filtro, entao a chamada fica
        # FORA do try - se caisse dentro, seria engolida e a tecla vazaria.
        suppress = False
        try:
            vk = data.vkCode
            down = msg in (0x0100, 0x0104)
            up = msg in (0x0101, 0x0105)

            if vk in VK_CTRL:
                state["ctrl"] = down
            elif vk in VK_ALT:
                state["alt"] = down
            elif vk in VK_SHIFT:
                state["shift"] = down
            elif tray_state is not None and tray_state.paused:
                # Pausado pela bandeja: nao age e nao suprime, entao as teclas
                # voltam a ser normais no sistema inteiro.
                pass
            else:
                now = time.time()
                # Esc so e interceptado durante a gravacao: fora dela, seria
                # inaceitavel roubar o Esc do sistema inteiro.
                if vk == VK_ESC and down and worker.state in (ST_HOLDING, ST_TOGGLE):
                    worker.post("cancel", now)
                    suppress = True
                elif down and held["action"] is None:
                    for name in REC_ACTIONS:
                        spec = CFG.get("hotkeys.%s" % name, {})
                        if vk == vk_of(spec) and match(spec):
                            held["action"] = name
                            worker.post("down", now, name)
                            suppress = True
                            break
                    else:
                        for name in ONESHOT:
                            spec = CFG.get("hotkeys.%s" % name, {})
                            if vk == vk_of(spec) and match(spec):
                                worker.post(name, now)
                                suppress = True
                                break
                elif up and held["action"] is not None:
                    spec = CFG.get("hotkeys.%s" % held["action"], {})
                    if vk == vk_of(spec):
                        held["action"] = None
                        worker.post("up", now)
                        suppress = True
        except Exception:
            log.error("filtro do hook: %s", traceback.format_exc())

        if suppress:
            listener.suppress_event()

    listener = keyboard.Listener(win32_event_filter=filt, suppress=False)
    listener.start()
    log.info("ditado ativo | %s=ditar %s=clipboard %s=ingles | mic: %s | overlay: %s | bandeja: %s",
             describe(CFG.get("hotkeys.dictate")),
             describe(CFG.get("hotkeys.clipboard")),
             describe(CFG.get("hotkeys.english")),
             audio.desc, "sim" if ov else "nao",
             "sim" if tray_state is not None else "nao")

    if ov is not None:
        try:
            ov.build()
            ov.root.mainloop()
            return
        except Exception:
            log.error("mainloop do overlay caiu: %s", traceback.format_exc())
            worker.ov = None
    listener.join()


if __name__ == "__main__":
    main()
