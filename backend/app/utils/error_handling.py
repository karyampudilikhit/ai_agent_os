# backend/app/utils/error_handling.py
"""Centralized error handling for the AI Workforce OS"""

from typing import Optional, Dict, Any
import logging
from enum import Enum

# Configure logger
logger = logging.getLogger(__name__)

class ErrorCode(Enum):
    """Standardized error codes for the system"""
    # Contract Errors
    CONTRACT_INVALID = "CONTRACT_INVALID"
    CONTRACT_PARSE_FAILED = "CONTRACT_PARSE_FAILED"
    
    # Agent Errors
    AGENT_CREATION_FAILED = "AGENT_CREATION_FAILED"
    AGENT_EXECUTION_FAILED = "AGENT_EXECUTION_FAILED"
    AGENT_VALIDATION_FAILED = "AGENT_VALIDATION_FAILED"
    
    # Trait Errors
    TRAIT_GENERATION_FAILED = "TRAIT_GENERATION_FAILED"
    TRAIT_VALIDATION_FAILED = "TRAIT_VALIDATION_FAILED"
    
    # Model Errors
    MODEL_ROUTING_FAILED = "MODEL_ROUTING_FAILED"
    MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    
    # Safety Errors
    RECURSION_LIMIT_EXCEEDED = "RECURSION_LIMIT_EXCEEDED"
    COST_LIMIT_EXCEEDED = "COST_LIMIT_EXCEEDED"
    DEPENDENCY_CYCLE_DETECTED = "DEPENDENCY_CYCLE_DETECTED"
    
    # Memory Errors
    MEMORY_STORAGE_FAILED = "MEMORY_STORAGE_FAILED"
    MEMORY_RETRIEVAL_FAILED = "MEMORY_RETRIEVAL_FAILED"
    
    # Tool Errors
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_BUDGET_EXCEEDED = "TOOL_BUDGET_EXCEEDED"
    
    # General Errors
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"

class AIWorkforceError(Exception):
    """Base exception class for AI Workforce OS"""
    
    def __init__(
        self, 
        message: str, 
        error_code: ErrorCode,
        context: Optional[Dict[str, Any]] = None,
        cause: Optional[Exception] = None
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.context = context or {}
        self.cause = cause
        self.timestamp = __import__('datetime').datetime.now()
        
        # Log the error
        logger.error(
            f"{error_code.value}: {message}", 
            extra={
                "error_code": error_code.value,
                "context": context,
                "cause": str(cause) if cause else None
            }
        )

class ContractError(AIWorkforceError):
    """Errors related to execution contracts"""
    pass

class AgentError(AIWorkforceError):
    """Errors related to agent creation or execution"""
    pass

class TraitError(AIWorkforceError):
    """Errors related to trait generation or validation"""
    pass

class ModelError(AIWorkforceError):
    """Errors related to model interactions"""
    pass

class SafetyError(AIWorkforceError):
    """Errors related to safety violations"""
    pass

class MemoryError(AIWorkforceError):
    """Errors related to memory operations"""
    pass

class ToolError(AIWorkforceError):
    """Errors related to tool execution"""
    pass

def handle_exception(func):
    """Decorator to wrap functions with standardized error handling"""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AIWorkforceError:
            # Re-raise our custom errors
            raise
        except Exception as e:
            # Wrap unexpected errors
            raise AIWorkforceError(
                f"Unexpected error in {func.__name__}: {str(e)}",
                ErrorCode.INTERNAL_ERROR,
                {"function": func.__name__},
                e
            )
    return wrapper

# Convenience functions for common error scenarios
def raise_contract_error(message: str, context: Optional[Dict] = None):
    raise ContractError(message, ErrorCode.CONTRACT_INVALID, context)

def raise_agent_error(message: str, context: Optional[Dict] = None):
    raise AgentError(message, ErrorCode.AGENT_CREATION_FAILED, context)

def raise_trait_error(message: str, context: Optional[Dict] = None):
    raise TraitError(message, ErrorCode.TRAIT_GENERATION_FAILED, context)

def raise_model_error(message: str, context: Optional[Dict] = None):
    raise ModelError(message, ErrorCode.MODEL_ROUTING_FAILED, context)

def raise_safety_error(message: str, context: Optional[Dict] = None):
    raise SafetyError(message, ErrorCode.RECURSION_LIMIT_EXCEEDED, context)

# Configuration for error handling
ERROR_CONFIG = {
    "log_level": "ERROR",
    "include_traceback": False,
    "max_context_length": 1000
}
