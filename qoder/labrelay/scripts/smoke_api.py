"""Drive the LabRelay HTTP API end-to-end against a running backend.

Usage: python scripts/smoke_api.py [base-url] [import-path]
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:49788"
DEFAULT_IMPORT = str(Path(__file__).resolve().parent.parent / "examples" / "demo-project")
IMPORT_PATH = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_IMPORT

failures: list[str] = []


def step(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=60) as client:
        ready = client.get("/health/ready")
        step("health/ready", ready.status_code == 200, str(ready.json()))
        if ready.status_code != 200:
            return 1

        project = client.post(
            "/api/projects",
            json={
                "title": "Aurora 接力",
                "research_area": "深度学习",
                "description": "baseline 复现与扩展实验",
            },
        )
        step("POST /api/projects", project.status_code == 201, project.text[:160])
        project_id = project.json()["id"]

        imported = client.post(f"/api/projects/{project_id}/imports", json={"path": IMPORT_PATH})
        step("POST /imports", imported.status_code == 200, str(imported.json()))
        summary = imported.json()

        search = client.post(
            f"/api/projects/{project_id}/search",
            json={"query": "baseline 训练 学习率", "asset_kinds": [], "limit": 5},
        )
        hits = search.json().get("hits", [])
        step(
            "POST /search",
            search.status_code == 200 and len(hits) > 0,
            f"hits={len(hits)} titles={[h['citation']['title'] for h in hits]}",
        )

        prepare = client.post(
            f"/api/projects/{project_id}/handoff/prepare",
            json={
                "objective": "完成 baseline 复现并继续下一项实验",
                "state": [{"text": "baseline 已完成，历史指标可复现。", "citations": []}],
                "next_action": "先检查数据缓存，再运行学习率消融。",
                "omissions": ["尚未验证非默认随机种子。"],
                "disposition": "continuable",
            },
        )
        step(
            "POST /handoff/prepare",
            prepare.status_code == 200,
            prepare.json().get("draft_id", ""),
        )
        draft_id = prepare.json()["draft_id"]

        confirm = client.post(
            f"/api/projects/{project_id}/handoff/confirm", json={"draft_id": draft_id}
        )
        confirmed = confirm.json() if confirm.status_code == 200 else {}
        step(
            "POST /handoff/confirm",
            confirm.status_code == 200 and bool(confirmed.get("handoff_ref")),
            f"objective={confirmed.get('content', {}).get('objective')}",
        )

        fetched = client.get(f"/api/projects/{project_id}/handoff")
        step(
            "GET /handoff",
            fetched.status_code == 200 and fetched.json().get("content") is not None,
            str(fetched.json().get("handoff_ref"))[:60],
        )

        ack = client.post(
            f"/api/projects/{project_id}/handoff/acknowledge",
            json={
                "receiver": "new-student",
                "status": "accepted",
                "receiver_checks": {
                    "live_state": "confirmed",
                    "capability": "confirmed",
                    "authorization": "confirmed",
                },
            },
        )
        step("POST /handoff/acknowledge", ack.status_code == 200, ack.json().get("status", ""))

        bad_ack = client.post(
            f"/api/projects/{project_id}/handoff/acknowledge", json={"receiver": "new-student"}
        )
        step(
            "accepted acknowledgement without checks is 422",
            bad_ack.status_code == 422,
            str(bad_ack.status_code),
        )

        outcome = client.post(
            f"/api/projects/{project_id}/handoff/outcome",
            json={
                "objective": "完成 baseline 复现",
                "status": "succeeded",
                "summary": "指标与交接一致",
                "observations": ["baseline 指标已核对"],
                "remaining_work": ["学习率消融"],
            },
        )
        step("POST /handoff/outcome", outcome.status_code == 200, outcome.text[:120])

        assessment = client.post(
            f"/api/projects/{project_id}/assessments",
            json={
                "python_level": "advanced",
                "linux_server_level": "basic",
                "paper_level": "read_related",
                "reproduction_level": "basic",
                "hours_per_week": 10,
                "target": "reproduce_baseline",
            },
        )
        step("POST /assessments", assessment.status_code == 201, assessment.text[:160])
        assessment_id = assessment.json()["id"]

        plan = client.post(
            f"/api/projects/{project_id}/plans", json={"assessment_id": assessment_id}
        )
        body = plan.json() if plan.status_code in (200, 201) else {}
        tasks = body.get("tasks", [])
        step(
            "POST /plans",
            plan.status_code in (200, 201) and len(tasks) > 0,
            f"version={body.get('version')} status={body.get('status')} tasks={len(tasks)}",
        )
        if not tasks:
            print(plan.text[:600])
            return 1

        citation_ok = all(task.get("citations") for task in tasks)
        step("every task carries at least one citation", citation_ok, f"{len(tasks)} tasks")

        entry_task_id = tasks[0]["id"]
        todo = [task for task in tasks if task["status"] == "todo"]
        step(
            "activation opens only the stage-0 root",
            [(t["stage"], t["status"]) for t in tasks]
            == [(0, "todo")] + [(stage, "locked") for stage in range(1, 7)],
            str([(t["stage"], t["status"]) for t in tasks]),
        )
        if todo:
            entry_task_id = todo[0]["id"]

        for status, evidence in (
            ("in_progress", None),
            ("done", {"summary": "已按 configs/baseline.yaml 跑通并核对历史指标"}),
        ):
            payload: dict[str, object] = {"status": status}
            if evidence is not None:
                payload["evidence"] = evidence
            patched = client.patch(f"/api/tasks/{entry_task_id}", json=payload)
            step(
                f"PATCH /tasks/{status}",
                patched.status_code == 200,
                str(patched.json().get("status", patched.text[:120])),
            )

        unlocked = client.get(f"/api/plans/{body['id']}")
        stages_now = {t["stage"]: t["status"] for t in unlocked.json().get("tasks", [])}
        step(
            "completing stage 0 unlocks stage 1",
            unlocked.status_code == 200 and stages_now.get(1) == "todo",
            str(stages_now),
        )

        context = client.get(f"/api/projects/{project_id}/learning-context")
        ctx = context.json() if context.status_code == 200 else {}
        step(
            "GET /learning-context",
            context.status_code == 200
            and len(ctx.get("completed", [])) == 1
            and ctx.get("completed", [{}])[0].get("evidence", {}).get("summary"),
            f"stage={ctx.get('current_stage')} progress={ctx.get('progress')} "
            f"memory_source={ctx.get('memory_source')}",
        )

        question = client.post(
            f"/api/projects/{project_id}/questions",
            json={"question": "训练 baseline 用哪个学习率？"},
        )
        q = question.json() if question.status_code == 200 else {}
        step(
            "POST /questions quotes retrieved evidence",
            question.status_code == 200
            and q.get("citations")
            and "摘录" in str(q.get("answer", "")),
            f"answer={str(q.get('answer'))[:70]} citations={len(q.get('citations', []))}",
        )

        empty = client.post(
            "/api/projects",
            json={"title": "空项目", "research_area": "DL", "description": "未导入任何资料"},
        ).json()
        off_topic = client.post(
            f"/api/projects/{empty['id']}/questions", json={"question": "食堂几点开门？"}
        )
        step(
            "a project without evidence refuses instead of guessing",
            off_topic.status_code == 200
            and "无法" in off_topic.json().get("answer", "")
            and off_topic.json().get("citations") == [],
            str(off_topic.json())[:90],
        )

        replan = client.post(
            f"/api/projects/{project_id}/plans/replan", json={"reason": "进度落后"}
        )
        new_body = replan.json() if replan.status_code in (200, 201) else {}
        step(
            "POST /plans/replan",
            replan.status_code in (200, 201)
            and new_body.get("version") == body.get("version", 0) + 1,
            f"version={new_body.get('version')}",
        )
        carried = [t for t in new_body.get("tasks", []) if t.get("source_task_id") == entry_task_id]
        step(
            "completed work is carried into the new version",
            bool(carried) and carried[0]["status"] == "done",
            str([(t["id"], t["status"]) for t in carried]),
        )

        reverse = client.post(f"/api/projects/{project_id}/handoff/draft")
        rv = reverse.json() if reverse.status_code == 200 else {}
        step(
            "POST /handoff/draft (reverse handoff)",
            reverse.status_code == 200 and bool(rv.get("draft")),
            f"disposition={rv.get('draft', {}).get('disposition')}",
        )

        print(
            f"\nimport: assets={summary.get('assets')} chunks={summary.get('chunks')} "
            f"failed={summary.get('failed')} skipped={summary.get('skipped')} "
            f"restricted={summary.get('restricted')}"
        )
        print(f"project: {project_id}")
    if failures:
        print(f"\n{len(failures)} failed steps: {failures}")
        return 1
    print("\nAll smoke steps passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
