"""
Pos-processamento do texto transcrito: comandos de voz, substituicoes de
vocabulario, capitalizacao e espacamento.

Sobre comandos de voz, e por que a lista padrao e curta:

  O Whisper ja pontua sozinho em pt-BR. Uma lista grande de comandos ("virgula",
  "ponto", "interrogacao") gera falso positivo o tempo todo - "a virgula que
  voce colocou" viraria "a , que voce colocou". Entao o padrao traz so os que
  o modelo NAO consegue inferir de jeito nenhum, que sao as quebras de linha.
  O resto fica por conta do usuario acrescentar no dashboard, ciente do risco.
"""

import re

# Comandos seguros: nao existe como o modelo adivinhar quebra de linha sozinho.
DEFAULT_COMMANDS = [
    {"say": "novo paragrafo", "emit": "\n\n"},
    {"say": "novo parágrafo", "emit": "\n\n"},
    {"say": "nova linha", "emit": "\n"},
    {"say": "pula linha", "emit": "\n"},
]


def _boundary_pattern(phrase):
    """
    Casa a frase como unidade, tolerando pontuacao que o Whisper cola nela.
    Ex.: 'nova linha' casa em 'nova linha', 'Nova linha.' e ', nova linha,'.
    """
    esc = r"\s+".join(re.escape(w) for w in phrase.split())
    return re.compile(r"(?<![\wáéíóúâêôãõàçü])" + esc + r"[.,;:!?]?(?![\wáéíóúâêôãõàçü])",
                      re.IGNORECASE)


_cmd_cache = {}


def apply_voice_commands(text, commands=None):
    cmds = commands if commands is not None else DEFAULT_COMMANDS
    for c in cmds:
        say = (c.get("say") or "").strip()
        if not say:
            continue
        emit = c.get("emit", "")
        pat = _cmd_cache.get(say)
        if pat is None:
            pat = _boundary_pattern(say)
            _cmd_cache[say] = pat
        text = pat.sub(lambda _m: emit, text)
    # Um comando no meio da frase deixa espaco sobrando dos dois lados.
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return text


def apply_replacements(text, replacements):
    """
    Substituicoes de vocabulario. Aplicadas em ordem; regex opcional.
    Erro de regex do usuario nao pode derrubar o ditado, entao e ignorado.
    """
    for r in replacements or []:
        src = r.get("from")
        if not src:
            continue
        dst = r.get("to", "")
        try:
            if r.get("regex"):
                text = re.sub(src, dst, text,
                              flags=0 if r.get("case_sensitive") else re.IGNORECASE)
            elif r.get("case_sensitive"):
                text = text.replace(src, dst)
            else:
                text = re.sub(re.escape(src), lambda _m: dst, text, flags=re.IGNORECASE)
        except re.error:
            continue
    return text


def capitalize_first(text):
    for i, ch in enumerate(text):
        if ch.isalpha():
            return text[:i] + ch.upper() + text[i + 1:]
        if not ch.isspace():
            break
    return text


def strip_final_period(text):
    return re.sub(r"\.\s*$", "", text)


def collapse_spaces(text):
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def process(text, opts, replacements=None, commands=None):
    """
    opts: dict com as chaves de "text" do config (ja com o perfil aplicado).
    A ordem importa: comandos de voz primeiro (podem inserir quebras), depois
    vocabulario (o usuario escreve as regras pensando no texto ja quebrado),
    e capitalizacao no fim.
    """
    if not text:
        return text
    if opts.get("voice_commands", True):
        text = apply_voice_commands(text, commands)
    text = apply_replacements(text, replacements)
    text = collapse_spaces(text)
    if opts.get("strip_trailing_period"):
        text = strip_final_period(text)
    if opts.get("capitalize", True):
        text = capitalize_first(text)
    return text


# ---------------------------------------------------------------------------
# Snippets e comando de envio
# ---------------------------------------------------------------------------
def _norm(s):
    """Minusculo, sem acento e sem pontuacao: 'Meu E-mail.' == 'meu e mail'."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^\w\s]", " ", s).split()


def match_snippet(text, snippets):
    """
    O ditado inteiro e so o gatilho (ou gatilho + 'envia')? Devolve o snippet.

    Casa a fala INTEIRA, nao um trecho no meio da frase: 'manda meu email
    pra ele' nao pode virar o bloco do snippet colado no meio da conversa.
    Aceita palavras de enchimento antes ("é... meu email") e as
    variacoes do gatilho listadas em 'aliases'.
    """
    words = _norm(text)
    if not words or len(words) > 10:
        return None
    filler = {"e", "ai", "a", "o", "cola", "snippet", "atalho", "ok", "entao"}
    while words and words[0] in filler:
        words = words[1:]
    for sn in snippets or []:
        gatilhos = [sn.get("trigger", "")] + list(sn.get("aliases") or [])
        for g in gatilhos:
            gw = _norm(g)
            if gw and words == gw:
                return sn
    # Sem casamento aproximado DE PROPOSITO: um erro tipico do Whisper para
    # um gatilho e uma frase normal qualquer chegam a dar a mesma parecenca
    # (~0,8) com o gatilho. Erro recorrente do
    # Whisper entra como alias explicito no snippet.
    return None


SEND_WORDS = ("envia", "enviar")


def split_send(text, words=SEND_WORDS):
    """
    'tudo certo, ja ja te mando o rastreio, envia' -> (texto sem o comando, True).

    So dispara se a palavra for a ULTIMA e vier SEPARADA por pausa (o Whisper
    poe virgula/ponto quando voce para antes de falar), ou se for a fala
    inteira. 'o comprovante voce envia' (sem pausa) NAO dispara. 'manda' ficou
    de fora de proposito: 'qualquer coisa me manda' fecha muita frase.
    Devolve (texto, False) se nao achou.
    """
    t = (text or "").rstrip()
    m = re.search(r"(^|[,.;!?…]\s*)(%s)[\s.!,]*$" % "|".join(words), t, re.IGNORECASE)
    if not m:
        return text, False
    antes = t[:m.start()]
    sep = m.group(1).strip()
    # Ponto/interrogacao antes do comando e fim de frase de verdade: fica.
    # Virgula era so a pausa antes de dizer "envia": sai.
    if sep in (".", "!", "?", "…"):
        antes += sep
    return antes.rstrip(" ,;"), True


def profile_for(exe, profiles, title=""):
    """
    Primeiro perfil que casa com a janela em foco. Dois criterios, ambos
    substring case-insensitive e ambos opcionais (o que estiver vazio nao
    filtra, mas pelo menos um tem que existir):
      match -> nome do executavel ('code' casa 'Code.exe')
      title -> titulo da janela ('WhatsApp' casa a aba do WhatsApp Web
               no navegador, que pelo executavel seria so um navegador)
    Perfis mais especificos (com title) devem vir antes na lista.
    """
    low = (exe or "").lower()
    tlow = (title or "").lower()
    for p in profiles or []:
        m = (p.get("match") or "").strip().lower()
        t = (p.get("title") or "").strip().lower()
        if not m and not t:
            continue
        if m and m not in low:
            continue
        if t and t not in tlow:
            continue
        return p
    return None
