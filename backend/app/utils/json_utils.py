# backend/app/utils/json_utils.py
"""Utility functions for JSON handling and validation"""

import json
import re
from typing import Any, Dict, Union, Optional
import jsonschema
from jsonschema import ValidationError

from ..utils.error_handling import raise_contract_error, raise_agent_error

def validate_json_schema(instance: Any, schema: Dict) -> bool:
    """
    Validate a JSON instance against a schema
    
    Args:
        instance: The JSON data to validate
        schema: The JSON schema to validate against
        
    Returns:
        bool: True if valid
        
    Raises:
        ValidationError: If validation fails
    """
    try:
        jsonschema.validate(instance=instance, schema=schema)
        return True
    except ValidationError as e:
        raise_contract_error(
            f"JSON validation failed: {e.message}",
            {"validation_error": str(e), "instance": instance}
        )

def parse_json_safely(text: str) -> Union[Dict, list]:
    """
    Safely parse JSON from text, handling common formatting issues
    
    Args:
        text: Text that may contain JSON
        
    Returns:
        Parsed JSON object (dict or list)
        
    Raises:
        ValueError: If JSON cannot be parsed
    """
    if not text or not isinstance(text, str):
        raise_agent_error("Invalid input for JSON parsing", {"input": text})
    
    # Remove markdown code blocks if present
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    
    # Try to find JSON in the text
    json_match = re.search(r'\{[\s\S]*\}|\[[\s\S]*\]', text)
    if json_match:
        json_text = json_match.group(0)
    else:
        json_text = text.strip()
    
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        raise_agent_error(
            f"Failed to parse JSON: {str(e)}",
            {"text": text[:200], "error": str(e)}
        )

def serialize_for_logging(obj: Any, max_length: int = 1000) -> str:
    """
    Serialize an object for logging, truncating if too long
    
    Args:
        obj: Object to serialize
        max_length: Maximum length of serialized string
        
    Returns:
        Serialized string representation
    """
    try:
        serialized = json.dumps(obj, indent=2, default=str)
        if len(serialized) > max_length:
            return serialized[:max_length] + "... [truncated]"
        return serialized
    except Exception:
        return f"<Non-serializable object: {type(obj).__name__}>"

def extract_json_from_response(response_text: str) -> Optional[Dict]:
    """
    Extract JSON object from LLM response text
    
    Args:
        response_text: Text response from LLM
        
    Returns:
        Extracted JSON object or None if not found
    """
    try:
        # Look for JSON objects in the response
        # This handles cases where LLM returns explanatory text + JSON
        brace_positions = []
        for i, char in enumerate(response_text):
            if char in '{}[]':
                brace_positions.append((char, i))
        
        if not brace_positions:
            return None
            
        # Find matching braces
        stack = []
        json_ranges = []
        for char, pos in brace_positions:
            if char in '{[':
                stack.append((char, pos))
            elif stack and (
                (char == '}' and stack[-1][0] == '{') or 
                (char == ']' and stack[-1][0] == '[')
            ):
                start_char, start_pos = stack.pop()
                json_ranges.append((start_pos, pos + 1))
        
        # Try to parse the largest JSON block
        for start, end in sorted(json_ranges, key=lambda x: x[1]-x[0], reverse=True):
            json_candidate = response_text[start:end]
            try:
                parsed = json.loads(json_candidate)
                if isinstance(parsed, (dict, list)):
                    return parsed
            except json.JSONDecodeError:
                continue
                
        return None
    except Exception:
        return None
