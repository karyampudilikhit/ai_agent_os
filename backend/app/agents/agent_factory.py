# backend/app/agents/agent_factory.py
"""Agent Factory for AI_AGENT_OS - Creates agents from contracts"""

from typing import Dict, List, Any, Optional
import logging

# Import our components
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

# Try to import with error handling
try:
    from backend.app.agents.agent_schema import AgentSchema, create_agent_from_schema
    from backend.app.traits.trait_config import generate_agent_traits
    AGENT_COMPONENTS_AVAILABLE = True
except ImportError as e:
    print(f"Warning: Could not import agent components: {e}")
    AGENT_COMPONENTS_AVAILABLE = False
    
    # Create minimal fallback classes
    class AgentSchema:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
    
    def create_agent_from_schema(data):
        return AgentSchema(**data)

try:
    from backend.app.contracts.execution_contract import ExecutionContract
    CONTRACT_AVAILABLE = True
except ImportError:
    CONTRACT_AVAILABLE = False
    print("Warning: ExecutionContract not available")

logger = logging.getLogger(__name__) if 'logging' in globals() else type('Logger', (), {'info': print, 'debug': print, 'warning': print, 'error': print})()

class AgentFactoryError(Exception):
    """Custom exception for agent factory errors"""
    pass

class AgentFactory:
    """Creates AI agents from execution contracts"""
    
    def __init__(self, config: Dict[str, Any] = None):
        """Initialize agent factory"""
        self.config = config or self._get_default_config()
        self.created_agents = []
        
    def _get_default_config(self) -> Dict[str, Any]:
        """Get default factory configuration"""
        return {
            "max_agents": 20,
            "default_complexity": "medium",
            "agent_naming_convention": "{role} for {deliverable}",
            "require_traits": True
        }
    
    def create_agents_from_contract(
        self, 
        contract: Any,  # Can be ExecutionContract or dict
        model_adapter=None
    ) -> List[AgentSchema]:
        """
        Create agents from execution contract
        
        Args:
            contract: Execution contract with deliverables
            model_adapter: Optional model adapter for intelligent agent creation
            
        Returns:
            List of AgentSchema instances
        """
        try:
            # Extract contract data
            if hasattr(contract, 'deliverables'):
                # It's an ExecutionContract object
                contract_data = {
                    "objective": contract.objective,
                    "deliverables": contract.deliverables,
                    "constraints": getattr(contract, 'constraints', []),
                    "risk_factors": getattr(contract, 'risk_factors', []),
                    "success_criteria": getattr(contract, 'success_criteria', []),
                    "id": getattr(contract, 'id', 'unknown')
                }
            elif isinstance(contract, dict):
                # It's a dictionary
                contract_data = contract
            else:
                raise AgentFactoryError("Invalid contract format")
            
            # Validate we have deliverables
            deliverables = contract_data.get("deliverables", [])
            if not deliverables:
                raise AgentFactoryError("No deliverables found in contract")
            
            # Check agent limit
            if len(deliverables) > self.config["max_agents"]:
                logger.warning(f"Too many deliverables ({len(deliverables)}), capping at {self.config['max_agents']}")
                deliverables = deliverables[:self.config["max_agents"]]
            
            # Create agents for each deliverable
            agents = []
            for i, deliverable in enumerate(deliverables):
                agent = self._create_single_agent(
                    deliverable=deliverable,
                    contract_context=contract_data,
                    index=i,
                    model_adapter=model_adapter
                )
                agents.append(agent)
                
                # Track created agents
                self.created_agents.append(agent.id)
                
                logger.info(f"Created agent {agent.id} for: {deliverable[:50]}...")
            
            logger.info(f"Successfully created {len(agents)} agents from contract")
            return agents
            
        except Exception as e:
            error_msg = f"Failed to create agents from contract: {e}"
            logger.error(error_msg)
            raise AgentFactoryError(error_msg)
    
    def _create_single_agent(
        self, 
        deliverable: str, 
        contract_context: Dict[str, Any], 
        index: int,
        model_adapter=None
    ) -> AgentSchema:
        """Create a single agent for a deliverable"""
        try:
            # Determine agent role from deliverable
            role = self._determine_agent_role(deliverable)
            
            # Generate agent name
            name = self.config["agent_naming_convention"].format(
                role=role,
                deliverable=deliverable[:30] + "..." if len(deliverable) > 30 else deliverable
            )
            
            # Generate traits based on role and context
            if AGENT_COMPONENTS_AVAILABLE:
                traits = generate_agent_traits(role, contract_context, deliverable)
            else:
                # Fallback traits
                traits = {
                    "exploration_bias": 0.5,
                    "refinement_depth": 2,
                    "risk_sensitivity": "neutral"
                }
            
            # Determine complexity
            complexity = self._estimate_complexity(deliverable, contract_context)
            
            # Define expected output structure
            expected_output = self._define_expected_output(deliverable, role)
            
            # Create agent data
            agent_data = {
                "name": name,
                "role": role,
                "objective": f"Complete: {deliverable}",
                "expected_output": expected_output,
                "dependencies": [],  # Will be set by orchestrator
                "complexity_level": complexity,
                "traits": traits,
                "status": "pending",
                "parent_contract_id": contract_context.get("id", "unknown")
            }
            
            # Validate and create agent
            agent = create_agent_from_schema(agent_data)
            return agent
            
        except Exception as e:
            error_msg = f"Failed to create agent for deliverable '{deliverable}': {e}"
            logger.error(error_msg)
            raise AgentFactoryError(error_msg)
    
    def _determine_agent_role(self, deliverable: str) -> str:
        """Determine appropriate role based on deliverable"""
        deliverable_lower = deliverable.lower()
        
        # Role mapping based on keywords
        role_mappings = {
            ("design", "mockup", "ui", "ux", "layout"): "Designer",
            ("content", "copy", "text", "description", "writing"): "Content Writer",
            ("code", "develop", "implement", "program", "script"): "Developer",
            ("research", "analysis", "study", "investigate"): "Research Analyst",
            ("strategy", "plan", "approach"): "Strategist",
            ("test", "qa", "quality", "verify"): "QA Specialist",
            ("security", "protect", "safeguard"): "Security Specialist",
            ("marketing", "promotion", "campaign"): "Marketing Specialist",
            ("data", "database", "information"): "Data Specialist"
        }
        
        # Find matching role
        for keywords, role in role_mappings.items():
            if any(keyword in deliverable_lower for keyword in keywords):
                return role
        
        # Default role
        return "Specialist"
    
    def _estimate_complexity(self, deliverable: str, context: Dict[str, Any]) -> str:
        """Estimate task complexity"""
        complexity_score = 0.5  # Base complexity
        
        # Keywords that increase complexity
        complex_keywords = ["advanced", "complex", "enterprise", "integration", 
                          "security", "scalable", "multi", "automated"]
        
        # Keywords that decrease complexity
        simple_keywords = ["basic", "simple", "standard", "common", "template"]
        
        deliverable_lower = deliverable.lower()
        
        # Adjust based on keywords
        for keyword in complex_keywords:
            if keyword in deliverable_lower:
                complexity_score += 0.2
                
        for keyword in simple_keywords:
            if keyword in deliverable_lower:
                complexity_score -= 0.2
        
        # Adjust based on contract context
        constraints = context.get("constraints", [])
        if any("limited" in str(c).lower() or "restrict" in str(c).lower() for c in constraints):
            complexity_score += 0.1
            
        # Clamp to valid range
        complexity_score = max(0.1, min(0.9, complexity_score))
        
        # Convert to level
        if complexity_score < 0.4:
            return "low"
        elif complexity_score < 0.7:
            return "medium"
        else:
            return "high"
    
    def _define_expected_output(self, deliverable: str, role: str) -> Dict[str, Any]:
        """Define expected output structure based on role"""
        role_lower = role.lower()
        
        if "design" in role_lower:
            return {
                "files": "list_of_file_paths_or_links",
                "specifications": "design_specifications_document",
                "assets": "exported_design_assets"
            }
        elif "content" in role_lower or "writer" in role_lower:
            return {
                "text_content": "written_content_in_appropriate_format",
                "metadata": "content_tags_and_categories",
                "sources": "references_and_sources_used"
            }
        elif "develop" in role_lower or "program" in role_lower:
            return {
                "code": "source_code_files",
                "documentation": "technical_documentation",
                "tests": "unit_tests_or_verification"
            }
        elif "research" in role_lower:
            return {
                "findings": "key_findings_and_insights",
                "data": "supporting_data_and_statistics",
                "recommendations": "actionable_recommendations"
            }
        else:
            # Generic output structure
            return {
                "primary_deliverable": "main_output_artifact",
                "supporting_materials": "auxiliary_documents_or_files",
                "validation": "proof_of_completion_or_quality_check"
            }
    
    def get_created_agents(self) -> List[str]:
        """Get list of all created agent IDs"""
        return self.created_agents.copy()
    
    def clear_created_agents(self):
        """Clear the list of created agents"""
        self.created_agents.clear()

# Convenience function
def create_agents_from_contract(contract: Any, model_adapter=None) -> List[AgentSchema]:
    """
    Create agents from contract
    
    Args:
        contract: Execution contract or dict
        model_adapter: Optional model adapter
        
    Returns:
        List of agents
    """
    factory = AgentFactory()
    return factory.create_agents_from_contract(contract, model_adapter)

# Example usage
if __name__ == "__main__":
    # Test agent creation
    try:
        factory = AgentFactory()
        
        # Sample contract data (mock)
        sample_contract = {
            "id": "contract_test123",
            "objective": "Create a marketing strategy for a coffee shop",
            "deliverables": [
                "Customer persona analysis document",
                "Competitive analysis report",
                "Social media content calendar",
                "Email marketing template designs"
            ],
            "constraints": ["Budget: $5000", "Timeline: 3 weeks"],
            "risk_factors": ["Market competition", "Seasonal fluctuations"],
            "success_criteria": ["Increased brand awareness", "Lead generation targets"]
        }
        
        # Create agents
        agents = factory.create_agents_from_contract(sample_contract)
        
        print(f"✅ Created {len(agents)} agents:")
        for agent in agents:
            print(f"   • {agent.name} ({agent.role})")
            print(f"     Traits: {getattr(agent, 'traits', {})}")
            print(f"     Complexity: {getattr(agent, 'complexity_level', 'unknown')}")
            print()
            
    except Exception as e:
        print(f"❌ Agent factory test failed: {e}")
        import traceback
        traceback.print_exc()
