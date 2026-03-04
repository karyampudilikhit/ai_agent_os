"""
Execution Contract Module
AI Workforce OS
"""

from typing import Dict, List, Optional, Any
from datetime import datetime
import uuid
import json
import logging

from pydantic import BaseModel, Field


# -----------------------------
# Logging
# -----------------------------

logger = logging.getLogger(__name__)


# -----------------------------
# Exceptions
# -----------------------------

class ContractGenerationError(Exception):
    pass


# -----------------------------
# Data Model
# -----------------------------

class ExecutionContract(BaseModel):
    id: str = Field(default_factory=lambda: f"contract_{uuid.uuid4().hex[:8]}")
    objective: str

    deliverables: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    success_criteria: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    risk_factors: List[str] = Field(default_factory=list)
    execution_plan: List[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    refinement_count: int = 0
    confidence_score: float = 0.0
    estimated_complexity: float = 0.5


# -----------------------------
# Contract Generator
# -----------------------------

class ExecutionContractGenerator:

    def __init__(self, model_adapter):
        """
        model_adapter must expose:
            chat_completion(prompt: str, temperature: float, max_tokens: int) -> str
        """
        self.model = model_adapter
        self.max_refinements = 2
        self.temperature = 0.6
        self.max_tokens = 2000

    # -----------------------------
    # Public Entry Point
    # -----------------------------

    def generate(self, objective: str) -> ExecutionContract:
        """
        Full pipeline:
            1. Initial generation
            2. Refinement passes
            3. Simulation
            4. Targeted final refinement (if needed)
            5. Confidence scoring
        """

        contract = self._generate_initial(objective)

        contract = self._refine(contract, objective, iterations=self.max_refinements)

        simulation = self._simulate(contract)

        if simulation.get("requires_adaptive_processing", False):
            contract = self._refine_post_simulation(
                contract,
                objective,
                simulation
            )

        contract.confidence_score = self._score_contract(contract, simulation)

        return contract

    # -----------------------------
    # Initial Generation
    # -----------------------------

    def _generate_initial(self, objective: str) -> ExecutionContract:
        prompt = self._initial_prompt(objective)

        response = self._call_model(prompt)

        data = self._extract_json(response)

        return ExecutionContract(
            objective=objective,
            deliverables=data.get("deliverables", []),
            constraints=data.get("constraints", []),
            success_criteria=data.get("success_criteria", []),
            assumptions=data.get("assumptions", []),
            risk_factors=data.get("risk_factors", []),
            execution_plan=data.get("execution_plan", []),
            refinement_count=0,
            confidence_score=0.6,
            estimated_complexity=self._estimate_complexity(objective)
        )

    # -----------------------------
    # Refinement
    # -----------------------------

    def _refine(self, contract: ExecutionContract, objective: str, iterations: int):
        current = contract

        for i in range(iterations):
            prompt = self._refinement_prompt(current, objective, i + 1)
            response = self._call_model(prompt)
            data = self._extract_json(response)

            current = ExecutionContract(
                id=current.id,
                objective=objective,
                deliverables=self._merge(current.deliverables, data.get("deliverables", [])),
                constraints=self._merge(current.constraints, data.get("constraints", [])),
                success_criteria=self._merge(current.success_criteria, data.get("success_criteria", [])),
                assumptions=self._merge(current.assumptions, data.get("assumptions", [])),
                risk_factors=self._merge(current.risk_factors, data.get("risk_factors", [])),
                execution_plan=self._merge(current.execution_plan, data.get("execution_plan", [])),
                refinement_count=i + 1,
                confidence_score=current.confidence_score,
                estimated_complexity=current.estimated_complexity
            )

        return current

    # -----------------------------
    # Simulation Phase
    # -----------------------------

    def _simulate(self, contract: ExecutionContract) -> Dict[str, Any]:
        prompt = self._simulation_prompt(contract)

        response = self._call_model(prompt)

        data = self._extract_json(response)

        return data

    # -----------------------------
    # Post-Simulation Refinement
    # -----------------------------

    def _refine_post_simulation(
        self,
        contract: ExecutionContract,
        objective: str,
        simulation_report: Dict[str, Any]
    ):

        prompt = self._post_simulation_refinement_prompt(
            contract,
            objective,
            simulation_report
        )

        response = self._call_model(prompt)
        data = self._extract_json(response)

        return ExecutionContract(
            id=contract.id,
            objective=objective,
            deliverables=data.get("deliverables", contract.deliverables),
            constraints=data.get("constraints", contract.constraints),
            success_criteria=data.get("success_criteria", contract.success_criteria),
            assumptions=data.get("assumptions", contract.assumptions),
            risk_factors=data.get("risk_factors", contract.risk_factors),
            execution_plan=data.get("execution_plan", contract.execution_plan),
            refinement_count=contract.refinement_count + 1,
            confidence_score=contract.confidence_score,
            estimated_complexity=contract.estimated_complexity
        )

    # -----------------------------
    # Prompts
    # -----------------------------

    def _initial_prompt(self, objective: str) -> str:
        return f"""
You are a senior execution architect.

Think step-by-step internally.
Output ONLY valid JSON.

Objective:
{objective}

Return JSON:

{{
  "deliverables": ["..."],
  "constraints": ["..."],
  "success_criteria": ["..."],
  "assumptions": ["..."],
  "risk_factors": ["..."],
  "execution_plan": ["..."]
}}

Rules:
- 3–7 items per list
- Measurable deliverables
- Measurable success criteria
- Concrete constraints
- Logical execution plan
- No vague wording
- JSON only
"""

    def _refinement_prompt(self, contract: ExecutionContract, objective: str, iteration: int) -> str:
        return f"""
You are refining execution contract iteration {iteration}.

Objective:
{objective}

Current contract:
{contract.model_dump_json(indent=2)}

Improve measurability, constraint clarity, sequencing, and risk realism.

Return JSON with same structure.
JSON only.
"""

    def _simulation_prompt(self, contract: ExecutionContract) -> str:
        return f"""
You are stress-testing this execution contract.

Do NOT rewrite it.
Only evaluate it.

Contract:
{contract.model_dump_json(indent=2)}

Return JSON:

{{
  "completeness_score": 0.0-1.0,
  "constraint_conflicts": [],
  "missing_dependencies": [],
  "unmeasurable_success_criteria": [],
  "risk_underestimation": [],
  "execution_plan_weaknesses": [],
  "requires_adaptive_processing": true|false
}}

JSON only.
"""

    def _post_simulation_refinement_prompt(
        self,
        contract: ExecutionContract,
        objective: str,
        simulation: Dict[str, Any]
    ) -> str:

        return f"""
You are fixing a contract based on simulation results.

Objective:
{objective}

Current contract:
{contract.model_dump_json(indent=2)}

Simulation report:
{json.dumps(simulation, indent=2)}

Fix only the detected weaknesses.
Do not expand scope.
Return JSON with same structure.
JSON only.
"""

    # -----------------------------
    # Helpers
    # -----------------------------

    def _call_model(self, prompt: str) -> str:
        try:
            return self.model.chat_completion(
                prompt,
                temperature=self.temperature,
                max_tokens=self.max_tokens
            )
        except Exception as e:
            raise ContractGenerationError(f"Model call failed: {str(e)}")

    def _extract_json(self, text: str) -> Dict[str, Any]:
        try:
            return json.loads(text)
        except:
            # Attempt to extract JSON block
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(text[start:end])
            raise ContractGenerationError("Invalid JSON from model")

    def _merge(self, original: List[str], new: List[str]) -> List[str]:
        result = original.copy()
        for item in new:
            if item not in result:
                result.append(item)
        return result

    def _score_contract(self, contract: ExecutionContract, simulation: Dict[str, Any]) -> float:
        score = 0.5

        if len(contract.deliverables) >= 3:
            score += 0.1
        if len(contract.constraints) >= 3:
            score += 0.1
        if len(contract.execution_plan) >= 3:
            score += 0.1
        if not simulation.get("constraint_conflicts"):
            score += 0.1
        if simulation.get("completeness_score", 0) > 0.75:
            score += 0.1

        return min(score, 0.95)

    def _estimate_complexity(self, objective: str) -> float:
        base = 0.5
        keywords = ["enterprise", "ai", "integration", "security", "multi", "optimization"]
        for k in keywords:
            if k in objective.lower():
                base += 0.05
        return min(base, 0.9)


# -----------------------------
# Convenience
# -----------------------------

def generate_execution_contract(objective: str, model_adapter) -> ExecutionContract:
    generator = ExecutionContractGenerator(model_adapter)
    return generator.generate(objective)