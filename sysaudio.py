"""
Captura do audio que SAI da maquina (loopback), para transcrever o outro lado
de uma ligacao ou reuniao.

Por que a biblioteca 'soundcard' e nao o sounddevice, que ja e usado no mic:
o WasapiSettings do sounddevice 0.5.5 nao tem parametro de loopback - a
assinatura e (exclusive, auto_convert, explicit_sample_format). Em teste, o
soundcard captou com nivel maior que o Stereo Mix,
nao depende de o Stereo Mix estar habilitado no Windows, e nao passa pelo
WDM-KS, que ja deu 'Unanticipated host error' aqui.

O Stereo Mix fica como fallback, via sounddevice, para o caso de a saida padrao
nao aceitar loopback.

A API imita a do AudioEngine do ditado.py (begin/end/discard/level) de proposito,
para a trabalhadora tratar as duas fontes do mesmo jeito.
"""

import time
import ctypes
import logging
import threading

import numpy as np

log = logging.getLogger("ditado")

RATE = 48000  # pedido explicitamente para casar com o mic e evitar reamostragem


class SystemEngine:
    def __init__(self):
        self.rate = RATE
        self.desc = "nenhum"
        self.recording = False
        self.level = 0.0
        self._buf = []
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._mode = None  # "soundcard" | "stereomix"

    # ---- descoberta ------------------------------------------------------
    def probe(self):
        """Descobre qual caminho existe, sem gravar. Devolve descricao ou None."""
        try:
            import soundcard as sc
            spk = sc.default_speaker()
            sc.get_microphone(str(spk.name), include_loopback=True)
            self._mode = "soundcard"
            self.desc = "loopback WASAPI: %s" % spk.name
            return self.desc
        except Exception as exc:
            log.info("loopback por soundcard indisponivel: %s", str(exc)[:120])

        try:
            import sounddevice as sd
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) < 1:
                    continue
                n = d["name"].lower()
                if "mixagem" in n or "stereo mix" in n or "what u hear" in n:
                    self._mode = "stereomix"
                    self._sm_index = i
                    self._sm_rate = int(d.get("default_samplerate") or 44100)
                    self.rate = self._sm_rate
                    self.desc = "stereo mix: [%d] %s" % (i, d["name"])
                    return self.desc
        except Exception as exc:
            log.info("stereo mix indisponivel: %s", str(exc)[:120])

        self._mode = None
        self.desc = "nenhum"
        return None

    # ---- gravacao --------------------------------------------------------
    def _run_soundcard(self):
        import soundcard as sc
        # O soundcard fala com o WASAPI por COM, e COM se inicializa POR THREAD.
        # Sem isto, qualquer chamada aqui morre com 0x800401f0
        # (CO_E_NOTINITIALIZED). No teste manual passava porque rodava na thread
        # principal, onde algo ja havia inicializado.
        co = None
        try:
            co = ctypes.windll.ole32
            hr = co.CoInitializeEx(None, 0x0)  # MTA
            if hr == -2147417850:              # RPC_E_CHANGED_MODE
                co.CoInitializeEx(None, 0x2)   # cai para STA
        except Exception as exc:
            log.warning("CoInitializeEx falhou: %s", exc)
            co = None
        try:
            spk = sc.default_speaker()
            mic = sc.get_microphone(str(spk.name), include_loopback=True)
            with mic.recorder(samplerate=self.rate, channels=2) as rec:
                bloco = self.rate // 10  # 100 ms
                while not self._stop.is_set():
                    data = rec.record(numframes=bloco)
                    if data is None or not len(data):
                        continue
                    mono = data.mean(axis=1) if data.ndim > 1 else data
                    pcm = np.clip(mono * 32767.0, -32768, 32767).astype(np.int16)
                    with self._lock:
                        self._buf.append(pcm)
                    try:
                        self.level = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
                    except Exception:
                        pass
        except Exception:
            log.exception("captura de sistema (soundcard) caiu")
        finally:
            if co is not None:
                try:
                    co.CoUninitialize()
                except Exception:
                    pass

    def _run_stereomix(self):
        import sounddevice as sd
        try:
            frames = []

            def cb(indata, n, t, status):
                if self._stop.is_set():
                    return
                a = indata.copy()
                if a.ndim > 1 and a.shape[1] > 1:
                    a = a.astype(np.int32).mean(axis=1).astype(np.int16)
                else:
                    a = a.reshape(-1)
                with self._lock:
                    self._buf.append(a)
                try:
                    self.level = float(np.sqrt(np.mean(a.astype(np.float32) ** 2)))
                except Exception:
                    pass

            with sd.InputStream(samplerate=self._sm_rate, channels=2, dtype="int16",
                                device=self._sm_index, callback=cb):
                while not self._stop.is_set():
                    time.sleep(0.05)
        except Exception:
            log.exception("captura de sistema (stereo mix) caiu")

    def begin(self):
        with self._lock:
            if self.recording:
                return True
            if self._mode is None and not self.probe():
                log.error("nenhum caminho de captura de sistema disponivel")
                return False
            self._buf = []
            self._stop.clear()
            self.recording = True
        target = self._run_soundcard if self._mode == "soundcard" else self._run_stereomix
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()
        log.info("captura de sistema iniciada (%s)", self.desc)
        return True

    def _halt(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None
        with self._lock:
            self.recording = False

    def end(self):
        if not self.recording:
            return None
        self._halt()
        with self._lock:
            frames, self._buf = self._buf, []
        if not frames:
            return None
        return np.concatenate(frames, axis=0)

    def discard(self):
        if self.recording:
            self._halt()
        with self._lock:
            self._buf = []


def resample(pcm, de, para):
    """Reamostragem linear. Basta para fala; nao e filtro de qualidade."""
    if de == para or pcm is None or not len(pcm):
        return pcm
    n = int(round(len(pcm) * (float(para) / float(de))))
    if n <= 0:
        return pcm[:0]
    x = np.linspace(0.0, 1.0, num=len(pcm), endpoint=False)
    xi = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(xi, x, pcm.astype(np.float32)).astype(np.int16)


def mix(a, rate_a, b, rate_b):
    """
    Soma microfone e saida do sistema numa faixa mono.

    Cada lado entra com ganho 0.6 em vez de 0.5 somado: 0.5 deixa a conversa
    baixa demais para o VAD, e 1.0 nos dois estoura quando as duas pontas falam
    ao mesmo tempo. 0.6 com clip cobre os dois casos.
    """
    if a is None and b is None:
        return None, rate_a or rate_b
    if a is None:
        return b, rate_b
    if b is None:
        return a, rate_a

    # Achata antes de qualquer coisa: um lado em (N,1) e o outro em (N,) faria o
    # numpy fazer broadcast para (N,N) e tentar alocar centenas de GiB.
    a = np.asarray(a).reshape(-1)
    b = np.asarray(b).reshape(-1)

    alvo = max(rate_a, rate_b)
    a = resample(a, rate_a, alvo)
    b = resample(b, rate_b, alvo)
    n = max(len(a), len(b))
    if len(a) < n:
        a = np.pad(a, (0, n - len(a)))
    if len(b) < n:
        b = np.pad(b, (0, n - len(b)))
    som = a.astype(np.float32) * 0.6 + b.astype(np.float32) * 0.6
    return np.clip(som, -32768, 32767).astype(np.int16), alvo
