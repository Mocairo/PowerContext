import { useState } from "react";

import { api } from "../api";
import type { Project } from "../types";

interface Props {
  onCreated: (project: Project) => void;
}

const DEFAULTS = {
  title: "Aurora 资料分析",
  research_area: "小样本图像分类",
  description: "上传我的资料，检查内容并围绕资料提问。",
};

export function ProjectForm({ onCreated }: Props) {
  const [title, setTitle] = useState(DEFAULTS.title);
  const [area, setArea] = useState(DEFAULTS.research_area);
  const [description, setDescription] = useState(DEFAULTS.description);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function create(input: typeof DEFAULTS) {
    setBusy(true);
    setError("");
    try {
      onCreated(await api.createProject(input));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    void create({ title, research_area: area, description });
  }

  return (
    <form className="card" onSubmit={submit}>
      <h2>1. 创建项目</h2>
      <p className="muted">
        项目建好后，同一页会出现「上传资料」「检索」「提问」三个区域。先用下面的默认信息一键开始，也可以改成自己的课题。
      </p>
      <div className="row">
        <button type="button" onClick={() => void create(DEFAULTS)} disabled={busy}>
          {busy ? "创建中…" : "用默认信息一键开始 →"}
        </button>
      </div>
      <details className="advanced">
        <summary>自定义项目信息</summary>
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
        <button type="submit" disabled={busy}>
          {busy ? "创建中…" : "创建项目"}
        </button>
      </details>
      {error && (
        <p role="status" className="error">
          {error}
        </p>
      )}
    </form>
  );
}
