"""多 Agent 编排的结构化协议。"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskCard(StrictModel):
    """Agent 间交接的唯一协议。"""

    task_id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[0-9a-f]{32}$")
    parent_task_id: str | None = None
    target_agent: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1)
    context_brief: str = Field(default="", max_length=2000)
    constraints: list[str] = Field(default_factory=list)
    expected_output: str | None = None
    artifacts: list[dict] = Field(default_factory=list)
    status: Literal["pending", "running", "done", "failed"] = "pending"
    result: str | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


class RouterDecision(StrictModel):
    """Router 的结构化决策。"""

    mode: Literal["direct", "orchestrate"]
    target_agent: str = "main"
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1)
    needs_clarification: bool = False
    clarification_question: str | None = None
    clarification_options: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_clarification(self):
        if self.needs_clarification and not self.clarification_question:
            raise ValueError("needs_clarification=true 时必须提供 clarification_question")
        return self


class Plan(StrictModel):
    """Planner 的结构化任务分解。"""

    tasks: list[TaskCard] = Field(min_length=1, max_length=5)
    execution: Literal["parallel", "sequential"] = "parallel"
    summary: str = Field(min_length=1)
