# backend/app/models/provider_adapters/ollama_adapter.py
"""Fixed Ollama Provider Adapter for AI_AGENT_OS"""

import httpx
import json
from typing import Dict, Any, Optional
from datetime import datetime
import logging

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

        # Test connection silently
        try:
            self.client.get(f"{self.base_url}/api/tags", timeout=5.0)
        except:
            pass  # Connection test fails silently
    
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
            
            # Make API call
            response = self.client.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=kwargs.get("timeout", 300.0)
            )
            
            # Parse response
            if response.status_code == 200:
                response_data = response.json()
                return response_data.get("response", "No response generated")
            else:
                return f"Error: {response.status_code} - {response.text}"
                
        except Exception as e:
            return f"Connection error: {str(e)}. Using mock response."
    
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
