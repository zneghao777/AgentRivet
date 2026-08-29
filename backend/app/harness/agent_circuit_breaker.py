"""进程内 Agent 连续失败熔断器。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from ..config import settings


@dataclass
class CircuitState:
    failures: int = 0
    opened_at: float | None = None


class AgentCircuitBreaker:
    def __init__(self) -> None:
        self._states: dict[tuple[str, str], CircuitState] = {}
        self._lock = threading.Lock()

    def available(self, tenant_id: str, agent_name: str) -> bool:
        key = (tenant_id, agent_name)
        with self._lock:
            state = self._states.get(key)
            if state is None or state.opened_at is None:
                return True
            if time.monotonic() - state.opened_at >= settings.AGENT_CIRCUIT_BREAKER_COOLDOWN:
                self._states.pop(key, None)
                return True
            return False

    def record_success(self, tenant_id: str, agent_name: str) -> None:
        with self._lock:
            self._states.pop((tenant_id, agent_name), None)

    def record_failure(self, tenant_id: str, agent_name: str) -> bool:
        key = (tenant_id, agent_name)
        with self._lock:
            state = self._states.setdefault(key, CircuitState())
            state.failures += 1
            if state.failures >= settings.AGENT_CIRCUIT_BREAKER_THRESHOLD:
                state.opened_at = time.monotonic()
                return True
            return False

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


agent_circuit_breaker = AgentCircuitBreaker()
