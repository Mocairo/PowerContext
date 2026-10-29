import { useState } from "react";

import { api } from "../api";
import type { HandoffDraft } from "../types";

interface Props {
  projectId: string;
  onConfirmed: () => void;
}

export function HandoffPanel({ projectId, onConfirmed }: Props) {
  const [objective, setObjective] = useState("完成 baseline 复现并继续下一项实验");
  const [stateText, setStateText] = useState("baseline 已完成，历史指标可复现。");
  const [nextAction, setNextAction] = useState("先检查数据缓存，再运行学习率消融。");
  const [omissions, setOmissions] = useState("尚未验证非默认随机种子。");
  const [draftId, setDraftId] = useState("");
  const [message, setMessage] = useState("");

  async function prepare(event: React.FormEvent) {
    event.preventDefault();
    setMessage("");
    const draft: HandoffDraft = {
      objective,
      state: [{ text: stateText, citations: [] }],
      next_action: nextAction,
      omissions: omissions ? [omissions] : [],
      disposition: "continuable",
    };
    try {
      const preview = await api.prepareHandoff(projectId, draft);
      setDraftId(preview.draft_id);
      setMessage(`草案已准备：${preview.draft_id}`);
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function confirm() {
    if (!draftId) return;
    setMessage("");
    try {
      const view = await api.confirmHandoff(projectId, draftId);
      setMessage(`Handoff 已提交：${view.handoff_ref}`);
      onConfirmed();
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : String(cause));
    }
  }

  return (
    <section className="card">
      <h2>5. 学长交接</h2>
      <form onSubmit={prepare}>
        <label>
          交接目标
          <input value={objective} onChange={(event) => setObjective(event.target.value)} required />
        </label>
        <label>
          已验证状态
          <textarea value={stateText} onChange={(event) => setStateText(event.target.value)} required />
        </label>
        <label>
          下一步
          <input value={nextAction} onChange={(event) => setNextAction(event.target.value)} required />
        </label>
        <label>
          遗留事项
          <input value={omissions} onChange={(event) => setOmissions(event.target.value)} />
        </label>
        <div className="row">
          <button type="submit">生成交接草案</button>
          <button type="button" onClick={confirm} disabled={!draftId}>
            确认并提交 Handoff
          </button>
        </div>
      </form>
      {message && <p role="status">{message}</p>}
    </section>
  );
}
