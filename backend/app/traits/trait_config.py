# backend/app/traits/trait_config.py
"""Trait Configuration System for AI_AGENT_OS"""

from typing import Dict, Any, List
import logging

# Simple logging setup
logger = logging.getLogger(__name__) if 'logging' in globals() else type('Logger', (), {'info': print, 'debug': print, 'warning': print})()

class TraitConfigError(Exception):
    """Custom exception for trait configuration errors"""
    pass

class TraitConfigurator:
    """Generates cognitive traits for agents based on contract context"""
    
    def __init__(self, config: Dict[str, Any] = None):
        """Initialize trait configurator"""
        self.config = config or self._get_default_config()
        
    def _get_default_config(self) -> Dict[str, Any]:
        """Get default trait configuration"""
        return {
            "default_traits": {
                "exploration_bias": 0.5,
                "risk_sensitivity": "neutral",
                "refinement_depth": 2,
                "validation_intensity": "medium",
                "confidence_threshold": 0.7
            },
            "trait_limits": {
                "max_refinement_depth": 5,
                "min_confidence_threshold": 0.5,
                "max_exploration_bias": 1.0
            }
        }
    
    def generate_traits_for_agent(
        self, 
        agent_role: str, 
        contract_context: Dict[str, Any],
        deliverable: str = ""
    ) -> Dict[str, Any]:
        """
        Generate cognitive traits for an agent based on role and context
        
        Args:
            agent_role: Agent's specific role
            contract_context: Execution contract providing context
            deliverable: Specific deliverable this agent handles
            
        Returns:
            Dict of cognitive traits
        """
        try:
            # Start with default traits
            traits = self.config["default_traits"].copy()
            
            # Modify traits based on agent role
            role_traits = self._get_traits_for_role(agent_role, deliverable)
            traits.update(role_traits)
            
            # Modify traits based on contract context
            context_traits = self._get_traits_from_context(contract_context)
            traits.update(context_traits)
            
            # Apply safety limits
            traits = self._apply_trait_limits(traits)
            
            logger.debug(f"Generated traits for {agent_role}: {traits}")
            return traits
            
        except Exception as e:
            logger.warning(f"Trait generation failed, using defaults: {e}")
            return self.config["default_traits"].copy()
    
    def _get_traits_for_role(self, role: str, deliverable: str = "") -> Dict[str, Any]:
        """Get traits based on agent role"""
        role = role.lower()
        deliverable = deliverable.lower()
        
        # Role-based trait adjustments
        if "design" in role or "design" in deliverable:
            return {
                "exploration_bias": 0.8,  # Highly creative
                "refinement_depth": 4,    # Multiple iterations
                "risk_sensitivity": "neutral"
            }
        elif "content" in role or "writer" in role or "copy" in deliverable:
            return {
                "exploration_bias": 0.4,  # Focused writing
                "refinement_depth": 3,    # Editing rounds
                "validation_intensity": "high"  # Accuracy important
            }
        elif "security" in role or "compliance" in deliverable:
            return {
                "exploration_bias": 0.2,  # Conservative
                "risk_sensitivity": "high", # Safety critical
                "validation_intensity": "high"
            }
        elif "analyst" in role or "research" in role:
            return {
                "exploration_bias": 0.7,  # Exploratory analysis
                "refinement_depth": 3,
                "validation_intensity": "high"
            }
        elif "developer" in role or "engineer" in role:
            return {
                "exploration_bias": 0.5,
                "refinement_depth": 4,
                "risk_sensitivity": "medium"
            }
        else:
            # Default balanced traits
            return {
                "exploration_bias": 0.5,
                "refinement_depth": 2,
                "risk_sensitivity": "neutral"
            }
    
    def _get_traits_from_context(self, contract_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get traits based on contract context"""
        traits = {}
        
        # Check constraints
        constraints = contract_context.get("constraints", [])
        if any("budget" in str(c).lower() for c in constraints):
            traits["cost_sensitivity"] = "high"
            
        if any("time" in str(c).lower() or "week" in str(c).lower() for c in constraints):
            traits["speed_orientation"] = "high"
        
        # Check risk factors
        risks = contract_context.get("risk_factors", [])
        if risks:
            traits["caution_level"] = "high"
            
        # Check success criteria
        success_criteria = contract_context.get("success_criteria", [])
        if any("quality" in str(s).lower() or "accur" in str(s).lower() for s in success_criteria):
            traits["precision_focus"] = "high"
            
        return traits
    
    def _apply_trait_limits(self, traits: Dict[str, Any]) -> Dict[str, Any]:
        """Apply safety limits to traits"""
        limits = self.config["trait_limits"]
        
        # Apply numeric limits
        if "refinement_depth" in traits:
            traits["refinement_depth"] = min(
                traits["refinement_depth"], 
                limits["max_refinement_depth"]
            )
            
        if "confidence_threshold" in traits:
            traits["confidence_threshold"] = max(
                traits["confidence_threshold"],
                limits["min_confidence_threshold"]
            )
            
        if "exploration_bias" in traits:
            traits["exploration_bias"] = min(
                traits["exploration_bias"],
                limits["max_exploration_bias"]
            )
        
        # Normalize categorical values
        risk_mapping = {"conservative": "conservative", "neutral": "neutral", "risky": "neutral"}
        if "risk_sensitivity" in traits:
            traits["risk_sensitivity"] = risk_mapping.get(
                traits["risk_sensitivity"], "neutral"
            )
            
        validation_mapping = {"low": "low", "medium": "medium", "high": "medium"}
        if "validation_intensity" in traits:
            traits["validation_intensity"] = validation_mapping.get(
                traits["validation_intensity"], "medium"
            )
        
        return traits

# Convenience function
def generate_agent_traits(
    agent_role: str, 
    contract_context: Dict[str, Any],
    deliverable: str = ""
) -> Dict[str, Any]:
    """
    Generate traits for an agent
    
    Args:
        agent_role: Agent's role
        contract_context: Contract providing context
        deliverable: Specific deliverable
        
    Returns:
        Dict of cognitive traits
    """
    configurator = TraitConfigurator()
    return configurator.generate_traits_for_agent(agent_role, contract_context, deliverable)

# Example usage
if __name__ == "__main__":
    # Test trait generation
    try:
        configurator = TraitConfigurator()
        
        # Sample contract context
        contract_context = {
            "constraints": ["Budget: $5000", "Timeline: 2 weeks"],
            "risk_factors": ["Security concerns", "Mobile responsiveness"],
            "success_criteria": ["High quality design", "Secure implementation"]
        }
        
        # Generate traits for different roles
        designer_traits = configurator.generate_traits_for_agent(
            "Website Designer", contract_context, "modern website design"
        )
        print("Designer traits:", designer_traits)
        
        writer_traits = configurator.generate_traits_for_agent(
            "Content Writer", contract_context, "menu descriptions"
        )
        print("Writer traits:", writer_traits)
        
        print("✅ Trait configuration test passed")
        
    except Exception as e:
        print(f"❌ Trait configuration test failed: {e}")
