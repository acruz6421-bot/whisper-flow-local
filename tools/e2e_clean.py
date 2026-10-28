"""
Teste de limpeza de ditado (auto_clean real do dictate.py) sem microfone.
Roda as travas de seguranca de verdade (corte, numero inventado).

    python tools/e2e_clean.py
Configure o provedor por variaveis: E2E_PROVIDER=ollama|custom, E2E_URL,
E2E_KEY, E2E_MODEL, E2E_FALLBACK_URL/KEY/MODEL (opcional).
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("WFL_DATA", os.path.join(os.environ.get("TEMP", "/tmp"), "wfl_e2e"))
os.environ["DITADO_NO_INJECT"] = "1"

import dictate  # noqa: E402
from config import CFG  # noqa: E402

CASES = [
    "é, bom dia, meu amigo, tudo bem? Esse seu pedido fica trezentos e cinquenta reais com o Sedex já incluído, "
    "se você quiser o PAC fica vinte e cinco, então o valor total vai ficar, é, trezentos e vinte e cinco",
    "então tipo eu queria saber se a reunião é amanhã às três, não, espera, às quatro da tarde, né, "
    "e se precisa levar o notebook",
    "manda o relatório pro João até sexta e me avisa quando terminar",
]


def main():
    prov = os.environ.get("E2E_PROVIDER", "ollama")
    llm = dict(CFG.get("llm"))
    llm["provider"] = prov
    llm["auto_clean"] = True
    llm["auto_timeout_s"] = float(os.environ.get("E2E_TIMEOUT", "30"))
    if prov == "ollama":
        llm["ollama"] = dict(llm["ollama"], model=os.environ["E2E_MODEL"],
                             url=os.environ.get("E2E_URL", "http://127.0.0.1:11434"))
    else:
        llm["custom"] = {"url": os.environ["E2E_URL"], "api_key": os.environ.get("E2E_KEY", ""),
                         "model": os.environ["E2E_MODEL"]}
    if os.environ.get("E2E_FALLBACK_URL"):
        llm["fallback"] = {"provider": "custom", "url": os.environ["E2E_FALLBACK_URL"],
                           "api_key": os.environ.get("E2E_FALLBACK_KEY", ""),
                           "model": os.environ.get("E2E_FALLBACK_MODEL", "")}
    data = dict(CFG.data)
    data["llm"] = llm
    CFG._data = data  # so em memoria; nada e gravado

    for text in CASES:
        t0 = time.time()
        out = dictate.auto_clean(text, None)
        print("\n[%.2fs] IN : %s" % (time.time() - t0, text))
        print("        OUT: %s" % (out if out is not None else "(rejeitado/pulado -> cola o bruto)"))


if __name__ == "__main__":
    main()
