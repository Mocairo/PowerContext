import type {
  Assessment,
  HandoffDraft,
  HandoffPreview,
  ImportResult,
  LearningContext,
  LearningPlan,
  LearningTask,
  Project,
  SearchHit,
  AnswerResponse,
  RuntimeStatus,
  UploadResult,
} from "./types";

interface ErrorEnvelope {
  error?: { code?: string; message?: string; details?: unknown };
  request_id?: string;
}

async function fail(response: Response): Promise<never> {
  let detail = `HTTP ${response.status}`;
  let requestId = "";
  try {
    const envelope = (await response.json()) as ErrorEnvelope;
    detail = envelope.error?.message ?? detail;
    requestId = envelope.request_id ?? "";
  } catch {
    detail = `${detail}: ${await response.text()}`;
  }
  throw new Error(requestId ? `${detail} (request ${requestId})` : detail);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) await fail(response);
  return (await response.json()) as T;
}

export const api = {
  createProject: (input: { title: string; research_area: string; description: string }) =>
    request<Project>("/api/projects", { method: "POST", body: JSON.stringify(input) }),
  importProject: (projectId: string, path: string) =>
    request<ImportResult>(`/api/projects/${projectId}/imports`, {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  uploadFiles: async (projectId: string, files: File[]): Promise<UploadResult> => {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    // No Content-Type header: the browser must add the multipart boundary itself.
    const response = await fetch(`/api/projects/${projectId}/uploads`, {
      method: "POST",
      body: form,
    });
    if (!response.ok) await fail(response);
    return (await response.json()) as UploadResult;
  },
  searchProject: (projectId: string, query: string, assetKinds: string[], limit: number) =>
    request<{ hits: SearchHit[] }>(`/api/projects/${projectId}/search`, {
      method: "POST",
      body: JSON.stringify({ query, asset_kinds: assetKinds, limit }),
    }),
  prepareHandoff: (projectId: string, draft: HandoffDraft) =>
    request<HandoffPreview>(`/api/projects/${projectId}/handoff/prepare`, {
      method: "POST",
      body: JSON.stringify(draft),
    }),
  confirmHandoff: (projectId: string, draftId: string) =>
    request<{ handoff_ref: string; scope_id: string }>(
      `/api/projects/${projectId}/handoff/confirm`,
      { method: "POST", body: JSON.stringify({ draft_id: draftId }) }
    ),
  submitAssessment: (projectId: string, input: Omit<Assessment, "id" | "project_id">) =>
    request<Assessment>(`/api/projects/${projectId}/assessments`, {
      method: "POST",
      body: JSON.stringify(input),
    }),
  generatePlan: (projectId: string, assessmentId: string) =>
    request<LearningPlan>(`/api/projects/${projectId}/plans`, {
      method: "POST",
      body: JSON.stringify({ assessment_id: assessmentId }),
    }),
  updateTask: (taskId: string, status: string, evidence?: Record<string, unknown>) =>
    request<LearningTask>(`/api/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify({ status, evidence }),
    }),
  getPlan: (planId: string) => request<LearningPlan>(`/api/plans/${planId}`),
  replanProject: (projectId: string, reason: string) =>
    request<LearningPlan>(`/api/projects/${projectId}/plans/replan`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  learningContext: (projectId: string) =>
    request<LearningContext>(`/api/projects/${projectId}/learning-context`),
  reverseHandoffDraft: (projectId: string) =>
    request<{ draft_id: string; draft: HandoffDraft }>(
      `/api/projects/${projectId}/handoff/draft`,
      { method: "POST" }
    ),
  askQuestion: (projectId: string, question: string) =>
    request<AnswerResponse>(`/api/projects/${projectId}/questions`, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
  runtimeStatus: () => request<RuntimeStatus>("/api/runtime/status"),
};
