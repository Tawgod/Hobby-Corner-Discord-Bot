"""Pure task workflow rules for the future Notes & Tasks service.

No Discord calls, storage writes, or notifications happen here. Callers must persist
the returned Task and deliver the returned Events transactionally/idempotently.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional, Tuple

VALID_MODES = {"direct", "pool"}
ACTIVE = {"pending", "available", "assigned", "in_progress"}
TERMINAL = {"completed", "cancelled"}

class WorkflowError(ValueError):
    pass

@dataclass(frozen=True)
class Task:
    id: str
    title: str
    creator_id: str
    scope: str = "professional"
    assignment_mode: str = "pool"
    assignee_id: Optional[str] = None
    status: str = "available"
    completion_summary: Optional[str] = None
    completed_at: Optional[str] = None
    archived_at: Optional[str] = None
    version: int = 0

@dataclass(frozen=True)
class Event:
    type: str
    task_id: str
    recipient_id: str
    dedupe_key: str

def create_task(task_id: str, title: str, creator_id: str, *,
                scope: str = "professional", assignee_id: Optional[str] = None) -> Tuple[Task, Tuple[Event, ...]]:
    if not all((task_id.strip(), title.strip(), creator_id.strip())):
        raise WorkflowError("Task ID, title and creator are required")
    if scope not in {"personal", "professional"}:
        raise WorkflowError("Invalid task scope")
    if scope == "personal" and assignee_id and assignee_id != creator_id:
        raise WorkflowError("Private personal tasks cannot be assigned to staff")
    mode = "direct" if assignee_id else "pool"
    status = "pending" if assignee_id else "available"
    task = Task(id=task_id, title=title, creator_id=creator_id, scope=scope,
                assignment_mode=mode, assignee_id=assignee_id, status=status)
    events = (Event("assignment_requested", task.id, assignee_id, f"{task.id}:assignment:0"),) if assignee_id else ()
    return task, events

def assign(task: Task, actor_id: str, assignee_id: Optional[str]) -> Tuple[Task, Tuple[Event, ...]]:
    if actor_id != task.creator_id:
        raise WorkflowError("Only creator may reassign")
    if task.status in TERMINAL:
        raise WorkflowError("Cannot assign terminal task")
    if task.scope == "personal" and assignee_id and assignee_id != task.creator_id:
        raise WorkflowError("Cannot delegate private personal task")
    result = replace(task, assignee_id=assignee_id,
                     assignment_mode="direct" if assignee_id else "pool",
                     status="pending" if assignee_id else "available",
                     version=task.version + 1)
    events = (Event("assignment_requested", result.id, assignee_id,
                    f"{result.id}:assignment:{result.version}"),) if assignee_id else ()
    return result, events

def accept(task: Task, actor_id: str) -> Task:
    if task.status == "pending" and task.assignee_id == actor_id:
        return replace(task, status="in_progress", version=task.version + 1)
    if task.status == "available" and task.assignment_mode == "pool" and task.scope == "professional":
        return replace(task, assignee_id=actor_id, status="in_progress", version=task.version + 1)
    raise WorkflowError("Task is not available for this user")

def decline(task: Task, actor_id: str) -> Tuple[Task, Tuple[Event, ...]]:
    if task.status != "pending" or task.assignee_id != actor_id:
        raise WorkflowError("Only pending assignee may decline")
    result = replace(task, assignee_id=None, assignment_mode="pool", status="available", version=task.version + 1)
    return result, (Event("assignment_declined", task.id, task.creator_id,
                          f"{task.id}:declined:{result.version}"),)

def complete(task: Task, actor_id: str, summary: Optional[str] = None,
             now: Optional[datetime] = None) -> Tuple[Task, Tuple[Event, ...]]:
    if task.status not in {"in_progress", "pending", "available"}:
        raise WorkflowError("Task cannot be completed in current status")
    if actor_id != task.creator_id and not (task.status == "in_progress" and task.assignee_id == actor_id):
        raise WorkflowError("Only creator or accepted assignee can complete")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    # Completed and archived in the same transition: history stays searchable in storage.
    result = replace(task, status="completed", completion_summary=(summary or "").strip() or None,
                     completed_at=stamp, archived_at=stamp, version=task.version + 1)
    return result, (Event("task_completed", task.id, task.creator_id,
                          f"{task.id}:completed:{result.version}"),)
