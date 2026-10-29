import { useEffect, useRef, useState } from "react";

import { api } from "../api";
import type { Citation } from "../types";

interface Props {
  projectId: string;
  disabled?: boolean;
}

interface Exchange {
  id: number;
  question: string;
  answer: string;
  citations: Citation[];
  error: string;
  pending: boolean;
}

const SAMPLE_QUESTIONS = [
  "这份资料主要讲了什么？",
  "旧数据缓存会不会影响数据划分？",
  "下一步应该做什么？"
];

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

function Citations({ citations }: { citations: Citation[] }) {
  if (!citations.length) return null;
  return (
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
  );
}

export function QuestionPanel({ projectId, disabled = false }: Props) {
  const [question, setQuestion] = useState("");
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const nextId = useRef(1);
  const lastTurn = useRef<HTMLDivElement>(null);
  const busy = exchanges.some((exchange) => exchange.pending);

  useEffect(() => {
    lastTurn.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [exchanges]);

  function patch(id: number, update: Partial<Exchange>) {
    setExchanges((current) =>
      current.map((exchange) => (exchange.id === id ? { ...exchange, ...update } : exchange))
    );
  }

  async function send(text: string, event?: React.FormEvent) {
    event?.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    const id = nextId.current++;
    setExchanges((current) => [
      ...current,
      { id, question: trimmed, answer: "", citations: [], error: "", pending: true }
    ]);
    setQuestion("");
    try {
      const response = await api.askQuestion(projectId, trimmed);
      patch(id, {
        answer: response.answer,
        citations: response.citations,
        pending: false
      });
    } catch (cause) {
      patch(id, {
        error: cause instanceof Error ? cause.message : String(cause),
        pending: false
      });
    }
  }

  return (
    <section className="card" id="questions">
      <h2>4. 围绕已导入资料提问</h2>
      <p className="muted">
        回答只使用本项目的检索结果；找不到证据时会明确说“无法确认”，不会补写资料里没有的内容。
      </p>

      {disabled ? (
        <p className="muted">先上传或导入资料，再开始提问。</p>
      ) : (
        <>
          <div className="chat" aria-live="polite">
            {exchanges.length === 0 && (
              <p className="muted">还没有提问。可以直接输入，或点下面的示例问题。</p>
            )}
            {exchanges.map((exchange, index) => (
              <div
                key={exchange.id}
                className="chat-turn"
                ref={index === exchanges.length - 1 ? lastTurn : undefined}
              >
                <p className="chat-question">{exchange.question}</p>
                <div className="chat-answer">
                  {exchange.pending ? (
                    <span className="muted">检索资料并生成回答…</span>
                  ) : exchange.error ? (
                    <span className="error">{exchange.error}</span>
                  ) : (
                    <>
                      <p className="answer">{exchange.answer}</p>
                      <Citations citations={exchange.citations} />
                    </>
                  )}
                </div>
              </div>
            ))}
          </div>

          <div className="sample-row">
            {SAMPLE_QUESTIONS.map((sample) => (
              <button
                key={sample}
                type="button"
                className="chip"
                onClick={() => setQuestion(sample)}
                disabled={busy}
              >
                {sample}
              </button>
            ))}
          </div>

          <form onSubmit={(event) => void send(question, event)} className="row">
            <label>
              问题
              <input
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                disabled={busy}
                placeholder="例如：这份资料里的 baseline 指标是多少？"
                required
              />
            </label>
            <button type="submit" disabled={busy}>
              {busy ? "回答中…" : "发送"}
            </button>
          </form>
        </>
      )}
    </section>
  );
}
