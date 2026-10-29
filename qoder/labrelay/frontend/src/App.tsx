import { useState } from "react";

import { api } from "./api";
import { AssessmentForm } from "./components/AssessmentForm";
import { HandoffPanel } from "./components/HandoffPanel";
import { PlanBoard } from "./components/PlanBoard";
import { ProjectForm } from "./components/ProjectForm";
import { QuestionPanel } from "./components/QuestionPanel";
import { SearchPanel } from "./components/SearchPanel";
import { TaskDetail } from "./components/TaskDetail";
import type {
  ImportResult,
  LearningContext,
  LearningPlan,
  LearningTask,
  Project,
} from "./types";

export default function App() {
  const [project, setProject] = useState<Project | null>(null);
  const [importPath, setImportPath] = useState("../examples/demo-project");
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [plan, setPlan] = useState<LearningPlan | null>(null);
  const [selectedTask, setSelectedTask] = useState<LearningTask | null>(null);
  const [context, setContext] = useState<LearningContext | null>(null);
  const [handoffConfirmed, setHandoffConfirmed] = useState(false);
  const [error, setError] = useState("");

  async function importAssets() {
    if (!project) return;
    setError("");
    try {
      setImportResult(await api.importProject(project.id, importPath));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function updateTask(status: string, evidence?: Record<string, unknown>) {
    if (!selectedTask || !plan) return;
    const taskId = selectedTask.id;
    await api.updateTask(taskId, status, evidence);
    // Unlocking the next task is a server-side effect of this write, so the plan is re-read
    // instead of patching only the task that was clicked; the board would otherwise keep
    // showing a successor as locked.
    const fresh = await api.getPlan(plan.id);
    setPlan(fresh);
    setSelectedTask(fresh.tasks.find((task) => task.id === taskId) ?? null);
    await refreshContext();
  }

  async function refreshContext() {
    if (!project) return;
    setError("");
    try {
      setContext(await api.learningContext(project.id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function replan() {
    if (!project) return;
    const reason = window.prompt("为什么需要重新规划？", "需要补充排障任务");
    if (!reason) return;
    setError("");
    try {
      const next = await api.replanProject(project.id, reason);
      setPlan(next);
      setSelectedTask(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  return (
    <main>
      <header>
        <h1>LabRelay 科研接力</h1>
        <p>把资料、交接和学习路径放在同一个可追溯闭环里。</p>
      </header>

      {!project ? (
        <ProjectForm onCreated={setProject} />
      ) : (
        <>
          <section className="card">
            <h2>项目：{project.title}</h2>
            <p>{project.description}</p>
            <small>Scope：{project.project_scope_id}</small>
          </section>

          <section className="card">
            <h2>2. 导入资料目录</h2>
            <div className="row">
              <label>
                本地路径
                <input
                  value={importPath}
                  onChange={(event) => setImportPath(event.target.value)}
                />
              </label>
              <button type="button" onClick={importAssets}>
                导入
              </button>
            </div>
            {importResult && (
              <p role="status">
                已导入 {importResult.assets} 项资料，{importResult.chunks} 个片段；失败{" "}
                {importResult.failed}，跳过 {importResult.skipped}，受限{" "}
                {importResult.restricted}。
              </p>
            )}
          </section>

          <SearchPanel projectId={project.id} disabled={!importResult} />
          <HandoffPanel
            projectId={project.id}
            onConfirmed={() => {
              setHandoffConfirmed(true);
              void refreshContext();
            }}
          />
          <AssessmentForm
            projectId={project.id}
            disabled={!handoffConfirmed}
            onGenerated={(next) => {
              setPlan(next);
              setSelectedTask(next.tasks[0] ?? null);
              void refreshContext();
            }}
          />
          {plan && (
            <>
              <PlanBoard
                plan={plan}
                selectedTaskId={selectedTask?.id ?? null}
                onSelect={setSelectedTask}
              />
              <div className="row">
                <button type="button" onClick={replan}>
                  重新规划
                </button>
                <button type="button" onClick={refreshContext}>
                  恢复学习进度
                </button>
              </div>
            </>
          )}
          {selectedTask && (
            <TaskDetail
              key={`${selectedTask.id}-${selectedTask.status}`}
              task={selectedTask}
              onUpdate={updateTask}
            />
          )}
          <QuestionPanel projectId={project.id} />
          {context && (
            <section className="card">
              <h2>学习进度</h2>
              <p>
                版本 v{context.plan_version} · 阶段 {context.current_stage} · 完成{" "}
                {context.progress.done}/{context.progress.total}
              </p>
              <pre>{context.memory_summary}</pre>
              <small>记忆来源：{context.memory_source}</small>
            </section>
          )}
        </>
      )}
      {error && (
        <p role="status" className="error">
          {error}
        </p>
      )}
    </main>
  );
}
