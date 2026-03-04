# backend/app/agents/agent_schema.py
"""Agent Schema Definition and Validation for AI_AGENT_OS"""

from typing import Dict, List, Any, Optional, Union
from datetime import datetime
import uuid
from pydantic import BaseModel, Field

class AgentSchema(BaseModel):
    """Standard schema for AI agents in the system"""
    
    # Unique identifier
    id: str = Field(default_factory=lambda: f"agent_{uuid.uuid4().hex[:8]}")
    
    # Agent identification
    name: str = Field(..., description="Human-readable agent name")
    role: str = Field(..., description="Agent's specific role/responsibility")
    
    # Core mission
    objective: str = Field(..., description="Specific task this agent must accomplish")
    
    # Expected output structure
    expected_output: Dict[str, Any] = Field(
        default={}, 
        description="Schema defining what successful output looks like"
    )
    
    # Dependencies and relationships
    dependencies: List[str] = Field(
        default_factory=list, 
        description="IDs of agents this depends on"
    )
    
    # Execution metadata
    complexity_level: str = Field(
        default="medium", 
        description="Complexity: low/medium/high"
    )
    
    # Cognitive traits (from traits system)
    traits: Dict[str, Union[str, int, float]] = Field(
        default_factory=dict,
        description="Cognitive traits guiding agent behavior"
    )
    
    # Status tracking
    status: str = Field(
        default="pending", 
        description="Agent status: pending/running/completed/failed"
    )
    
    # Metadata
    created_at: datetime = Field(default_factory=datetime.now)
    parent_contract_id: Optional[str] = Field(None, description="Source contract ID")
    
    class Config:
        arbitrary_types_allowed = True

# Validation schema for agent structure
AGENT_SCHEMA_DEFINITION = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "role": {"type": "string"},
        "objective": {"type": "string"},
        "expected_output": {"type": "object"},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "complexity_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "traits": {"type": "object"},
        "status": {"type": "string", "enum": ["pending", "running", "completed", "failed"]},
        "parent_contract_id": {"type": ["string", "null"]}
    },
    "required": ["name", "role", "objective", "expected_output"]
}

def validate_agent_schema(agent_data: Dict[str, Any]) -> bool:
    """Validate agent data against schema"""
    try:
        AgentSchema(**agent_data)
        return True
    except Exception as e:
        print(f"Agent schema validation failed: {e}")
        return False

def create_agent_from_schema(agent_data: Dict[str, Any]) -> AgentSchema:
    """Create validated agent instance from data"""
    try:
        return AgentSchema(**agent_data)
    except Exception as e:
        raise ValueError(f"Invalid agent data: {e}")

# Example agent for testing
EXAMPLE_AGENT = {
    "name": "Website Designer",
    "role": "UI/UX Design Specialist",
    "objective": "Create modern website design for bakery",
    "expected_output": {
        "mockups": "figma_file_link",
        "color_palette": "HEX_codes_list",
        "typography": "font_names_and_usage"
    },
    "dependencies": [],
    "complexity_level": "medium",
    "traits": {
        "exploration_bias": 0.7,
        "refinement_depth": 3,
        "risk_sensitivity": "neutral"
    },
    "status": "pending"
}

if __name__ == "__main__":
    # Test the schema
    try:
        agent = create_agent_from_schema(EXAMPLE_AGENT)
        print("✅ Agent schema test passed")
        print(f"Agent ID: {agent.id}")
        print(f"Agent Role: {agent.role}")
    except Exception as e:
        print(f"❌ Agent schema test failed: {e}")
