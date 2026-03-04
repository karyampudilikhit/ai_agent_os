# backend/app/utils/config_loader.py
"""Configuration loading and validation for AI_AGENT_OS"""

import yaml
import os
from typing import Dict, Any
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

# Use absolute import by adding to sys.path in the function
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class LimitsConfig(BaseModel):
    """Configuration for system limits"""
    max_total_agents: int = Field(default=50, ge=1, le=100)
    max_adaptive_cycles: int = Field(default=2, ge=0, le=10)
    max_agents_per_cycle: int = Field(default=3, ge=1, le=10)
    max_spawn_rate_per_minute: int = Field(default=5, ge=1, le=20)
    max_total_cost: float = Field(default=100.0, ge=0.0)
    max_refinement_depth: int = Field(default=3, ge=1, le=10)
    min_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    min_completeness_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_error_rate: float = Field(default=0.2, ge=0.0, le=1.0)
    max_dependency_depth: int = Field(default=10, ge=1, le=50)

class RoutingConfig(BaseModel):
    """Configuration for model routing"""
    high_complexity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    cost_optimization_enabled: bool = True
    fallback_model: str = "gpt-4o-mini"

class TraitsDefaultsConfig(BaseModel):
    """Default trait values"""
    exploration_bias: float = Field(default=0.5, ge=0.0, le=1.0)
    risk_sensitivity: str = "neutral"
    refinement_depth: int = Field(default=2, ge=1, le=10)
    validation_intensity: str = "medium"
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)

class TraitsLimitsConfig(BaseModel):
    """Trait limits"""
    max_refinement_depth: int = Field(default=3, ge=1, le=10)
    min_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    max_exploration_bias: float = Field(default=1.0, ge=0.0, le=1.0)
    min_exploration_bias: float = Field(default=0.0, ge=0.0, le=1.0)

class TraitsConfig(BaseModel):
    """Configuration for traits"""
    default_values: TraitsDefaultsConfig = TraitsDefaultsConfig()
    limits: TraitsLimitsConfig = TraitsLimitsConfig()

class MistakeRepositoryConfig(BaseModel):
    """Configuration for mistake repository"""
    enabled: bool = True
    retention_days: int = 30
    max_patterns: int = 1000

class EmbeddingStoreConfig(BaseModel):
    """Configuration for embedding store"""
    enabled: bool = False
    provider: str = "pinecone"

class MemoryConfig(BaseModel):
    """Configuration for memory systems"""
    mistake_repository: MistakeRepositoryConfig = MistakeRepositoryConfig()
    embedding_store: EmbeddingStoreConfig = EmbeddingStoreConfig()

class SafetyConfig(BaseModel):
    """Configuration for safety systems"""
    recursion_protection: bool = True
    constraint_enforcement: bool = True
    input_validation: bool = True
    output_validation: bool = True

class LoggingConfig(BaseModel):
    """Configuration for logging"""
    level: str = "INFO"
    audit_trail: bool = True
    performance_monitoring: bool = True

class RateLimitConfig(BaseModel):
    """Rate limit configuration"""
    requests_per_minute: int = 60
    burst_limit: int = 10

class ApiConfig(BaseModel):
    """API configuration"""
    rate_limit: RateLimitConfig = RateLimitConfig()
    timeout_seconds: int = 300
    max_payload_size_mb: int = 10

class ToolsConfig(BaseModel):
    """Tools configuration"""
    max_concurrent_tools: int = 5
    timeout_seconds: int = 120
    budget_limit: float = 50.0

class ModelsProviderOpenAIConfig(BaseModel):
    """OpenAI provider configuration"""
    enabled: bool = True
    api_base: str = "https://api.openai.com/v1"

class ModelsProviderAnthropicConfig(BaseModel):
    """Anthropic provider configuration"""
    enabled: bool = True
    api_base: str = "https://api.anthropic.com"

class ModelsProviderGeminiConfig(BaseModel):
    """Gemini provider configuration"""
    enabled: bool = False
    api_base: str = "https://generativelanguage.googleapis.com"

class ModelsProvidersConfig(BaseModel):
    """Model providers configuration"""
    openai: ModelsProviderOpenAIConfig = ModelsProviderOpenAIConfig()
    anthropic: ModelsProviderAnthropicConfig = ModelsProviderAnthropicConfig()
    gemini: ModelsProviderGeminiConfig = ModelsProviderGeminiConfig()

class ModelsConfig(BaseModel):
    """Configuration for model settings"""
    default_model: str = "gpt-4o-mini"
    high_complexity_model: str = "gpt-4o"
    cost_optimized_model: str = "gpt-3.5-turbo"
    providers: ModelsProvidersConfig = ModelsProvidersConfig()

class SystemConfig(BaseModel):
    """System-level configuration"""
    name: str = "AI_AGENT_OS"
    version: str = "1.0.0"
    environment: str = "development"

class Config(BaseSettings):
    """Main configuration model for AI_AGENT_OS"""
    system: SystemConfig = SystemConfig()
    limits: LimitsConfig = LimitsConfig()
    models: ModelsConfig = ModelsConfig()
    routing: RoutingConfig = RoutingConfig()
    traits: TraitsConfig = TraitsConfig()
    memory: MemoryConfig = MemoryConfig()
    safety: SafetyConfig = SafetyConfig()
    logging: LoggingConfig = LoggingConfig()
    api: ApiConfig = ApiConfig()
    tools: ToolsConfig = ToolsConfig()
    
    class Config:
        env_file = ".env"
        env_nested_delimiter = "__"

def load_config(config_path: str = None) -> Config:
    """
    Load configuration from YAML file
    
    Args:
        config_path: Path to config file. If None, looks in default locations
        
    Returns:
        Config object with loaded settings
        
    Raises:
        Exception: If config cannot be loaded or validated
    """
    # Import error handling here to avoid circular imports
    from .error_handling import AIWorkforceError, ErrorCode
    
    if config_path is None:
        # Look for config in common locations
        possible_paths = [
            "backend/app/config.yaml",
            "config.yaml",
            os.path.join(os.path.dirname(__file__), "..", "config.yaml")
        ]
        
        config_path = None
        for path in possible_paths:
            if os.path.exists(path):
                config_path = path
                break
                
        if config_path is None:
            raise AIWorkforceError(
                "Configuration file not found",
                ErrorCode.CONFIGURATION_ERROR,
                {"searched_paths": possible_paths}
            )
    
    try:
        with open(config_path, 'r') as f:
            config_dict = yaml.safe_load(f)
        
        # Convert to Config object
        config = Config(**config_dict)
        return config
        
    except FileNotFoundError:
        raise AIWorkforceError(
            f"Configuration file not found: {config_path}",
            ErrorCode.CONFIGURATION_ERROR,
            {"config_path": config_path}
        )
    except yaml.YAMLError as e:
        raise AIWorkforceError(
            f"Invalid YAML in configuration file: {str(e)}",
            ErrorCode.CONFIGURATION_ERROR,
            {"config_path": config_path, "error": str(e)}
        )
    except Exception as e:
        raise AIWorkforceError(
            f"Failed to load configuration: {str(e)}",
            ErrorCode.CONFIGURATION_ERROR,
            {"config_path": config_path, "error": str(e)}
        )

# Global config instance
_global_config: Config = None

def get_config() -> Config:
    """
    Get the global configuration instance
    
    Returns:
        Config object
    """
    global _global_config
    if _global_config is None:
        _global_config = load_config()
    return _global_config

def reload_config(config_path: str = None) -> Config:
    """
    Reload configuration from file
    
    Args:
        config_path: Path to config file
        
    Returns:
        Reloaded Config object
    """
    global _global_config
    _global_config = load_config(config_path)
    return _global_config
