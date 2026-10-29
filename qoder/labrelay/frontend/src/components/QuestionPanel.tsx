import { useEffect, useRef, useState } from "react";

import { api } from "../api";
import type { Citation, RuntimeStatus } from "../types";

interface Props {
  projectId: string;
  disabled?: boolean;
}

interface Exchange {
  id: number;
  question: string;
  answer: string;
  citations: Citation[];
  retrieved: Citation[];
  contextUsed: boolean;
  runtime: RuntimeStatus | null;
  error: string;
  pending: boolean;
}

const SAMPLE_QUESTIONS = [
  "这份资料主要讲了什么？",
  "旧数据缓存会不会影响数据划分？",
  "下一步应该做什么？",
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

function providerLabel(runtime: RuntimeStatus | null): string {
  if (!runtime) return "未读取";
  if (runtime.model_provider === "qoder") return `Qoder · ${runtime.model}`;
  if (runtime.model_provider === "openai_compat") return `OpenAI 兼容 · ${runtime.model}`;
  return `离线确定性 · ${runtime.model}`;
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
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const nextId = useRef(1);
  const lastTurn = useRef<HTMLDivElement>(null);
  const busy = exchanges.some((exchange) => exchange.pending);
  const latest = exchanges.length ? exchanges[exchanges.length - 1] : null;

  useEffect(() => {
    let active = true;
    api
      .runtimeStatus()
      .then((status) => {
        if (active) setRuntime(status);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);

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
      {
        id,
        question: trimmed,
        answer: "",
        citations: [],
        retrieved: [],
        contextUsed: false,
        runtime: null,
        error: "",
        pending: true,
      },
    ]);
    setQuestion("");
    try {
      const response = await api.askQuestion(projectId, trimmed);
      setRuntime(response.runtime);
      patch(id, {
        answer: response.answer,
        citations: response.citations,
        retrieved: response.retrieved,
        contextUsed: response.context_used,
        runtime: response.runtime,
        pending: false,
      });
    } catch (cause) {
      patch(id, {
        error: cause instanceof Error ? cause.message : String(cause),
        pending: false,
      });
    }
  }

  return (
    <section className="card" id="questions">
      <h2>4. 围绕已导入资料提问</h2>
      <p className="muted">
        每次提问都会自动检索本项目资料；配置了 PowerContext 时，还会带上该项目的记忆上下文。回答只使用这些内容。
      </p>

      {disabled ? (
        <p className="muted">先上传或导入资料，再开始提问。</p>
      ) : (
        <div className="qa-layout">
          <div className="qa-chat">
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
                      <span className="muted">自动检索资料并生成回答…</span>
                    ) : exchange.error ? (
                      <span className="error">{exchange.error}</span>
                    ) : (
                      <>
                        <p className="answer">{exchange.answer}</p>
                        <p className="answer-meta">
                          自动检索 {exchange.retrieved.length} 段 ·{" "}
                          {exchange.contextUsed ? "PowerContext 上下文已注入" : "未使用上下文"} ·{" "}
                          {providerLabel(exchange.runtime)}
                        </p>
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
          </div>

          <aside className="qa-side" aria-label="检索与运行环境">
            <div className="qa-side-block">
              <h3>自动检索</h3>
              {latest && latest.pending ? (
                <p className="muted">正在检索…</p>
              ) : latest && latest.retrieved.length ? (
                <ul className="retrieved-list">
                  {latest.retrieved.slice(0, 6).map((item) => (
                    <li key={`${item.asset_id}-${JSON.stringify(item.locator)}`}>
                      <strong>{item.title}</strong>
                      <small>
                        {item.kind} · {formatLocator(item.locator)}
                      </small>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">提问后在这里显示命中的资料片段。</p>
              )}
            </div>

            <div className="qa-side-block">
              <h3>运行环境</h3>
              <dl className="runtime-list">
                <div>
                  <dt>检索</dt>
                  <dd>{runtime?.backend === "seekdb" ? "seekdb" : "内存索引"}</dd>
                </div>
                <div>
                  <dt>上下文</dt>
                  <dd>{runtime?.context === "powercontext" ? "PowerContext" : "内存上下文"}</dd>
                </div>
                <div>
                  <dt>模型</dt>
                  <dd>{providerLabel(runtime)}</dd>
                </div>
              </dl>
              {runtime?.model_provider === "deterministic" && (
                <p className="muted">
                  离线模型只摘录原文并标注“未做推理”。使用 Qoder 内置 Qwen3.8-Flash：在普通终端执行{" "}
                  <code>qodercli login</code>，再用{" "}
                  <code>LABRELAY_MODEL_PROVIDER=qoder</code> 重启后端。
                </p>
              )}
              {runtime?.model_provider === "qoder" && (
                <p className="muted">使用本机 Qoder 登录调用 Qwen3.8-Flash，不经过展示站。</p>
              )}
            </div>
          </aside>
        </div>
      )}
    </section>
  );
}
