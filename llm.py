"""
Text-LLM client used by every AI feature (auto-clean, polish, command mode,
transforms, English mode).

Two provider types, both configured in config.json -> "llm":

  ollama  - local Ollama server (default http://127.0.0.1:11434). Uses the
            native /api/chat endpoint so it can pass keep_alive, which lets
            Ollama unload the model from VRAM on the same idle policy as
            Whisper.
  custom  - any OpenAI-compatible /chat/completions endpoint with an optional
            API key: OpenAI, OpenRouter, Groq, Gemini (OpenAI-compat), LM Studio,
            llama.cpp server, vLLM, a self-hosted proxy, etc.

Optional "fallback": a second provider tried once if the first one fails or
times out. Nothing is hardcoded: no URL, key or model ships in this file.

Only the Python standard library is used.
"""

import json
import time
import logging
import urllib.error
import urllib.request

log = logging.getLogger("ditado")

OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"


class LLMError(RuntimeError):
    pass


def _post_json(url, payload, headers, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=dict({"Content-Type": "application/json"}, **headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise LLMError("HTTP %s from %s: %s" % (exc.code, url, body))
    except urllib.error.URLError as exc:
        raise LLMError("cannot reach %s: %s" % (url, exc.reason))


def _get_json(url, headers=None, timeout=5):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _chat_url(base):
    base = (base or "").rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _strip_think(text):
    """Reasoning models (qwen3, deepseek-r1) may prefix <think>...</think>."""
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()


# ---------------------------------------------------------------------------
# provider calls
# ---------------------------------------------------------------------------
def _call_ollama(prov, model, messages, temperature, max_tokens, timeout):
    url = (prov.get("url") or OLLAMA_DEFAULT_URL).rstrip("/")
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
        # Free VRAM after this long without use (Ollama duration string or
        # seconds). Same idea as Whisper's idle unload.
        "keep_alive": prov.get("keep_alive", "10m"),
    }
    if prov.get("think") is False:
        payload["think"] = False
    data = _post_json(url + "/api/chat", payload, {}, timeout)
    return ((data.get("message") or {}).get("content") or "").strip()


def _call_custom(prov, model, messages, temperature, max_tokens, timeout):
    url = prov.get("url") or ""
    if not url:
        raise LLMError("custom provider has no URL")
    headers = {"User-Agent": "whisper-flow-local"}
    if prov.get("api_key"):
        headers["Authorization"] = "Bearer %s" % prov["api_key"]
    for k, v in (prov.get("headers") or {}).items():
        headers[str(k)] = str(v)
    payload = {"model": model, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens}
    data = _post_json(_chat_url(url), payload, headers, timeout)
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise LLMError("unexpected response shape: %s" % str(data)[:200])


_CALLERS = {"ollama": _call_ollama, "custom": _call_custom}


def _providers(cfg):
    """Primary first, then the optional fallback. Each item: (name, dict)."""
    out = []
    prov = (cfg.get("provider") or "off").lower()
    if prov in _CALLERS:
        out.append((prov, cfg.get(prov) or {}))
    fb = cfg.get("fallback") or {}
    fb_type = (fb.get("provider") or "off").lower()
    if fb_type in _CALLERS:
        out.append((fb_type, fb))
    return out


def enabled(cfg):
    return bool(_providers(cfg or {}))


def chat(cfg, system, user, task="clean", timeout=None, temperature=0.2, max_tokens=4000):
    """
    Run one chat completion. `task` picks the per-task model override:
    cfg["models"][task] (clean / command / polish / translate) falls back to
    the provider's own "model". Raises LLMError when every provider failed.
    """
    cfg = cfg or {}
    chain = _providers(cfg)
    if not chain:
        raise LLMError("LLM is off (set llm.provider to 'ollama' or 'custom')")
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    errors = []
    # Um prazo so para a cadeia inteira: o fallback usa o que sobrou, assim a
    # limpeza automatica nunca segura o ditado mais que timeout_s.
    budget = timeout if timeout is not None else float(cfg.get("timeout_s", 30))
    deadline = time.time() + budget
    for i, (kind, prov) in enumerate(chain):
        is_fallback = i > 0
        model = prov.get("model") if is_fallback else (
            ((cfg.get("models") or {}).get(task)) or prov.get("model"))
        if not model:
            errors.append("%s: no model set" % kind)
            continue
        t = deadline - time.time()
        if t < 0.3:
            errors.append("%s/%s: no time left" % (kind, model))
            break
        t0 = time.time()
        try:
            out = _CALLERS[kind](prov, model, messages, temperature, max_tokens, t)
            out = _strip_think(out)
            log.info("llm %s/%s (%s) in %.2fs%s", kind, model, task, time.time() - t0,
                     " [fallback]" if is_fallback else "")
            return out
        except Exception as exc:
            errors.append("%s/%s: %s" % (kind, model, str(exc)[:160]))
            log.warning("llm %s/%s failed after %.2fs: %s", kind, model,
                        time.time() - t0, str(exc)[:160])
    raise LLMError(" | ".join(errors))


# ---------------------------------------------------------------------------
# helpers for the dashboard
# ---------------------------------------------------------------------------
def warm(cfg):
    """
    Load the Ollama model into VRAM without generating anything, so the
    cleanup after this dictation does not pay the cold load (5-15 s). Called
    when recording starts, in a background thread. No-op for custom providers.
    """
    cfg = cfg or {}
    if (cfg.get("provider") or "").lower() != "ollama" or not cfg.get("auto_clean"):
        return False
    prov = cfg.get("ollama") or {}
    model = ((cfg.get("models") or {}).get("clean")) or prov.get("model")
    if not model:
        return False
    url = (prov.get("url") or OLLAMA_DEFAULT_URL).rstrip("/")
    try:
        _post_json(url + "/api/generate", {"model": model,
                   "keep_alive": prov.get("keep_alive", "10m")}, {}, 60)
        return True
    except Exception as exc:
        log.debug("ollama warm failed: %s", exc)
        return False


def list_models(kind, prov):
    """Model ids the provider reports, for the dashboard dropdown."""
    try:
        if kind == "ollama":
            url = (prov.get("url") or OLLAMA_DEFAULT_URL).rstrip("/")
            data = _get_json(url + "/api/tags")
            return sorted(m.get("name") for m in data.get("models", []) if m.get("name"))
        if kind == "custom":
            base = (prov.get("url") or "").rstrip("/")
            if base.endswith("/chat/completions"):
                base = base[: -len("/chat/completions")]
            headers = {}
            if prov.get("api_key"):
                headers["Authorization"] = "Bearer %s" % prov["api_key"]
            data = _get_json(base + "/models", headers)
            return sorted(m.get("id") for m in data.get("data", []) if m.get("id"))
    except Exception as exc:
        raise LLMError(str(exc)[:200])
    return []


def test(cfg):
    """One tiny round-trip, reported per provider for the dashboard."""
    results = []
    for i, (kind, prov) in enumerate(_providers(cfg or {})):
        t0 = time.time()
        try:
            out = _CALLERS[kind](prov, prov.get("model") or "", [
                {"role": "user", "content": "Reply with the single word: ok"}],
                0.0, 20, float((cfg or {}).get("timeout_s", 30)))
            results.append({"provider": kind, "fallback": i > 0, "ok": True,
                            "model": prov.get("model"), "seconds": round(time.time() - t0, 2),
                            "reply": _strip_think(out)[:60]})
        except Exception as exc:
            results.append({"provider": kind, "fallback": i > 0, "ok": False,
                            "model": prov.get("model"), "seconds": round(time.time() - t0, 2),
                            "error": str(exc)[:200]})
    return results
