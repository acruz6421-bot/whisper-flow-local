"""
Silencia a saida de audio do PC enquanto voce dita.

Serve para duas coisas: nao te distrair, e principalmente evitar que o som do
computador vaze pelo microfone e entre na transcricao.

Tres decisoes que importam:

  - Restaura o ESTADO ANTERIOR, nao "desmuta". Se voce ja estava no mudo antes
    de ditar, continua mudo depois. Um unmute cego mudaria a sua configuracao.
  - Nunca deve ser aplicado nas capturas de sistema (Ctrl+Alt+S e Ctrl+Alt+D):
    silenciar a saida ali mataria exatamente o audio que se quer capturar. Quem
    decide e o chamador, mas o aviso fica aqui porque e o erro obvio de cometer.
  - COM e do comtypes, nao nosso. Ele chama CoInitializeEx no proprio import, em
    STA; inicializar por fora em MTA antes disso quebra com RPC_E_CHANGED_MODE
    ("nao e possivel alterar o modo de thread"). Para as threads de trabalho
    usamos comtypes.CoInitialize(), que respeita o modo dele.

Aceita mudo total ou abaixar para uma porcentagem (duck), melhor quando voce
esta numa chamada e nao quer perder o que o outro lado fala.
"""

import logging
import threading

log = logging.getLogger("ditado")

# Importado no carregamento do modulo, na thread principal, para o comtypes
# fazer a inicializacao dele antes de qualquer thread nossa aparecer.
try:
    import comtypes
    from pycaw.pycaw import AudioUtilities
    OK = True
except Exception as _exc:  # pragma: no cover
    logging.getLogger("ditado").warning("controle de volume indisponivel: %s", _exc)
    comtypes = None
    AudioUtilities = None
    OK = False

_lock = threading.Lock()
_saved = None  # (estava_mudo, volume_anterior) ou None se nao mexemos


class _Com:
    """CoInitialize/CoUninitialize por thread, do jeito que o comtypes espera."""

    def __enter__(self):
        if comtypes is not None:
            try:
                comtypes.CoInitialize()
            except Exception:
                pass
        return self

    def __exit__(self, *a):
        if comtypes is not None:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
        return False


def _endpoint():
    return AudioUtilities.GetSpeakers().EndpointVolume


def available():
    if not OK:
        return False
    try:
        with _Com():
            _endpoint().GetMute()
        return True
    except Exception as exc:
        log.warning("controle de volume nao respondeu: %s", str(exc)[:120])
        return False


def silence(duck_percent=0):
    """
    Silencia (duck_percent=0) ou abaixa para a porcentagem dada.
    Guarda o estado anterior para o restore(). Idempotente: chamar duas vezes
    nao sobrescreve o estado guardado na primeira.
    """
    global _saved
    if not OK:
        return False
    with _lock:
        if _saved is not None:
            return True
        try:
            with _Com():
                vol = _endpoint()
                antes = (bool(vol.GetMute()), float(vol.GetMasterVolumeLevelScalar()))
                if duck_percent and duck_percent > 0:
                    vol.SetMasterVolumeLevelScalar(
                        max(0.0, min(1.0, duck_percent / 100.0)), None)
                else:
                    vol.SetMute(1, None)
            _saved = antes
            return True
        except Exception as exc:
            log.warning("nao consegui silenciar a saida: %s", str(exc)[:140])
            _saved = None
            return False


def restore():
    """Devolve exatamente o estado que existia antes do silence()."""
    global _saved
    if not OK:
        return
    with _lock:
        if _saved is None:
            return  # nao mexemos; nao ha o que restaurar
        estava_mudo, nivel = _saved
        _saved = None
        try:
            with _Com():
                vol = _endpoint()
                vol.SetMasterVolumeLevelScalar(nivel, None)
                vol.SetMute(1 if estava_mudo else 0, None)
        except Exception as exc:
            log.warning("nao consegui restaurar a saida: %s", str(exc)[:140])
