import { useState } from "react";

import { api } from "../api";
import type { Project } from "../types";

interface Props {
  onCreated: (project: Project) => void;
}

export function ProjectForm({ onCreated }: Props) {
  const [title, setTitle] = useState("");
  const [area, setArea] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    try {
      onCreated(
        await api.createProject({
          title,
          research_area: area,
          description,
        })
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  return (
    <form className="card" onSubmit={submit}>
      <h2>1. 创建交接项目</h2>
      <label>
        项目名称
        <input value={title} onChange={(event) => setTitle(event.target.value)} required />
      </label>
      <label>
        研究领域
        <input value={area} onChange={(event) => setArea(event.target.value)} required />
      </label>
      <label>
        项目目标
        <textarea
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          required
        />
      </label>
      <button type="submit">创建项目</button>
      {error && (
        <p role="status" className="error">
          {error}
        </p>
      )}
    </form>
  );
}
