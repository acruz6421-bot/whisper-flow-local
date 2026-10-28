"""
Overlay de gravacao: barrinha no rodape do monitor, no estilo do Wispr Flow.

Mostra o nivel do audio captado em tempo real enquanto grava, muda para o estado
de transcricao e desaparece sozinha.

Duas exigencias tecnicas que definem o desenho:

1. A janela NAO pode receber foco. Se receber, o Ctrl+V da injecao cola no
   overlay em vez do aplicativo do usuario. Por isso ela leva WS_EX_NOACTIVATE
   (nunca ativa) e WS_EX_TRANSPARENT (clique passa direto), aplicados via Win32
   depois que o Tk cria o HWND. WS_EX_TOOLWINDOW mantem ela fora do Alt+Tab.

2. Tkinter so pode ser tocado pela thread principal. Entao nada aqui e chamado
   direto pelas outras threads: elas empilham comandos numa fila que o proprio
   laco do Tk drena.

Usa apenas a stdlib: nada de Qt nem dependencia nova.
"""

import queue
import ctypes
import logging
import tkinter as tk
from ctypes import wintypes

log = logging.getLogger("ditado")

# Geometria e cores
W, H = 340, 54
MARGIN_BOTTOM = 64
BARS = 26
BAR_W = 4
BAR_GAP = 3
RADIUS = 16

BG = "#14181c"
CHROMA = "#ff00ff"   # cor que o Windows torna transparente (cantos arredondados)
FG_REC = "#25d366"   # verde, gravando
FG_BUSY = "#3aa0ff"  # azul, transcrevendo
FG_ERR = "#e5484d"
TXT = "#e9edef"
TXT_DIM = "#8696a0"

u32 = ctypes.WinDLL("user32", use_last_error=True)
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000

u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
u32.GetWindowLongW.restype = ctypes.c_long
u32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
u32.SetWindowLongW.restype = ctypes.c_long
u32.GetParent.argtypes = [wintypes.HWND]
u32.GetParent.restype = wintypes.HWND


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """Tkinter nao tem retangulo arredondado; monta com arcos e retangulos."""
    canvas.create_rectangle(x1 + r, y1, x2 - r, y2, outline="", **kw)
    canvas.create_rectangle(x1, y1 + r, x2, y2 - r, outline="", **kw)
    for cx, cy, start in ((x1, y1, 90), (x2 - 2 * r, y1, 0),
                          (x1, y2 - 2 * r, 180), (x2 - 2 * r, y2 - 2 * r, 270)):
        canvas.create_arc(cx, cy, cx + 2 * r, cy + 2 * r,
                          start=start, extent=90, style="pieslice",
                          outline="", **kw)


class Overlay:
    def __init__(self, level_fn):
        """level_fn: callable que devolve o RMS instantaneo do audio (0..~8000)."""
        self.level_fn = level_fn
        self.cmds = queue.Queue()
        self.state = "hidden"
        self.history = [0.0] * BARS
        self.scale = 800.0        # normalizacao adaptativa
        self.phase = 0
        self.root = None
        self.canvas = None
        self._styled = False

    # ---- API para as outras threads (thread-safe: so empilha) -------------
    def recording(self):
        self.cmds.put(("state", "rec"))

    def transcribing(self):
        self.cmds.put(("state", "busy"))

    def error(self):
        self.cmds.put(("state", "err"))

    def hide(self, delay_ms=0):
        self.cmds.put(("hide", delay_ms))

    # ---- Construcao ------------------------------------------------------
    def build(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", CHROMA)
        except tk.TclError:
            pass  # sem transparencia: fica retangular, mas funciona
        self.root.configure(bg=CHROMA)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - W) // 2
        y = sh - H - MARGIN_BOTTOM
        self.root.geometry("%dx%d+%d+%d" % (W, H, x, y))

        self.canvas = tk.Canvas(self.root, width=W, height=H, bg=CHROMA,
                                highlightthickness=0, bd=0)
        self.canvas.pack()

        self.root.withdraw()
        self.root.after(16, self._tick)
        return self.root

    def _apply_no_focus(self):
        """
        Precisa rodar depois que a janela existe de verdade. Sem isto o overlay
        pode receber foco e a colagem do texto vai para ele em vez do app.
        """
        if self._styled:
            return
        try:
            hwnd = u32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ex |= (WS_EX_NOACTIVATE | WS_EX_TRANSPARENT
                   | WS_EX_TOOLWINDOW | WS_EX_LAYERED)
            u32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
            self._styled = True
            log.info("overlay: estilo sem-foco aplicado (hwnd=%s)", hwnd)
        except Exception as exc:
            log.warning("overlay: nao consegui aplicar estilo sem-foco: %s", exc)

    # ---- Laco de desenho -------------------------------------------------
    def _tick(self):
        try:
            self._drain()
            if self.state != "hidden":
                self._draw()
        except Exception:
            log.exception("overlay: erro no tick")
        self.root.after(33, self._tick)  # ~30 fps

    def _drain(self):
        while True:
            try:
                kind, val = self.cmds.get_nowait()
            except queue.Empty:
                return
            if kind == "state":
                self.state = val
                if val == "rec":
                    self.history = [0.0] * BARS
                    self.scale = 800.0
                self.root.deiconify()
                self.root.attributes("-topmost", True)
                self._apply_no_focus()
            elif kind == "hide":
                if val:
                    self.root.after(val, self._do_hide)
                else:
                    self._do_hide()

    def _do_hide(self):
        self.state = "hidden"
        try:
            self.root.withdraw()
        except Exception:
            pass

    def _draw(self):
        c = self.canvas
        c.delete("all")
        _round_rect(c, 0, 0, W, H, RADIUS, fill=BG)

        if self.state == "rec":
            color, label = FG_REC, "ouvindo"
            lvl = 0.0
            try:
                lvl = float(self.level_fn() or 0.0)
            except Exception:
                lvl = 0.0
            # Escala adaptativa com decaimento: a barra reage bem tanto para
            # fala baixa quanto alta, sem precisar calibrar ganho na mao.
            self.scale = max(600.0, self.scale * 0.96, lvl * 1.15)
            self.history.append(min(1.0, lvl / self.scale))
            self.history = self.history[-BARS:]
            values = self.history
        elif self.state == "busy":
            color, label = FG_BUSY, "transcrevendo"
            self.phase += 1
            # Onda viajando, so para indicar que esta trabalhando.
            values = [0.25 + 0.55 * abs(((i * 0.5 - self.phase * 0.35) % 6) / 6.0 - 0.5) * 2
                      for i in range(BARS)]
        else:
            color, label = FG_ERR, "falhou"
            values = [0.18] * BARS

        # Texto a esquerda
        c.create_text(16, H // 2, anchor="w", text=label,
                      fill=TXT if self.state == "rec" else TXT_DIM,
                      font=("Segoe UI", 9, "bold"))

        # Barras a direita
        total = BARS * BAR_W + (BARS - 1) * BAR_GAP
        x0 = W - 16 - total
        mid = H / 2.0
        for i, v in enumerate(values):
            h = max(2.0, v * (H - 22))
            x = x0 + i * (BAR_W + BAR_GAP)
            c.create_rectangle(x, mid - h / 2, x + BAR_W, mid + h / 2,
                               fill=color, outline="")
