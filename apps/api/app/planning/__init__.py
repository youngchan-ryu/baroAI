"""Pure synthetic assignment engine. No database, authentication, or LLM access."""

from app.planning.baselines import fifo, nearest, urgency_fifo
from app.planning.optimizer import solve_ortools
from app.planning.schemas import PlanningInput, PlanResult, TicketSpec, TravelMatrix, WorkerSpec
from app.planning.validation import PlanValidationError, input_hash, validate_plan

__all__ = [
    "PlanningInput",
    "PlanResult",
    "WorkerSpec",
    "TicketSpec",
    "TravelMatrix",
    "PlanValidationError",
    "fifo",
    "urgency_fifo",
    "nearest",
    "solve_ortools",
    "validate_plan",
    "input_hash",
]
