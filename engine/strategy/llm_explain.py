# llm_explain.py
import os
import threading

VOICE_ENABLED = os.environ.get("VOICE_ENABLED", "1") == "1"
VOICE_TIMEOUT_S = float(os.environ.get("VOICE_TIMEOUT_S", "6.0"))
VOICE_MAX_PROMPT_CHARS = int(os.environ.get("VOICE_MAX_PROMPT_CHARS", "8000"))
VOICE_MAX_RESPONSE_CHARS = int(os.environ.get("VOICE_MAX_RESPONSE_CHARS", "700"))

def run_llm_explain_with_timeout(prompt: str, timeout_s: float) -> str:
    result = {}
    error = {}

    def _worker():
        try:
            try:
    from llm import llmExplain
except Exception:
    raise RuntimeError("llmExplain unavailable")
            result["text"] = llmExplain(prompt)
        except Exception as e:
            error["error"] = str(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout_s)

    if t.is_alive():
        raise TimeoutError(f"LLM timeout after {timeout_s}s")

    if "error" in error:
        raise RuntimeError(error["error"])

    return str(result.get("text") or "")
