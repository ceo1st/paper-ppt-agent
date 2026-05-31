from __future__ import annotations

from backend.session.manager import Job, SessionManager
from backend.queue import tasks


def test_huey_job_cancel_marks_terminal(monkeypatch) -> None:
    manager = object.__new__(SessionManager)
    job = Job(
        id="job-1",
        session_id="session-1",
        status="research",
        worker_backend="huey-sqlite",
        message="Generating manuscript from brief",
    )
    manager._jobs = {job.id: job}
    manager._tasks = {}
    manager._ws_queues = {}
    monkeypatch.setattr(manager, "_persist_state", lambda: None)
    monkeypatch.setattr(
        "backend.runtime.worker_process_registry.terminate_job_process_tree",
        lambda _job_id: False,
    )

    assert manager.cancel_job(job.id)

    cancelled = manager.get_job(job.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"


def test_cancelled_queued_job_is_not_started(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "_persisted_job_status", lambda _job_id: "cancelled")
    called = False

    def fake_run(*_args, **_kwargs) -> int:
        nonlocal called
        called = True
        return 1

    monkeypatch.setattr(tasks, "run_generation_subprocess", fake_run)

    assert tasks.run_generation_job.call_local("job-1", "request.json") == 0
    assert not called


def test_refine_job_does_not_point_at_parent_workspace_before_clone(monkeypatch) -> None:
    manager = object.__new__(SessionManager)
    parent = Job(
        id="parent-1",
        session_id="session-1",
        status="complete",
        project_dir="workspaces/parent-1",
        feedback_history=["make it shorter"],
    )
    manager._jobs = {parent.id: parent}
    monkeypatch.setattr(manager, "_persist_state", lambda: None)

    refine = manager.create_refine_job(parent.id, "make slide 3 visual")

    assert refine is not None
    assert refine.project_dir is None
    assert refine.feedback_history == ["make it shorter", "make slide 3 visual"]
