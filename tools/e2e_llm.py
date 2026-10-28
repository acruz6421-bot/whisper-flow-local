"""
Teste de ponta a ponta do llm.py contra o servidor rodando (porta 9978).
Uso: python tools/e2e_llm.py
Le chaves de variaveis de ambiente; nada fica gravado no repo.
"""
import json
import os
import sys
import time
import urllib.request

SRV = os.environ.get("WFL_SERVER", "http://127.0.0.1:9978")


def post(path, body, timeout=300):
    req = urllib.request.Request(SRV + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    ollama_model = os.environ.get("E2E_OLLAMA_MODEL", "")
    print("models (ollama):", post("/api/llm/models",
          {"provider": "ollama", "config": {"url": "http://127.0.0.1:11434"}}))
    if ollama_model:
        r = post("/api/llm/test", {"llm": {"provider": "ollama", "timeout_s": 180,
                 "ollama": {"url": "http://127.0.0.1:11434", "model": ollama_model,
                            "keep_alive": "2m", "think": False}}})
        print("ollama:", r["results"])
    url, key, model = (os.environ.get("E2E_CUSTOM_URL", ""), os.environ.get("E2E_CUSTOM_KEY", ""),
                       os.environ.get("E2E_CUSTOM_MODEL", ""))
    if url:
        r = post("/api/llm/test", {"llm": {"provider": "custom", "timeout_s": 30,
                 "custom": {"url": url, "api_key": key, "model": model},
                 "fallback": {"provider": "custom", "url": "http://127.0.0.1:1/v1", "model": "x"}}})
        for x in r["results"]:
            print("custom:", x)
        print("models (custom):", str(post("/api/llm/models", {"provider": "custom",
              "config": {"url": url, "api_key": key}}))[:200])


if __name__ == "__main__":
    sys.exit(main())
