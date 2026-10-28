# whisper-flow-local

Ditado por voz para Windows que funciona em **qualquer aplicativo**, no estilo do Wispr Flow: você aperta um atalho, fala e o texto aparece onde está o cursor. A transcrição roda **na sua placa de vídeo** com o Whisper large-v3-turbo. A limpeza do texto (tirar "é…", "tipo", "né", pontuar e corrigir quando você se corrige no meio da frase) usa **a IA que você escolher**: um modelo local no Ollama ou qualquer API compatível com OpenAI.

Não precisa de assinatura nem de conta, e o áudio não sai da sua máquina.

> Foi feito em português e para o português do Brasil, mas funciona em qualquer idioma que o Whisper reconhece.

---

## O que ele faz

| | |
|---|---|
| **Ditado global** | `Ctrl+Alt+Space` em qualquer janela. Segurar = push-to-talk; tocar uma vez = começa, tocar de novo = para. Funciona com macro de mouse. |
| **Transcreve enquanto você fala** | O áudio é cortado nas pausas e mandado para a GPU durante a fala. Ao soltar a tecla só falta o último pedaço. |
| **Limpeza por IA** | Tira hesitações e aplica a autocorreção ("às três, não, espera, às quatro" → "às 16h") sem mudar o seu jeito de falar. |
| **Travas de segurança** | Se a IA cortar mais de 45% das palavras, inventar um número ou estourar o timeout, cola o texto bruto. O ditado nunca trava por causa da IA. |
| **Perfis por aplicativo** | No WhatsApp sem ponto final e em tom de chat; em assistentes de IA, como instrução clara. Casa pelo executável ou pelo título da janela. |
| **Command Mode** | Selecione um texto, aperte `Ctrl+Alt+C` e fale o que fazer: "deixa mais curto", "traduz pro inglês", "vira lista". |
| **Transforms** | `Ctrl+Alt+1/2/3` reescreve o texto selecionado com uma instrução fixa, sem precisar falar. |
| **Snippets por voz** | Fale só o gatilho ("meu email") e entra o texto salvo, exatamente como está. |
| **"…, envia"** | Termine a frase com "envia" e ele cola e já aperta Enter. |
| **Som do PC** | `Ctrl+Alt+S` transcreve o que sai da caixa de som (o outro lado de uma chamada). `Ctrl+Alt+D` transcreve microfone + PC. |
| **Libera a GPU** | O Whisper sai da VRAM depois de 1 h parado e o Ollama em 10 min (configurável). Volta sozinho no próximo uso. |
| **Arquivos** | Pasta vigiada ou clique direito → *Enviar para* → *Transcrever com Whisper*: gera `.txt` e `.srt` de áudio e vídeo. |
| **Painel** | `http://127.0.0.1:9978`: atalhos, microfone, IA, perfis, vocabulário e histórico pesquisável. |

Detalhes que dão trabalho e já estão resolvidos:

- **Microfone USB no Windows:** testa WASAPI, DirectSound e MME de verdade, abrindo cada um, e fica com o primeiro que entrega áudio. Alguns microfones USB só funcionam por WASAPI.
- **Teclado ABNT2:** o AltGr não dispara os atalhos `Ctrl+Alt`.
- **Áudio do PC:** o som é silenciado enquanto você dita, para não vazar no microfone, e depois volta exatamente como estava.
- **Clipboard:** o que você tinha copiado é restaurado depois da colagem.

---

## Instalação

Requisitos: **Windows 10/11**, **Python 3.10+**. Placa NVIDIA é recomendada; sem ela, roda na CPU, mais devagar.

```powershell
git clone https://github.com/acruz6421-bot/whisper-flow-local
cd whisper-flow-local
powershell -ExecutionPolicy Bypass -File install.ps1        # com GPU NVIDIA
powershell -ExecutionPolicy Bypass -File install.ps1 -Cpu   # sem GPU
```

O instalador cria o `venv`, instala as dependências, baixa o modelo (~1,6 GB, só na primeira vez) e cria o atalho *Enviar para*.

**Ligar:** `start.bat` (abre o painel). **Desligar e liberar a VRAM:** `stop.bat`.

---

## Escolhendo a IA de texto

No painel, em **IA de texto**:

### Ollama (100% local)

1. Instale o [Ollama](https://ollama.com) e baixe um modelo pequeno, por exemplo `ollama pull qwen3:4b` ou `ollama pull gemma3:4b`.
2. Provedor **Ollama**, clique em **listar**, escolha o modelo e clique em **Testar conexão**.

Modelos de 3B a 8B limpam ditado bem e respondem em 1 a 3 s numa GPU de 8 GB ou mais. O modelo é carregado enquanto você ainda está falando, então a primeira limpeza não paga o tempo de carga. "Pensar" vem desligado em modelos de raciocínio, porque só atrasa.

### Custom (qualquer API compatível com OpenAI)

| Serviço | Endpoint |
|---|---|
| OpenAI | `https://api.openai.com/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Groq | `https://api.groq.com/openai/v1` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` |
| LM Studio | `http://127.0.0.1:1234/v1` |
| llama.cpp / vLLM | `http://127.0.0.1:8080/v1` |

Cole o endpoint, a chave e o modelo, e clique em **Testar conexão**.

### Fallback e modelo por tarefa

Dá para configurar uma segunda IA, tentada uma vez se a primeira cair. O prazo é compartilhado: com timeout de 4 s, a segunda usa o que sobrou, então o ditado nunca espera mais que isso. Também dá para usar um modelo rápido na limpeza e um mais forte no Command Mode.

---

## Atalhos padrão

| Atalho | Ação |
|---|---|
| `Ctrl+Alt+Space` | Ditar e colar |
| `Ctrl+Alt+Shift+Space` | Ditar e só copiar (para janelas de administrador) |
| `Ctrl+Alt+E` | Ditar e traduzir para inglês |
| `Ctrl+Alt+C` | Command Mode |
| `Ctrl+Alt+1` / `2` / `3` | Transforms |
| `Ctrl+Alt+B` | Trocar o último texto limpo pelo que você falou de fato |
| `Ctrl+Alt+P` | Adicionar a palavra selecionada ao vocabulário |
| `Ctrl+Alt+Z` / `R` | Desfazer / colar de novo |
| `Ctrl+Alt+S` / `D` | Transcrever o som do PC / microfone + PC |
| `Esc` | Cancelar a gravação (só durante a gravação) |

Todos podem ser trocados no painel.

---

## Privacidade

- O servidor escuta só em `127.0.0.1`.
- Config, histórico e logs ficam em `data/`, que está no `.gitignore`.
- Com Ollama, nada sai da máquina. Com um provedor custom, só o **texto** transcrito vai para a API que você configurou, nunca o áudio.

## API

O servidor também fala o protocolo da OpenAI. Qualquer app que use `POST /v1/audio/transcriptions` funciona apontando para `http://127.0.0.1:9978`.

## Testes

```bash
python -m unittest discover -s tests -v     # llm.py e texto, sem rede nem GPU
python dictate.py --selftest                # microfone, transcrição, clipboard
python tools/e2e_dashboard.py               # painel num Chromium real (Playwright)
```

## Licença

MIT
