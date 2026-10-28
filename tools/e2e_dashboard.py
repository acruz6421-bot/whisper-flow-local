"""
Teste do painel num navegador de verdade (Chromium headless via Playwright).
Clica nos botoes da secao de IA e confere o que aparece na tela.

    pip install playwright && playwright install chromium
    python tools/e2e_dashboard.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

URL = os.environ.get("WFL_SERVER", "http://127.0.0.1:9978")


def main():
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(URL)
        pg.wait_for_function("document.getElementById('l-prov').value !== ''")
        print("provedor carregado:", pg.input_value("#l-prov"))

        # troca para custom e confere a visibilidade dos blocos
        pg.select_option("#l-prov", "custom")
        print("custom visivel:", pg.is_visible("#box-custom"), "| ollama visivel:", pg.is_visible("#box-ollama"))
        pg.select_option("#l-prov", "ollama")
        print("ollama visivel:", pg.is_visible("#box-ollama"), "| custom visivel:", pg.is_visible("#box-custom"))

        # listar modelos do Ollama
        pg.click("button[onclick=\"listModels('ollama')\"]")
        pg.wait_for_selector("#toast.show", timeout=15000)
        print("listar ->", pg.inner_text("#toast"),
              "| opcoes:", pg.eval_on_selector_all("#lo-list option", "o => o.map(x => x.value)"))

        # testar conexao
        pg.click("button[onclick='testLLM()']")
        pg.wait_for_function("!document.getElementById('llm-test').textContent.includes('testando')"
                             " && document.getElementById('llm-test').textContent.trim() !== ''",
                             timeout=120000)
        print("testar ->", pg.inner_text("#llm-test"))

        # salvar e reler
        pg.fill("#lo-keep", "3m")
        pg.click("button.primary")
        pg.wait_for_selector("#toast.show", timeout=10000)
        print("salvar ->", pg.inner_text("#toast"))
        pg.reload()
        pg.wait_for_function("document.getElementById('lo-keep').value !== ''")
        print("apos reload keep_alive =", pg.input_value("#lo-keep"))
        pg.screenshot(path=os.path.join(os.environ.get("TEMP", "."), "wfl_dashboard.png"), full_page=False)
        b.close()
    print("erros de console:", errors or "nenhum")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
