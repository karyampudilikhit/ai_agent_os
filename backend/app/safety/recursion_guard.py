# backend/app/safety/recursion_guard.py
"""Enforces all safety limits and prevents runaway spawning"""

import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum

from ..utils.error_handling import SafetyError, ErrorCode

logger = logging.getLogger(__name__)

class SpawnReason(Enum):
    """Reasons for agent spawning"""
    LOW_COMPLETENESS = "low_completeness"
    LOW_CONFIDENCE = "low_confidence"
    CONSTRAINT_VIOLATION = "constraint_violation"
    USER_REQUEST = "user_request"
    ERROR_RATE_HIGH = "error_rate_high"
    MISSING_DELIVERABLES = "missing_deliverables"

@dataclass
class SpawnAuditEntry:
    """Record of a spawn attempt for auditing"""
    agent_id: str
    reason: str
    timestamp: datetime
    context_summary: Dict[str, Any]

class RecursionGuard:
    """Enforces all safety limits and prevents runaway spawning"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.spawn_timestamps: List[datetime] = []
        self.total_agents_spawned = 0
        self.adaptive_cycle_count = 0
        self.spawned_agent_ids = set()
        self.spawn_audit_log: List[SpawnAuditEntry] = []
        self.fail_safe_triggered = False
        
        logger.info("RecursionGuard initialized with config", extra={"config": config})
    
    def can_spawn(self, execution_context: Dict) -> bool:
        """Check all safety conditions before allowing spawn"""
        checks = [
            self._within_total_agent_limit(execution_context),
            self._within_adaptive_cycle_limit(),
            self._within_spawn_rate_limit(),
            self._within_cost_limit(execution_context),
            self._fail_safe_not_triggered()
        ]
        
        can_spawn = all(checks)
        if not can_spawn:
            logger.warning("Spawn blocked by safety limits", extra={
                "checks": {
                    "total_agent_limit": checks[0],
                    "adaptive_cycle_limit": checks[1],
                    "spawn_rate_limit": checks[2],
                    "cost_limit": checks[3],
                    "fail_safe": checks[4]
                },
                "context": self._get_context_summary(execution_context)
            })
        
        return can_spawn
    
    def record_spawn(self, agent: Dict, reason: SpawnReason, context: Dict):
        """Record spawn attempt for auditing and limits"""
        timestamp = datetime.now()
        self.spawn_timestamps.append(timestamp)
        self.total_agents_spawned += 1
        self.spawned_agent_ids.add(agent["id"])
        
        audit_entry = SpawnAuditEntry(
            agent_id=agent["id"],
            reason=reason.value,
            timestamp=timestamp,
            context_summary=self._get_context_summary(context)
        )
        self.spawn_audit_log.append(audit_entry)
        
        logger.info("Agent spawn recorded", extra={
            "agent_id": agent["id"],
            "reason": reason.value,
            "total_spawned": self.total_agents_spawned,
            "cycle_count": self.adaptive_cycle_count
        })
    
    def increment_adaptive_cycle(self):
        """Track adaptive cycles to prevent infinite loops"""
        self.adaptive_cycle_count += 1
        logger.debug("Adaptive cycle incremented", extra={
            "cycle_count": self.adaptive_cycle_count
        })
    
    def _within_total_agent_limit(self, context: Dict) -> bool:
        total_agents = len(context.get("all_agents", []))
        limit = self.config.get("max_total_agents", 50)
        within_limit = total_agents < limit
        if not within_limit:
            self._trigger_fail_safe("total_agent_limit_exceeded")
        return within_limit
    
    def _within_adaptive_cycle_limit(self) -> bool:
        limit = self.config.get("max_adaptive_cycles", 2)
        within_limit = self.adaptive_cycle_count < limit
        if not within_limit:
            self._trigger_fail_safe("adaptive_cycle_limit_exceeded")
        return within_limit
    
    def _within_spawn_rate_limit(self) -> bool:
        # Clean old timestamps (older than 1 minute)
        cutoff = datetime.now() - timedelta(minutes=1)
        self.spawn_timestamps = [t for t in self.spawn_timestamps if t > cutoff]
        rate_limit = self.config.get("max_spawn_rate_per_minute", 5)
        within_limit = len(self.spawn_timestamps) < rate_limit
        if not within_limit:
            self._trigger_fail_safe("spawn_rate_limit_exceeded")
        return within_limit
    
    def _within_cost_limit(self, context: Dict) -> bool:
        total_cost = context.get("total_cost", 0)
        cost_limit = self.config.get("max_total_cost", 100.0)
        within_limit = total_cost < cost_limit
        if not within_limit:
            self._trigger_fail_safe("cost_limit_exceeded")
        return within_limit
    
    def _fail_safe_not_triggered(self) -> bool:
        # Fail-safe triggered if too many violations
        violation_count = len([log for log in self.spawn_audit_log 
                             if "violation" in log.reason])
        trigger_count = self.config.get("fail_safe_trigger_count", 10)
        fail_safe_condition = violation_count < trigger_count
        if not fail_safe_condition:
            self._trigger_fail_safe("violation_count_exceeded")
        return fail_safe_condition and not self.fail_safe_triggered
    
    def _trigger_fail_safe(self, reason: str):
        """Trigger fail-safe mode to prevent system instability"""
        if not self.fail_safe_triggered:
            self.fail_safe_triggered = True
            logger.critical("Fail-safe triggered", extra={
                "reason": reason,
                "total_agents_spawned": self.total_agents_spawned,
                "adaptive_cycles": self.adaptive_cycle_count
            })
            raise SafetyError(
                f"Fail-safe triggered due to {reason}",
                ErrorCode.RECURSION_LIMIT_EXCEEDED,
                {"reason": reason, "total_agents": self.total_agents_spawned}
            )
    
    def _get_context_summary(self, context: Dict) -> Dict[str, Any]:
        """Get a summary of the execution context for logging"""
        return {
            "total_agents": len(context.get("all_agents", [])),
            "cycle_count": self.adaptive_cycle_count,
            "cumulative_cost": context.get("total_cost", 0),
            "completed_agents": len(context.get("completed_agents", []))
        }
    
    def get_status(self) -> Dict[str, Any]:
        """Get current status of the recursion guard"""
        return {
            "total_agents_spawned": self.total_agents_spawned,
            "adaptive_cycle_count": self.adaptive_cycle_count,
            "spawn_rate_last_minute": len(self.spawn_timestamps),
            "fail_safe_triggered": self.fail_safe_triggered,
            "audit_log_count": len(self.spawn_audit_log)
        }
