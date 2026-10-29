import { useState } from "react";

import { api } from "../api";
import type { SearchHit } from "../types";

interface Props {
  projectId: string;
  disabled?: boolean;
  onCompleted?: () => void;
}

export function SearchPanel({ projectId, disabled = false, onCompleted }: Props) {
  const [query, setQuery] = useState("如何复现 baseline");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [message, setMessage] = useState("");

  async function search(event: React.FormEvent) {
    event.preventDefault();
    setMessage("");
    try {
      const result = await api.searchProject(projectId, query, [], 12);
      setHits(result.hits);
      if (result.hits.length === 0) setMessage("项目资料中没有可确认答案。");
      onCompleted?.();
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : String(cause));
    }
  }

  return (
    <section className="card" id="search">
      <h2>3. 检索证据</h2>
      <form onSubmit={search} className="row">
        <label>
          查询
          <input value={query} onChange={(event) => setQuery(event.target.value)} required />
        </label>
        <button type="submit" disabled={disabled}>
          检索
        </button>
      </form>
      {message && <p role="status">{message}</p>}
      <ul className="results">
        {hits.map((hit) => (
          <li key={`${hit.citation.asset_id}-${JSON.stringify(hit.citation.locator)}`}>
            <strong>
              [{hit.citation.kind}] {hit.citation.title}
            </strong>
            <p>{hit.citation.excerpt}</p>
            <small>
              {hit.citation.uri}
              {hit.citation.version ? ` @ ${hit.citation.version}` : ""} ·{" "}
              {JSON.stringify(hit.citation.locator)}
            </small>
          </li>
        ))}
      </ul>
    </section>
  );
}
