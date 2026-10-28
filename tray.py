"""
Icone na bandeja: pausar/retomar o ditado, abrir o dashboard, sair.

Roda em thread propria (pystray run_detached no Windows) porque a thread
principal ja esta ocupada com o mainloop do Tkinter do overlay.

O estado de pausa vive num objeto compartilhado e nao aqui: o hook de teclado
consulta esse objeto a cada evento, entao pausar tem efeito imediato sem
desmontar nem remontar o hook.
"""

import os
import logging
import threading
import webbrowser

log = logging.getLogger("ditado")

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "whisper.ico")


class TrayState:
    """Compartilhado com o hook de teclado."""

    def __init__(self):
        self.paused = False
        self.lock = threading.Lock()

    def toggle(self):
        with self.lock:
            self.paused = not self.paused
            return self.paused


def _icon_image(paused):
    from PIL import Image, ImageDraw
    size = 64
    try:
        if os.path.exists(ICON) and not paused:
            img = Image.open(ICON).convert("RGBA").resize((size, size))
            return img
    except Exception:
        pass
    # Fallback desenhado: circulo verde ativo, cinza com pausa.
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cor = (110, 120, 130, 255) if paused else (37, 211, 102, 255)
    d.ellipse((6, 6, size - 6, size - 6), fill=cor)
    if paused:
        d.rectangle((24, 20, 30, 44), fill=(255, 255, 255, 255))
        d.rectangle((36, 20, 42, 44), fill=(255, 255, 255, 255))
    else:
        d.ellipse((26, 18, 38, 36), fill=(255, 255, 255, 255))
        d.rectangle((30, 36, 34, 44), fill=(255, 255, 255, 255))
        d.rectangle((24, 44, 40, 48), fill=(255, 255, 255, 255))
    return img


def start(state, server_url, on_quit=None, describe_fn=None):
    """Sobe o icone. Devolve o objeto do icone, ou None se nao der."""
    try:
        import pystray
        from pystray import MenuItem as Item, Menu
    except Exception as exc:
        log.warning("bandeja indisponivel (%s); seguindo sem ela", exc)
        return None

    icon = None

    def refresh():
        try:
            icon.icon = _icon_image(state.paused)
            icon.title = "Ditado %s" % ("pausado" if state.paused else "ativo")
            icon.update_menu()
        except Exception:
            pass

    def do_toggle(_i=None, _it=None):
        paused = state.toggle()
        log.info("ditado %s pela bandeja", "pausado" if paused else "retomado")
        refresh()

    def do_dashboard(_i=None, _it=None):
        try:
            webbrowser.open(server_url)
        except Exception:
            log.warning("nao consegui abrir o navegador")

    def do_quit(_i=None, _it=None):
        log.info("saindo pela bandeja")
        try:
            icon.stop()
        except Exception:
            pass
        if on_quit:
            on_quit()

    menu = Menu(
        Item(lambda _i: "Pausar ditado" if not state.paused else "Retomar ditado",
             do_toggle, default=True),
        Item(lambda _i: describe_fn() if describe_fn else "", None, enabled=False),
        Menu.SEPARATOR,
        Item("Abrir dashboard", do_dashboard),
        Menu.SEPARATOR,
        Item("Sair", do_quit),
    )

    icon = pystray.Icon("ditado", _icon_image(False), "Ditado ativo", menu)
    try:
        icon.run_detached()
        log.info("bandeja ativa")
        return icon
    except Exception as exc:
        log.warning("nao consegui subir a bandeja: %s", exc)
        return None
