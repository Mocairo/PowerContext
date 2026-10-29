import { useRef, useState } from "react";

import { api } from "../api";
import type { UploadResult } from "../types";

const MAX_FILES = 20;
const MAX_BYTES = 10 * 1024 * 1024;

const REJECTION_LABELS: Record<string, string> = {
  unsupported_extension: "不支持的格式",
  too_large: "超过 10 MB",
  binary_content: "看起来是二进制文件",
  empty_file: "空文件",
  invalid_name: "文件名不可用",
  unreadable: "无法读取",
};

const STATUS_BADGES: Record<
  UploadResult["files"][number]["status"],
  { label: string; className: string }
> = {
  active: { label: "已索引", className: "done" },
  restricted: { label: "受限 · 未索引", className: "needs_material" },
  failed: { label: "解析失败", className: "blocked" },
  imported: { label: "已上传", className: "todo" },
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

interface Props {
  projectId: string;
  onImported: (result: UploadResult) => void;
}

export function UploadPanel({ projectId, onImported }: Props) {
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  function addFiles(incoming: FileList | File[]) {
    setError("");
    const next = [...files];
    for (const file of Array.from(incoming)) {
      if (next.length >= MAX_FILES) {
        setError(`一次最多上传 ${MAX_FILES} 个文件。`);
        break;
      }
      if (file.size > MAX_BYTES) {
        setError(`${file.name} 超过 10 MB，已跳过。`);
        continue;
      }
      if (!next.some((item) => item.name === file.name && item.size === file.size)) {
        next.push(file);
      }
    }
    setFiles(next);
    if (inputRef.current) inputRef.current.value = "";
  }

  async function upload() {
    if (!files.length || busy) return;
    setBusy(true);
    setError("");
    try {
      const response = await api.uploadFiles(projectId, files);
      setResult(response);
      setFiles([]);
      onImported(response);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <h2>2. 上传资料并分析</h2>
      <p className="muted">
        支持 Markdown、文本、PDF、代码、配置、CSV 等；单个文件 ≤ 10 MB，一次最多 {MAX_FILES}{" "}
        个。含密钥或密码的文件会被标记为受限，不进入检索。
      </p>

      <div
        className={`dropzone${dragging ? " dragging" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          addFiles(event.dataTransfer.files);
        }}
      >
        <p>把文件或文件夹里的资料拖到这里</p>
        <label className="file-picker">
          选择文件
          <input
            ref={inputRef}
            type="file"
            multiple
            onChange={(event) => {
              if (event.target.files) addFiles(event.target.files);
            }}
          />
        </label>
      </div>

      {files.length > 0 && (
        <ul className="file-list">
          {files.map((file) => (
            <li key={`${file.name}-${file.size}`}>
              <span>{file.name}</span>
              <small>{formatSize(file.size)}</small>
              <button
                type="button"
                className="link"
                onClick={() => setFiles(files.filter((item) => item !== file))}
              >
                移除
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="row">
        <button type="button" onClick={upload} disabled={!files.length || busy}>
          {busy ? "上传并解析中…" : `上传并分析（${files.length}）`}
        </button>
      </div>

      {error && (
        <p role="status" className="error">
          {error}
        </p>
      )}

      {result && (
        <div className="import-summary">
          <p role="status">
            已导入 {result.assets} 项资料，生成 {result.chunks} 个检索片段；失败 {result.failed}
            ，跳过 {result.skipped}，受限 {result.restricted}。
          </p>
          <ul className="file-list">
            {result.files.map((file) => {
              const badge = STATUS_BADGES[file.status];
              return (
                <li key={file.name}>
                  <span>{file.name}</span>
                  <small>{formatSize(file.size)}</small>
                  <span className={`badge ${badge.className}`}>{badge.label}</span>
                </li>
              );
            })}
          </ul>
          {result.rejected.length > 0 && (
            <div>
              <strong>未导入</strong>
              <ul>
                {result.rejected.map((item) => (
                  <li key={`${item.name}-${item.reason}`}>
                    {item.name}：{REJECTION_LABELS[item.reason] ?? item.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {result.restricted > 0 && (
            <p className="muted">
              有 {result.restricted} 个文件检测到凭据，已保存但不进入检索：
              {Object.values(result.restricted_reasons).flat().join("、")}
            </p>
          )}
          <p className="muted">现在可以在下面检索证据，或直接跳到「围绕资料提问」。</p>
        </div>
      )}
    </section>
  );
}
