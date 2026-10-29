import { useState } from "react";

import { api } from "../api";
import type { Assessment, LearningPlan } from "../types";

interface Props {
  projectId: string;
  disabled?: boolean;
  onGenerated: (plan: LearningPlan) => void;
}

const LEVELS = ["none", "basic", "advanced"] as const;

export function AssessmentForm({ projectId, disabled = false, onGenerated }: Props) {
  const [pythonLevel, setPythonLevel] = useState<"none" | "basic" | "advanced">("basic");
  const [linuxLevel, setLinuxLevel] = useState<"none" | "basic" | "advanced">("none");
  const [paperLevel, setPaperLevel] = useState<"none" | "read_related" | "detailed">("none");
  const [reproduction, setReproduction] = useState<"none" | "basic" | "experienced">("none");
  const [hours, setHours] = useState(10);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      const assessment: Assessment = await api.submitAssessment(projectId, {
        python_level: pythonLevel,
        linux_server_level: linuxLevel,
        paper_level: paperLevel,
        reproduction_level: reproduction,
        hours_per_week: hours,
        target: "reproduce_baseline",
      });
      const plan = await api.generatePlan(projectId, assessment.id);
      onGenerated(plan);
      setMessage(`已生成学习路径 v${plan.version}（状态：${plan.status}）`);
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <h2>6. 新人测评与学习路径</h2>
      <form onSubmit={submit}>
        <label>
          Python 基础
          <select value={pythonLevel} onChange={(event) => setPythonLevel(event.target.value as typeof pythonLevel)}>
            {LEVELS.map((level) => (
              <option key={level}>{level}</option>
            ))}
          </select>
        </label>
        <label>
          Linux/服务器
          <select value={linuxLevel} onChange={(event) => setLinuxLevel(event.target.value as typeof linuxLevel)}>
            {LEVELS.map((level) => (
              <option key={level}>{level}</option>
            ))}
          </select>
        </label>
        <label>
          论文阅读
          <select value={paperLevel} onChange={(event) => setPaperLevel(event.target.value as typeof paperLevel)}>
            <option>none</option>
            <option>read_related</option>
            <option>detailed</option>
          </select>
        </label>
        <label>
          复现经验
          <select value={reproduction} onChange={(event) => setReproduction(event.target.value as typeof reproduction)}>
            <option>none</option>
            <option>basic</option>
            <option>experienced</option>
          </select>
        </label>
        <label>
          每周小时
          <input
            type="number"
            min={1}
            max={80}
            value={hours}
            onChange={(event) => setHours(Number(event.target.value))}
          />
        </label>
        <button type="submit" disabled={busy || disabled}>
          {busy ? "生成中…" : "提交测评并生成路径"}
        </button>
      </form>
      {message && <p role="status">{message}</p>}
    </section>
  );
}
