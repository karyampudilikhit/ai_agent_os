# backend/app/models/provider_adapters/ollama_adapter.py
"""Fixed Ollama Provider Adapter for AI_AGENT_OS"""

import httpx
import json
import os
import random
import time
from typing import Dict, Any, Optional
from datetime import datetime
import logging

# Ollama cloud returns HTTP 500 intermittently and in bursts. Measured
# during a live session: five short probes returned 200 while a real run
# was failing, and prompt size was ruled out (1KB through 64KB all
# returned 200). One specialist then took FOUR consecutive 500s across 36
# seconds -- enough to exhaust the agentic loop's own retry and end its
# tool use entirely, so run_python never ran and the founder was told
# "no computation was ever run".
#
# Retrying HERE rather than at each call site covers every call in the
# system -- synthesis, critique, task classification, evidence extraction
# -- not just the loop's step call. Jittered because a team whose
# specialists all back off in lockstep resends as a thundering herd,
# which is a plausible contributor to the bursts.
OLLAMA_HTTP_RETRIES = int(os.environ.get("OLLAMA_HTTP_RETRIES", "2"))
OLLAMA_HTTP_RETRY_BACKOFF = float(os.environ.get("OLLAMA_HTTP_RETRY_BACKOFF", "2.0"))


# Simple error classes for standalone operation
class ErrorCode:
    MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"

class OllamaAdapterError(Exception):
    def __init__(self, message, error_code=None, context=None):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.context = context or {}

class OllamaAdapter:
    """Adapter for Ollama local model serving OR Ollama Cloud API.

    When api_key is set (Ollama Cloud), attaches Authorization header
    to every request. Cloud + local expose the same /api/generate
    endpoint shape, so nothing else has to change."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3",
        api_key: Optional[str] = None,
    ):
        """
        Args:
            base_url: Ollama server URL. Local daemon by default;
                      set to https://ollama.com for Ollama Cloud API.
            model:    Model name.
            api_key:  Ollama Cloud API key. Local daemon needs none.
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.client = httpx.Client(timeout=300.0, headers=headers)

        # Probe the server once so a misconfigured host is visible in the
        # logs at construction rather than surfacing later as a confusing
        # mid-run failure. Deliberately non-fatal — the daemon may still
        # be starting — but no longer a bare `except: pass`, which hid
        # even KeyboardInterrupt.
        try:
            self.client.get(f"{self.base_url}/api/tags", timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "Ollama not reachable at %s (%s) — calls will fail until it is",
                self.base_url, exc,
            )
    
    def chat_completion(self, prompt: str, **kwargs) -> str:
        """
        Generate chat completion using Ollama
        
        Args:
            prompt: Text prompt for the model
            **kwargs: Additional parameters
            
        Returns:
            str: Generated response text
        """
        try:
            # Default parameters
            temperature = kwargs.get("temperature", 0.7)
            max_tokens = kwargs.get("max_tokens", 2000)
            
            # Prepare request payload
            payload = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                # Forces valid-JSON-constrained decoding. Every caller in
                # this codebase asks for JSON back; without this, small
                # models like phi3 free-generate near-JSON and break
                # unpredictably (same prompt, run twice, different result).
                "format": kwargs.get("format", "json"),
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens
                }
            }
            
            # Make API call. Transient 5xx are retried here so a burst of
            # backend errors doesn't surface as a content failure; every
            # other status falls through to the handling below unchanged.
            response = None
            for _attempt in range(OLLAMA_HTTP_RETRIES + 1):
                response = self.client.post(
                    f"{self.base_url}/api/generate",
                    json=payload,
                    timeout=kwargs.get("timeout", 300.0)
                )
                if response.status_code < 500:
                    break
                if _attempt < OLLAMA_HTTP_RETRIES:
                    delay = OLLAMA_HTTP_RETRY_BACKOFF * (2 ** _attempt)
                    delay += random.uniform(0, delay * 0.25)  # jitter
                    logging.getLogger(__name__).warning(
                        "Ollama HTTP %s (attempt %d/%d), retrying in %.1fs",
                        response.status_code, _attempt + 1,
                        OLLAMA_HTTP_RETRIES + 1, delay,
                    )
                    time.sleep(delay)

            # Parse response
            if response.status_code == 200:
                response_data = response.json()
                text = response_data.get("response") or ""

                # An EMPTY 200 is a failure wearing a success's clothes,
                # and it is the normal outcome for a thinking model whose
                # reasoning outgrew the token budget: Ollama spends
                # `num_predict` on `thinking` first, then has nothing left
                # for `response` and returns done_reason="length" with
                # response="". Found while trying to run the
                # tool-selection diagnostic on nemotron-3-super — every
                # call came back "" and the run would have reported
                # "the model never called run_python", which is the exact
                # WRONG conclusion: it never said anything at all.
                #
                # Same rule as the HTTP branch below: raise. Returning ""
                # lets a backend condition travel as content, and an empty
                # string is worse than an error string because nothing
                # downstream can even tell something went wrong.
                if not text.strip():
                    thinking = response_data.get("thinking") or ""
                    reason = response_data.get("done_reason")
                    detail = (
                        f"empty response (done_reason={reason!r}, "
                        f"eval_count={response_data.get('eval_count')}, "
                        f"thinking={len(thinking)} chars)"
                    )
                    if reason == "length" and thinking:
                        detail += (
                            " — the model's reasoning consumed the whole "
                            f"num_predict budget ({max_tokens}). Raise "
                            "max_tokens for this model, or use one that "
                            "reasons less."
                        )
                    raise OllamaAdapterError(
                        f"Ollama returned an {detail}",
                        error_code=ErrorCode.MODEL_CALL_FAILED,
                        context={
                            "model": self.model,
                            "done_reason": reason,
                            "max_tokens": max_tokens,
                            "thinking_chars": len(thinking),
                        },
                    )
                return text

            # RAISE, never return the error as if it were the model's
            # answer. Returning it — which this did — means every caller
            # treats a backend failure as valid content, and the failure
            # travels instead of stopping. Observed for real: a run
            # shipped three 502 bodies as the founder's deliverable and
            # was still marked done, and a specialist fed
            # 'Error: 502 - {"error":"Post \"https://ollama.com...' into
            # a web search as its query. Callers that can genuinely
            # continue without a model already wrap this in try/except
            # (see dynamic_employee._needs_external_lookup and
            # _search_query_for); the ones that can't now fail loudly.
            raise OllamaAdapterError(
                f"Ollama returned HTTP {response.status_code}: {response.text[:300]}",
                error_code=ErrorCode.MODEL_CALL_FAILED,
                context={"status_code": response.status_code, "model": self.model},
            )

        except OllamaAdapterError:
            raise
        except httpx.TimeoutException as exc:
            raise OllamaAdapterError(
                f"Ollama timed out after {kwargs.get('timeout', 300.0)}s: {exc}",
                error_code=ErrorCode.MODEL_TIMEOUT,
                context={"model": self.model},
            ) from exc
        except Exception as exc:
            raise OllamaAdapterError(
                f"Ollama call failed: {exc}",
                error_code=ErrorCode.MODEL_CALL_FAILED,
                context={"model": self.model, "base_url": self.base_url},
            ) from exc
    
    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the configured model"""
        return {
            "provider": "Ollama (Local)",
            "model": self.model,
            "status": "ready"
        }
    
    def close(self):
        """Close HTTP client connection"""
        if hasattr(self, 'client'):
            self.client.close()

# Simple test
if __name__ == "__main__":
    try:
        adapter = OllamaAdapter()
        response = adapter.chat_completion("Say hello in one sentence")
        print("Ollama test:", response[:100] + "..." if len(response) > 100 else response)
    except Exception as e:
        print("Ollama test failed:", e)
