import { useState } from "react";

import type { LearningTask } from "../types";

interface Props {
  task: LearningTask;
  onUpdate: (status: string, evidence?: Record<string, unknown>) => Promise<void>;
}

export function TaskDetail({ task, onUpdate }: Props) {
  const [summary, setSummary] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");

  async function update(status: string, evidence?: Record<string, unknown>) {
    setError("");
    try {
      await onUpdate(status, evidence);
      setSummary("");
      setReason("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  return (
    <section className="card">
      <h2>7. 任务详情</h2>
      <h3>{task.title}</h3>
      <p>{task.objective}</p>
      <p>
        <strong>完成标准：</strong>
        {task.completion_criteria}
      </p>
      <p>
        <strong>需要证据：</strong>
        {task.evidence_requirements.length ? task.evidence_requirements.join("、") : "一份总结"}
      </p>
      {task.steps.length > 0 && (
        <ol>
          {task.steps.map((step, index) => (
            <li key={`${index}-${step}`}>{step}</li>
          ))}
        </ol>
      )}
      {task.citations.length > 0 && (
        <details>
          <summary>参考资料（{task.citations.length}）</summary>
          <ul>
            {task.citations.map((citation) => (
              <li key={`${citation.asset_id}-${JSON.stringify(citation.locator)}`}>
                [{citation.kind}] {citation.title} · {citation.uri} ·{" "}
                {JSON.stringify(citation.locator)}
                <p>{citation.excerpt}</p>
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className="row">
        <button type="button" onClick={() => update("in_progress")}>
          开始
        </button>
        <button type="button" onClick={() => update("blocked", { reason })} disabled={!reason}>
          标记受阻
        </button>
        <button
          type="button"
          onClick={() =>
            update("skipped", { reason, skip_confirmed_by: "learner-ui" })
          }
          disabled={!reason}
        >
          跳过
        </button>
      </div>
      <label>
        完成总结或阻塞原因
        <textarea value={summary} onChange={(event) => setSummary(event.target.value)} />
      </label>
      <label>
        跳过/阻塞理由
        <input value={reason} onChange={(event) => setReason(event.target.value)} />
      </label>
      <button
        type="button"
        disabled={!summary.trim()}
        onClick={() => update("done", { summary })}
      >
        提交完成
      </button>
      {error && (
        <p role="status" className="error">
          {error}
        </p>
      )}
    </section>
  );
}
