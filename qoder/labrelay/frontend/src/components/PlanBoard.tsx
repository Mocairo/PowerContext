import type { LearningPlan, LearningTask } from "../types";

interface Props {
  plan: LearningPlan;
  selectedTaskId: string | null;
  onSelect: (task: LearningTask) => void;
}

const STATUS_LABELS: Record<string, string> = {
  locked: "未解锁",
  todo: "可开始",
  in_progress: "进行中",
  blocked: "受阻",
  done: "已完成",
  skipped: "已跳过",
  needs_material: "缺资料",
};

export function PlanBoard({ plan, selectedTaskId, onSelect }: Props) {
  const stages = [...new Set(plan.tasks.map((task) => task.stage))].sort((a, b) => a - b);
  return (
    <section className="card" id="plan">
      <h2>7. 学习路径 v{plan.version}</h2>
      {stages.map((stage) => (
        <div key={stage} className="stage">
          <h3>阶段 {stage}</h3>
          <ul>
            {plan.tasks
              .filter((task) => task.stage === stage)
              .map((task) => (
                <li key={task.id} className={task.id === selectedTaskId ? "selected" : ""}>
                  <button type="button" className="link" onClick={() => onSelect(task)}>
                    {task.title}
                  </button>
                  <span className={`badge ${task.status}`}>{STATUS_LABELS[task.status]}</span>
                  <small>{task.estimated_minutes} 分钟</small>
                </li>
              ))}
          </ul>
        </div>
      ))}
    </section>
  );
}
