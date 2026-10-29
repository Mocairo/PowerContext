export type AssetKind =
  | "paper"
  | "code"
  | "config"
  | "experiment"
  | "note"
  | "operations"
  | "other";

export interface Citation {
  asset_id: string;
  kind: AssetKind;
  title: string;
  uri: string;
  version: string | null;
  locator: Record<string, string | number>;
  excerpt: string;
}

export interface SearchHit {
  citation: Citation;
  score: number;
}

export interface LearningTask {
  id: string;
  project_id: string;
  plan_id: string;
  stage: number;
  title: string;
  objective: string;
  prerequisite_task_ids: string[];
  estimated_minutes: number;
  citations: Citation[];
  steps: string[];
  pitfalls: string[];
  completion_criteria: string;
  evidence_requirements: string[];
  status:
    | "locked"
    | "todo"
    | "in_progress"
    | "blocked"
    | "done"
    | "skipped"
    | "needs_material";
  task_type: "citation_required" | "self_assessment";
  source_task_id?: string | null;
}

export interface LearningPlan {
  id: string;
  project_id: string;
  version: number;
  status: "draft" | "active" | "superseded";
  assessment_id: string;
  tasks: LearningTask[];
}

export interface Project {
  id: string;
  title: string;
  research_area: string;
  description: string;
  project_scope_id: string;
  learner_scope_id: string;
  active_plan_id: string | null;
  handoff_ref?: string | null;
}

export interface Assessment {
  id: string;
  project_id: string;
  python_level: "none" | "basic" | "advanced";
  linux_server_level: "none" | "basic" | "advanced";
  paper_level: "none" | "read_related" | "detailed";
  reproduction_level: "none" | "basic" | "experienced";
  hours_per_week: number;
  target: "understand" | "reproduce_baseline" | "continue_experiment";
}

export interface HandoffDraft {
  draft_id?: string | null;
  objective: string;
  state: Array<{ text: string; citations: Citation[] }>;
  next_action: string;
  omissions: string[];
  disposition: "continuable" | "blocked" | "complete";
}

export interface HandoffPreview {
  project_id: string;
  draft_id: string;
  objective: string;
  next_action: string;
  disposition: string;
}

export interface LearningContext {
  plan_version: number;
  current_stage: number;
  next_task: LearningTask | null;
  completed: Array<{
    task_id: string;
    title: string;
    status: string;
    evidence: Record<string, unknown>;
    reason: string;
  }>;
  blockers: Array<{ task_id: string; title: string; reason: string }>;
  progress: { done: number; total: number };
  memory_summary: string;
  memory_source: string;
  memory_available: boolean;
}

export interface ImportResult {
  assets: number;
  chunks: number;
  failed: number;
  skipped: number;
  restricted: number;
  restricted_reasons: Record<string, string[]>;
}

export interface UploadedFile {
  name: string;
  size: number;
  status: "active" | "restricted" | "failed" | "imported";
}

export interface RejectedFile {
  name: string;
  reason: string;
}

export interface UploadResult extends ImportResult {
  files: UploadedFile[];
  rejected: RejectedFile[];
}

export interface RuntimeStatus {
  backend: string;
  context: string;
  model: string;
  model_provider: string;
}

export interface AnswerResponse {
  answer: string;
  citations: Citation[];
  retrieved: Citation[];
  context_used: boolean;
  runtime: RuntimeStatus;
}
