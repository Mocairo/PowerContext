import { useState } from "react";

import { api } from "../api";
import type { Citation } from "../types";

interface Props {
  projectId: string;
}

export function QuestionPanel({ projectId }: Props) {
  const [question, setQuestion] = useState("baseline 之前要注意什么？");
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [error, setError] = useState("");

  async function ask(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const response = await api.askQuestion(projectId, question);
      setAnswer(response.answer);
      setCitations(response.citations);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  return (
    <section className="card">
      <h2>8. 围绕资料提问</h2>
      <form onSubmit={ask} className="row">
        <label>
          问题
          <input value={question} onChange={(event) => setQuestion(event.target.value)} required />
        </label>
        <button type="submit">提问</button>
      </form>
      {answer && <p className="answer">{answer}</p>}
      {citations.length > 0 && (
        <ul>
          {citations.map((citation) => (
            <li key={`${citation.asset_id}-${JSON.stringify(citation.locator)}`}>
              [{citation.kind}] {citation.title} · {citation.uri}
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p role="status" className="error">
          {error}
        </p>
      )}
    </section>
  );
}
