import { useState } from "react";

import { api } from "../api";
import type { Citation } from "../types";

interface Props {
  projectId: string;
  disabled?: boolean;
}

function formatLocator(locator: Citation["locator"]): string {
  if ("section" in locator) return `section: ${locator.section}`;
  if ("line_start" in locator) {
    const end = locator.line_end ?? locator.line_start;
    return `L${locator.line_start}–${end}`;
  }
  if ("run_key" in locator) return `run_key: ${locator.run_key}`;
  const entries = Object.entries(locator);
  return entries.length ? entries.map(([key, value]) => `${key}: ${value}`).join(" · ") : "";
}

export function QuestionPanel({ projectId, disabled = false }: Props) {
  const [question, setQuestion] = useState("旧数据缓存会不会影响数据划分？");
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function ask(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    setAnswer("");
    setCitations([]);
    try {
      const response = await api.askQuestion(projectId, question);
      setAnswer(response.answer);
      setCitations(response.citations);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card" id="questions">
      <h2>4. 围绕已导入资料提问</h2>
      <p className="muted">
        回答只使用本项目的检索结果；找不到证据时会明确说“无法确认”，不会补写资料里没有的内容。
      </p>
      <form onSubmit={ask} className="row">
        <label>
          问题
          <input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            disabled={disabled || busy}
            required
          />
        </label>
        <button type="submit" disabled={disabled || busy}>
          {busy ? "检索并回答中…" : "提问"}
        </button>
      </form>
      {disabled && <p className="muted">先上传或导入资料，再开始提问。</p>}
      {answer && <p className="answer">{answer}</p>}
      {citations.length > 0 && (
        <div className="citation-list">
          <strong>引用证据</strong>
          <ul>
            {citations.map((citation) => (
              <li key={`${citation.asset_id}-${JSON.stringify(citation.locator)}`}>
                <div>
                  <span className="badge todo">{citation.kind}</span> {citation.title}
                  <small> · {formatLocator(citation.locator)}</small>
                </div>
                {citation.excerpt && <p className="muted">{citation.excerpt.slice(0, 240)}</p>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {error && (
        <p role="status" className="error">
          {error}
        </p>
      )}
    </section>
  );
}
