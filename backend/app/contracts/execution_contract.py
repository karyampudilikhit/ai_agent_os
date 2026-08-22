# backend/app/contracts/execution_contract.py
"""Production-Grade Execution Contract Generator for AI_AGENT_OS"""

from typing import Dict, List, Optional, Any
from datetime import datetime
import uuid
import logging

from pydantic import BaseModel, Field

# Fix imports - use absolute paths
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

# Try to import error handling, but don't fail if not available
try:
    from backend.app.utils.error_handling import AIWorkforceError, ErrorCode
    ERROR_HANDLING_AVAILABLE = True
except ImportError:
    ERROR_HANDLING_AVAILABLE = False
    print("Warning: Error handling not available, using standard exceptions")
    
    class ErrorCode:
        CONTRACT_PARSE_FAILED = "CONTRACT_PARSE_FAILED"
        MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
    
    class AIWorkforceError(Exception):
        def __init__(self, message, error_code, context=None):
            super().__init__(message)
            self.message = message
            self.error_code = error_code
            self.context = context or {}

# Try to import JSON utilities
try:
    from backend.app.utils.json_utils import parse_json_safely, validate_json_schema, extract_json_from_response
    JSON_UTILS_AVAILABLE = True
except ImportError:
    JSON_UTILS_AVAILABLE = False
    print("Warning: JSON utilities not available, using basic parsing")
    
    import json
    import re
    
    def parse_json_safely(text: str) -> dict:
        try:
            return json.loads(text)
        except:
            return {"error": "Failed to parse JSON"}
    
    def validate_json_schema(instance, schema):
        return True  # Skip validation in mock mode
    
    def extract_json_from_response(response_text: str) -> Optional[dict]:
        return None

logger = logging.getLogger(__name__) if 'logging' in globals() else type('Logger', (), {'info': print, 'debug': print, 'warning': print, 'error': print})()

class ExecutionContract(BaseModel):
    """Structured execution contract for AI workflows"""
    
    # Unique identifier for this contract
    id: str = Field(default_factory=lambda: f"contract_{uuid.uuid4().hex[:8]}")
    
    # Original user objective
    objective: str
    
    # Structured components
    deliverables: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    success_criteria: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    risk_factors: List[str] = Field(default_factory=list)
    execution_plan: List[str] = Field(default_factory=list)
    
    # Metadata
    created_at: datetime = Field(default_factory=datetime.now)
    refinement_count: int = 0
    confidence_score: float = 0.0
    estimated_complexity: float = 0.5

class ContractGeneratorError(AIWorkforceError):
    """Errors specific to contract generation"""
    pass

class ExecutionContractGenerator:
    """Production-grade contract generator with refinement capabilities"""
    
    def __init__(self, model_adapter=None, config: Optional[Dict[str, Any]] = None):
        """
        Initialize contract generator
        
        Args:
            model_adapter: LLM adapter for contract generation
            config: Configuration dictionary
        """
        self.model_adapter = model_adapter
        self.config = config or self._get_default_config()
        self.refinement_iterations = self.config.get("refinement_iterations", 2)
    
    def _get_default_config(self) -> Dict[str, Any]:
        """Get default configuration"""
        return {
            "refinement_iterations": 2,
            "timeout_seconds": 300,
            "max_tokens": 2000,
            "temperature": 0.7,
            "require_json": True
        }
    
    def generate_contract(self, user_objective: str, max_refinements: Optional[int] = None) -> ExecutionContract:
        """
        Generate execution contract from user objective with refinement
        
        Args:
            user_objective: Natural language objective from user
            max_refinements: Number of refinement iterations (overrides config)
            
        Returns:
            ExecutionContract: Structured contract ready for agent generation
        """
        try:
            # Use provided max_refinements or fall back to config
            refinement_count = max_refinements if max_refinements is not None else self.refinement_iterations
            
            logger.info(f"Generating contract for objective: {user_objective[:100]}...")
            
            # Generate initial draft
            initial_contract = self._generate_initial_draft(user_objective)
            logger.debug(f"Initial contract generated with {len(initial_contract.deliverables)} deliverables")
            
            # Refine contract through iterations
            refined_contract = self._refine_contract(initial_contract, user_objective, refinement_count)
            logger.info(f"Contract refined {refined_contract.refinement_count} times")
            
            return refined_contract
            
        except Exception as e:
            error_msg = f"Failed to generate contract for objective: {user_objective[:100]}..."
            logger.error(error_msg, extra={"error": str(e)} if hasattr(logger, 'error') and 'extra' in logger.error.__code__.co_varnames else {})
            raise ContractGeneratorError(
                error_msg,
                ErrorCode.CONTRACT_PARSE_FAILED,
                {"objective": user_objective, "error": str(e)}
            )
    
    def _generate_initial_draft(self, objective: str) -> ExecutionContract:
        """
        Generate initial contract draft using LLM or mock data
        
        Args:
            objective: User objective
            
        Returns:
            ExecutionContract: Initial draft
        """
        if self.model_adapter:
            # Use real LLM for generation
            prompt = self._create_initial_generation_prompt(objective)
            llm_response = self._call_model_with_retry(prompt)
            contract_data = self._extract_contract_from_response(llm_response)
        else:
            # Mock generation for testing
            contract_data = self._mock_initial_contract(objective)
            logger.warning("Using mock contract generation - no model adapter provided")
        
        # Validate and create contract. `get_schema` is defined on the
        # generator (not the pydantic model) — calling it on the model
        # raises AttributeError and silently drops us to the 1-deliverable
        # default. Use the generator's schema, and only fall back on real
        # validation failures.
        #
        # `get_schema()` requires "objective", but contract_data here is
        # only the LLM-extracted deliverables/constraints/etc — the LLM
        # is never asked to echo the objective back, so this key is
        # never present. Without injecting it, validation fails on every
        # single run regardless of how good the extracted data is, and
        # we silently discard it for the 1-deliverable default every
        # time. Inject the objective we already have before validating.
        validation_data = {**contract_data, "objective": objective}
        try:
            validate_json_schema(validation_data, self.get_schema())
        except Exception as e:
            logger.warning(f"Contract validation failed, using default structure: {e}")
            contract_data = self._create_default_contract_structure(objective)
        
        contract = ExecutionContract(
            objective=objective,
            deliverables=contract_data.get("deliverables", []),
            constraints=contract_data.get("constraints", []),
            success_criteria=contract_data.get("success_criteria", []),
            assumptions=contract_data.get("assumptions", []),
            risk_factors=contract_data.get("risk_factors", []),
            execution_plan=contract_data.get("execution_plan", []),
        )
        
        contract.refinement_count = 0
        contract.confidence_score = 0.6  # Initial confidence
        contract.estimated_complexity = self._estimate_complexity(objective)
        
        logger.debug(f"Initial contract created with confidence {contract.confidence_score:.2f}")
        return contract
    
    def _refine_contract(self, contract: ExecutionContract, original_objective: str, max_iterations: int) -> ExecutionContract:
        """
        Refine contract through multiple iterations
        
        Args:
            contract: Contract to refine
            original_objective: Original user objective
            max_iterations: Number of refinement iterations
            
        Returns:
            ExecutionContract: Refined contract
        """
        current_contract = contract
        
        for i in range(max_iterations):
            try:
                logger.debug(f"Starting refinement iteration {i+1}/{max_iterations}")
                
                if self.model_adapter:
                    # Use real LLM for refinement
                    prompt = self._create_refinement_prompt(current_contract, original_objective, i+1)
                    llm_response = self._call_model_with_retry(prompt)
                    refined_data = self._extract_contract_from_response(llm_response)
                else:
                    # Mock refinement for testing
                    refined_data = self._mock_refined_contract(current_contract, i+1)
                
                # Update contract with refined data
                current_contract = ExecutionContract(
                    id=current_contract.id,
                    objective=original_objective,
                    deliverables=self._merge_lists(current_contract.deliverables, refined_data.get("deliverables", [])),
                    constraints=self._merge_lists(current_contract.constraints, refined_data.get("constraints", [])),
                    success_criteria=self._merge_lists(current_contract.success_criteria, refined_data.get("success_criteria", [])),
                    assumptions=self._merge_lists(current_contract.assumptions, refined_data.get("assumptions", [])),
                    risk_factors=self._merge_lists(current_contract.risk_factors, refined_data.get("risk_factors", [])),
                    execution_plan=self._merge_lists(current_contract.execution_plan, refined_data.get("execution_plan", [])),
                    refinement_count=i+1,
                    confidence_score=min(0.95, current_contract.confidence_score + 0.1),  # Increase confidence
                    estimated_complexity=current_contract.estimated_complexity
                )
                
                logger.debug(f"Completed refinement iteration {i+1}")
                
            except Exception as e:
                # Log error but continue with previous version
                logger.warning(f"Refinement iteration {i+1} failed: {e}")
                current_contract.refinement_count = i  # Update count to actual completed iterations
                break
        
        return current_contract
    
    def _call_model_with_retry(self, prompt: str, max_retries: int = 3) -> str:
        """
        Call model with retry logic
        
        Args:
            prompt: Prompt to send to model
            max_retries: Maximum number of retry attempts
            
        Returns:
            str: Model response
        """
        last_error = None
        
        for attempt in range(max_retries):
            try:
                response = self.model_adapter.chat_completion(
                    prompt,
                    temperature=self.config.get("temperature", 0.7),
                    max_tokens=self.config.get("max_tokens", 2000)
                )
                return response
            except Exception as e:
                last_error = e
                logger.warning(f"Model call attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    import time
                    time.sleep(2 ** attempt)  # Exponential backoff
        
        raise ContractGeneratorError(
            f"Model call failed after {max_retries} attempts",
            ErrorCode.MODEL_CALL_FAILED,
            {"error": str(last_error), "attempts": max_retries}
        )
    
    def _extract_contract_from_response(self, response: str) -> Dict[str, Any]:
        """
        Extract contract data from model response

        Args:
            response: Raw model response

        Returns:
            Dict: Extracted contract data
        """
        # Try to extract JSON from response
        if JSON_UTILS_AVAILABLE:
            extracted_json = extract_json_from_response(response)
            if extracted_json:
                return self._normalize_contract_data(extracted_json)

        # Fallback to parsing entire response
        try:
            return self._normalize_contract_data(parse_json_safely(response))
        except Exception as e:
            logger.warning(f"Failed to parse JSON from response: {e}")
            # Return basic structure from response text
            return {
                "deliverables": [line.strip() for line in response.split('\n') if line.strip()][:5],
                "constraints": [],
                "success_criteria": [],
                "assumptions": [],
                "risk_factors": [],
                "execution_plan": []
            }

    def _normalize_contract_data(self, data: Any) -> Dict[str, Any]:
        """Coerce loose LLM output into a strict Contract-shaped dict.

        Small models often return list items as dicts like
        ``{"task": "Do X"}`` or ``{"step": "Do X"}`` instead of plain
        strings. Pydantic then rejects the whole contract with a
        validation error and we silently fall back to the 1-item
        default. This normalizer walks each list and coerces items
        back into strings using the value of a known key, or a JSON
        dump as a last resort.
        """
        if not isinstance(data, dict):
            # LLM returned a list or scalar — downstream expects a dict,
            # so surface an empty dict rather than propagating the wrong type.
            return {}

        list_fields = (
            "deliverables",
            "constraints",
            "success_criteria",
            "assumptions",
            "risk_factors",
            "execution_plan",
        )
        key_priority = ("task", "step", "item", "description", "text", "name", "goal")

        for field in list_fields:
            items = data.get(field)
            if not isinstance(items, list):
                continue
            coerced = []
            for item in items:
                if isinstance(item, str):
                    if item.strip():
                        coerced.append(item)
                    # empty string — nothing meaningful, drop it
                elif isinstance(item, dict):
                    if not item:
                        continue  # {} has nothing to extract — drop, don't stringify
                    for key in key_priority:
                        if key in item and isinstance(item[key], str) and item[key].strip():
                            coerced.append(item[key])
                            break
                    else:
                        # No known key — flatten non-empty values.
                        vals = [str(v) for v in item.values() if v]
                        if vals:
                            coerced.append(" — ".join(vals))
                        # else: dict had only falsy values — nothing usable, drop it
                else:
                    text = str(item).strip()
                    if text and text not in ("{}", "[]", "None", "null"):
                        coerced.append(text)
            data[field] = coerced

        return data
    
    def _create_initial_generation_prompt(self, objective: str) -> str:
        """Create prompt for initial contract generation"""
        return f"""
        You are an expert project planner. Convert this objective into a structured execution contract.
        
        Objective: {objective}
        
        Generate a comprehensive JSON contract with these exact keys:
        {{
          "deliverables": ["specific, measurable outcomes - be detailed"],
          "constraints": ["limitations, restrictions, boundaries - be specific"],
          "success_criteria": ["measurable success conditions - quantifiable"],
          "assumptions": ["things assumed to be true - explicit"],
          "risk_factors": ["potential problems or obstacles - realistic"],
          "execution_plan": ["high-level steps to achieve deliverables - sequential"]
        }}
        
        Guidelines:
        - Be specific and concrete. Avoid vague statements.
        - Each list should have 3-7 items.
        - Focus on measurable outcomes.
        - Consider realistic constraints and risks.
        - Return ONLY valid JSON - no markdown, no explanations.
        """

    def _create_refinement_prompt(self, contract: ExecutionContract, original_objective: str, iteration: int) -> str:
        """Create prompt for contract refinement"""
        return f"""
        You are reviewing execution contract #{iteration} for improvement.
        
        Original Objective: {original_objective}
        
        Current Contract:
        {contract.model_dump_json(indent=2) if hasattr(contract, 'model_dump_json') else str(contract)}
        
        Improve this contract by:
        1. Making deliverables more specific and measurable
        2. Adding any missing critical constraints
        3. Clarifying ambiguous success criteria
        4. Identifying unstated assumptions
        5. Anticipating additional risk factors
        6. Creating a more detailed execution plan
        
        Return ONLY a JSON object with the same structure:
        {{
          "deliverables": [...],
          "constraints": [...],
          "success_criteria": [...],
          "assumptions": [...],
          "risk_factors": [...],
          "execution_plan": [...]
        }}
        
        Guidelines:
        - Add new items, don't just rewrite existing ones
        - Focus on quality improvements, not quantity increases
        - Return ONLY valid JSON - no markdown, no explanations
        """

    def _mock_initial_contract(self, objective: str) -> Dict[str, Any]:
        """Mock initial contract for testing"""
        return {
            "deliverables": [
                f"Complete analysis of: {objective}",
                "Detailed implementation plan with timeline",
                "Risk assessment and mitigation strategies",
                "Quality assurance framework",
                "Documentation package"
            ],
            "constraints": [
                "Must be completed within 2 weeks",
                "Budget limit: $10,000",
                "No external dependencies beyond stated ones",
                "Compliance with industry standards",
                "Resource availability limitations"
            ],
            "success_criteria": [
                "All deliverables completed and approved",
                "Client/stakeholder sign-off received",
                "Within budget and timeline constraints",
                "Meets quality standards (95% defect-free)",
                "Knowledge transfer completed"
            ],
            "assumptions": [
                "Client will provide necessary information in timely manner",
                "Required resources are available when needed",
                "No major scope changes during execution",
                "Stakeholders are responsive to queries",
                "Technical environment stability maintained"
            ],
            "risk_factors": [
                "Incomplete or unclear requirements",
                "Resource availability conflicts",
                "Technical challenges beyond current expertise",
                "Scope creep or changing requirements",
                "External dependency delays"
            ],
            "execution_plan": [
                "Requirements gathering and analysis",
                "Design and architecture planning",
                "Implementation and development",
                "Testing and quality assurance",
                "Deployment and delivery",
                "Documentation and knowledge transfer"
            ]
        }
    
    def _mock_refined_contract(self, contract: ExecutionContract, iteration: int) -> Dict[str, Any]:
        """Mock refined contract for testing"""
        # Add more specificity in refinement
        refined_deliverables = []
        for deliverable in contract.deliverables:
            refined_deliverables.append(f"[REFINED-{iteration}] {deliverable} - with measurable KPIs")
        
        return {
            "deliverables": refined_deliverables,
            "constraints": contract.constraints + [f"Refinement #{iteration}: Added constraint for quality review process"],
            "success_criteria": [f"Refinement #{iteration}: " + crit for crit in contract.success_criteria],
            "assumptions": contract.assumptions + [f"Refinement #{iteration}: Assumed stakeholder availability for weekly reviews"],
            "risk_factors": contract.risk_factors + [f"Refinement #{iteration}: New risk - regulatory compliance requirements identified"],
            "execution_plan": contract.execution_plan + [f"Refinement #{iteration}: Add quality checkpoint milestone"]
        }
    
    def _merge_lists(self, original: List[str], new: List[str]) -> List[str]:
        """Merge two lists, preferring new items but keeping originals"""
        result = original.copy()
        for item in new:
            if item not in result:
                result.append(item)
        return result
    
    def _estimate_complexity(self, objective: str) -> float:
        """Estimate task complexity (0.0-1.0)"""
        # Simple heuristic based on keywords
        complexity_indicators = {
            'complex': 0.3, 'advanced': 0.3, 'enterprise': 0.4, 'integration': 0.3,
            'simple': -0.2, 'basic': -0.2, 'straightforward': -0.2, 'easy': -0.2,
            'ai': 0.2, 'machine learning': 0.4, 'analytics': 0.3, 'database': 0.2,
            'website': 0.1, 'mobile': 0.2, 'api': 0.2, 'security': 0.3
        }
        
        complexity = 0.5  # Base complexity
        objective_lower = objective.lower()
        
        for indicator, weight in complexity_indicators.items():
            if indicator in objective_lower:
                complexity += weight
        
        # Clamp to 0.0-1.0 range
        return max(0.1, min(0.9, complexity))
    
    def _create_default_contract_structure(self, objective: str) -> Dict[str, Any]:
        """Create default contract structure when parsing fails"""
        return {
            "deliverables": [f"Complete {objective} task"],
            "constraints": ["Meet basic requirements", "Within reasonable timeframe"],
            "success_criteria": ["Task completed satisfactorily"],
            "assumptions": ["Standard working conditions apply"],
            "risk_factors": ["Common project risks apply"],
            "execution_plan": ["Analyze requirements", "Execute task", "Review and deliver"]
        }

    @classmethod
    def get_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "deliverables": {"type": "array", "items": {"type": "string"}},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "success_criteria": {"type": "array", "items": {"type": "string"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "risk_factors": {"type": "array", "items": {"type": "string"}},
                "execution_plan": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["objective", "deliverables", "constraints"]
        }

# Convenience function for easy usage
def generate_execution_contract(
    objective: str, 
    model_adapter=None, 
    max_refinements: Optional[int] = None,
    config: Optional[Dict[str, Any]] = None
) -> ExecutionContract:
    """
    Convenience function to generate execution contract
    
    Args:
        objective: User objective
        model_adapter: LLM adapter (optional)
        max_refinements: Number of refinement iterations
        config: Configuration dictionary
        
    Returns:
        ExecutionContract: Generated contract
    """
    generator = ExecutionContractGenerator(model_adapter, config)
    return generator.generate_contract(objective, max_refinements)

# Example usage:
if __name__ == "__main__":
    # Test the contract generator
    try:
        generator = ExecutionContractGenerator()  # No model adapter = mock mode
        
        # Generate contract
        contract = generator.generate_contract("Create a comprehensive marketing strategy for a tech startup")
        
        print("Generated Contract:")
        print(f"ID: {contract.id}")
        print(f"Objective: {contract.objective}")
        print(f"Deliverables: {contract.deliverables}")
        print(f"Constraints: {contract.constraints}")
        print(f"Refinements: {contract.refinement_count}")
        print(f"Confidence: {contract.confidence_score:.2f}")
        print(f"Complexity: {contract.estimated_complexity:.2f}")
        
    except Exception as e:
        print(f"Error: {e}")
