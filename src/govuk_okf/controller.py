"""Durable deterministic task controller for the programme DAG."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
DAG_PATH = ROOT / "orchestration" / "dag.yaml"
CONTRACTS_DIR = ROOT / "orchestration" / "task-contracts"

STATES = {
    "queued",
    "leased",
    "running",
    "validating",
    "accepted",
    "retryable",
    "blocked",
    "escalated",
    "failed",
    "superseded",
}
ALLOWED_TRANSITIONS = {
    "queued": {"leased", "blocked", "escalated", "superseded"},
    "leased": {"running", "retryable", "blocked", "failed"},
    "running": {"validating", "retryable", "blocked", "failed"},
    "validating": {"accepted", "retryable", "blocked", "failed"},
    "retryable": {"leased", "failed", "blocked"},
    "blocked": {"queued", "superseded"},
    "escalated": {"queued", "blocked", "superseded"},
    "accepted": {"superseded"},
    "failed": {"superseded"},
    "superseded": set(),
}


class ControllerError(RuntimeError):
    """Raised for an invalid DAG or task transition."""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def load_dag(path: Path = DAG_PATH) -> dict[str, object]:
    dag = json.loads(path.read_text(encoding="utf-8"))
    tasks = dag.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ControllerError("DAG has no tasks")
    ids = [task.get("id") for task in tasks if isinstance(task, dict)]
    if len(ids) != len(tasks) or len(set(ids)) != len(ids):
        raise ControllerError("DAG task IDs are missing or duplicated")
    task_ids = set(ids)
    for task in tasks:
        unknown = set(task.get("depends", [])) - task_ids
        if unknown:
            raise ControllerError(f"{task['id']} has unknown dependencies: {sorted(unknown)}")
        if task["id"] in task.get("depends", []):
            raise ControllerError(f"{task['id']} depends on itself")

    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {task["id"]: task for task in tasks}

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ControllerError(f"DAG cycle at {task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id].get("depends", []):
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in ids:
        visit(task_id)
    return dag


def contract_for(task: dict[str, object]) -> dict[str, object]:
    payload = {
        "schema_version": 1,
        "task_id": task["id"],
        "objective": task["name"].replace("-", " "),
        "dependencies": task.get("depends", []),
        "requirement_ids": task.get("requirements", []),
        "model_profile": task.get("profile", "deterministic"),
        "allowed_sources": ["declared public official sources", "repository inputs"],
        "source_priority": ["official primary", "normative standard", "peer reviewed"],
        "output_artifacts": [task["output"]],
        "acceptance_tests": [f"deterministic validation for {task['id']}", "requirements trace is non-empty"],
        "budgets": {"attempts": 3, "network_attempts": 5, "external_paid_model_gbp": 0},
        "retry_policy": {"max_attempts": 3, "network_backoff": "exponential_with_jitter"},
        "human_gate": task.get("human_gate"),
        "on_block": "continue_independent",
        "prohibited_actions": ["invent evidence", "bypass rights or robots", "publish secrets"],
    }
    payload["idempotency_key"] = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return payload


def materialize_contracts(check: bool = False) -> list[str]:
    dag = load_dag()
    errors: list[str] = []
    for task in dag["tasks"]:
        path = CONTRACTS_DIR / f"{task['id']}.json"
        expected = json.dumps(contract_for(task), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                errors.append(f"{path.relative_to(ROOT)} is missing or out of date")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
    return errors


@dataclass(frozen=True)
class TaskState:
    task_id: str
    state: str
    attempt: int


class Controller:
    def __init__(self, database: Path, events_path: Path) -> None:
        self.database = database
        self.events_path = events_path
        database.parent.mkdir(parents=True, exist_ok=True)
        events_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              task_id TEXT PRIMARY KEY,
              state TEXT NOT NULL,
              attempt INTEGER NOT NULL DEFAULT 0,
              idempotency_key TEXT NOT NULL,
              dependencies TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              event_json TEXT NOT NULL
            );
            """
        )

    def close(self) -> None:
        self.connection.close()

    def bootstrap(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        dag = load_dag()
        with self.connection:
            for task in dag["tasks"]:
                contract = contract_for(task)
                self.connection.execute(
                    "INSERT OR IGNORE INTO tasks(task_id,state,attempt,idempotency_key,dependencies,updated_at) VALUES(?,?,?,?,?,?)",
                    (
                        task["id"],
                        "queued",
                        0,
                        contract["idempotency_key"],
                        canonical_json(task.get("depends", [])),
                        now,
                    ),
                )

    def state(self, task_id: str) -> TaskState:
        row = self.connection.execute(
            "SELECT task_id,state,attempt FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise ControllerError(f"unknown task: {task_id}")
        return TaskState(row["task_id"], row["state"], row["attempt"])

    def ready(self) -> list[str]:
        rows = self.connection.execute("SELECT task_id,state,dependencies FROM tasks ORDER BY task_id").fetchall()
        states = {row["task_id"]: row["state"] for row in rows}
        return [
            row["task_id"]
            for row in rows
            if row["state"] in {"queued", "retryable"}
            and all(states.get(dep) == "accepted" for dep in json.loads(row["dependencies"]))
        ]

    def transition(self, task_id: str, target: str, detail: dict[str, object] | None = None) -> TaskState:
        if target not in STATES:
            raise ControllerError(f"unknown target state: {target}")
        current = self.state(task_id)
        if target not in ALLOWED_TRANSITIONS[current.state]:
            raise ControllerError(f"invalid transition {current.state} -> {target} for {task_id}")
        attempt = current.attempt + (1 if target == "leased" else 0)
        timestamp = datetime.now(timezone.utc).isoformat()
        event = {
            "schema_version": 1,
            "task_id": task_id,
            "from": current.state,
            "to": target,
            "attempt": attempt,
            "timestamp": timestamp,
            "detail": detail or {},
        }
        encoded = canonical_json(event)
        with self.connection:
            self.connection.execute(
                "UPDATE tasks SET state=?,attempt=?,updated_at=? WHERE task_id=?",
                (target, attempt, timestamp, task_id),
            )
            self.connection.execute("INSERT INTO events(event_json) VALUES(?)", (encoded,))
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
        return TaskState(task_id, target, attempt)

    def summary(self) -> dict[str, int]:
        return {
            row["state"]: row["count"]
            for row in self.connection.execute(
                "SELECT state,COUNT(*) AS count FROM tasks GROUP BY state ORDER BY state"
            )
        }

