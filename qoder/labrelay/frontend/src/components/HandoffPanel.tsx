import { useState } from "react";

import { api } from "../api";
import type { HandoffDraft, HandoffView } from "../types";

interface Props {
  projectId: string;
  onConfirmed: () => void;
}

interface DraftSnapshot {
  draftId: string;
  draft: HandoffDraft;
}

function textOf(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && typeof (value as { text?: unknown }).text === "string") {
    return (value as { text: string }).text;
  }
  return "";
}

function stateItems(content: Record<string, unknown> | null | undefined) {
  const state = content?.state;
  if (!Array.isArray(state)) return [];
  return state
    .map((item) => {
      const record = item as { text?: unknown; citations?: unknown };
      return {
        text: textOf(record),
        citations: Array.isArray(record.citations) ? record.citations.length : 0,
      };
    })
    .filter((item) => item.text);
}

function omissionItems(content: Record<string, unknown> | null | undefined): string[] {
  const omissions = content?.omissions;
  if (!Array.isArray(omissions)) return [];
  return omissions.map(textOf).filter(Boolean);
}

function formatRef(reference: string): string {
  try {
    const parsed = JSON.parse(reference) as { family?: string; artifact_id?: string; revision?: number };
    return `${parsed.family ?? "handoff"} / ${parsed.artifact_id ?? "artifact"} @ r${parsed.revision ?? "?"}`;
  } catch {
    return reference;
  }
}

function ContentView({ content }: { content: Record<string, unknown> | null | undefined }) {
  const items = stateItems(content);
  const omissions = omissionItems(content);
  return (
    <div className="draft-content">
      <dl className="runtime-list">
        <div>
          <dt>交接目标</dt>
          <dd>{textOf(content?.objective) || "未填写"}</dd>
        </div>
        <div>
          <dt>下一步</dt>
          <dd>{textOf(content?.next_action) || "未填写"}</dd>
        </div>
      </dl>
      {items.length > 0 && (
        <ul className="state-list">
          {items.map((item, index) => (
            <li key={`${index}-${item.text}`}>
              {item.text}
              <small>{item.citations} 个引用</small>
            </li>
          ))}
        </ul>
      )}
      {omissions.length > 0 && <p className="muted">遗留事项：{omissions.join("；")}</p>}
    </div>
  );
}

export function HandoffPanel({ projectId, onConfirmed }: Props) {
  const [objective, setObjective] = useState("完成 baseline 复现并继续下一项实验");
  const [stateText, setStateText] = useState("baseline 已完成，历史指标可复现。");
  const [nextAction, setNextAction] = useState("先检查数据缓存，再运行学习率消融。");
  const [omissions, setOmissions] = useState("尚未验证非默认随机种子。");
  const [draft, setDraft] = useState<DraftSnapshot | null>(null);
  const [confirmed, setConfirmed] = useState<HandoffView | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function prepare(event: React.FormEvent) {
    event.preventDefault();
    setMessage("");
    setBusy(true);
    setConfirmed(null);
    const payload: HandoffDraft = {
      objective,
      state: [{ text: stateText, citations: [] }],
      next_action: nextAction,
      omissions: omissions ? [omissions] : [],
      disposition: "continuable",
    };
    try {
      const preview = await api.prepareHandoff(projectId, payload);
      setDraft({ draftId: preview.draft_id, draft: payload });
      setMessage(`草案已生成，可以直接点开查看：${preview.draft_id}`);
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!draft) return;
    setMessage("");
    setBusy(true);
    try {
      const view = await api.confirmHandoff(projectId, draft.draftId);
      const persisted = await api.getHandoff(projectId);
      setConfirmed(persisted);
      setMessage(`Handoff 已提交：${formatRef(view.handoff_ref)}`);
      onConfirmed();
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  async function loadConfirmed() {
    setMessage("");
    try {
      setConfirmed(await api.getHandoff(projectId));
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : String(cause));
    }
  }

  return (
    <section className="card" id="handoff">
      <h2>5. 学长交接</h2>
      <p className="muted">
        先填写交接内容并生成草案；草案会显示在下方，点「确认并提交」后才写入精确 Handoff Revision。
      </p>
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
          <button type="submit" disabled={busy}>
            {busy ? "生成中…" : "生成交接草案"}
          </button>
          <button type="button" onClick={confirm} disabled={!draft || busy}>
            确认并提交 Handoff
          </button>
          <button type="button" className="link" onClick={loadConfirmed}>
            查看已提交交接
          </button>
        </div>
      </form>

      {draft && (
        <details className="draft-view" open>
          <summary>交接草案 · {draft.draftId}</summary>
          <ContentView
            content={{
              objective: draft.draft.objective,
              next_action: draft.draft.next_action,
              state: draft.draft.state,
              omissions: draft.draft.omissions,
            }}
          />
        </details>
      )}

      {confirmed && (
        <details className="draft-view confirmed" open>
          <summary>已提交交接 · {formatRef(confirmed.handoff_ref)}</summary>
          <p className="muted">
            scope：{confirmed.scope_id} · 内容来自 <code>GET /handoff</code> 的持久化读回
          </p>
          <ContentView content={confirmed.content} />
        </details>
      )}

      {message && <p role="status">{message}</p>}
    </section>
  );
}
