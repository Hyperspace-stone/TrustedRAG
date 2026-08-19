import { buildApiUrl, httpRequest } from "@/shared/api/http";

export type KbFileEntry = {
  name: string;
  display_name?: string;
  path?: string;
  size?: number;
  mtime?: number;
  category: string;
  type?: string;
  count?: number;
  file_count?: number;
  owner_user_id?: string;
  visibility?: "private" | "public" | "shared";
  visible_users?: string[];
  can_manage?: boolean;
  can_view?: boolean;
  pdf_available?: boolean;
};

export type KbPdfMetadata = {
  collection_id: string;
  display_name: string;
  file_name: string;
  size: number;
  pdf_url: string;
};

export type KbFilesResponse = {
  raw: KbFileEntry[];
  corpus: KbFileEntry[];
  chunks: KbFileEntry[];
  index: KbFileEntry[];
  db_status?: string;
  db_config?: Record<string, unknown>;
};

export type KbVisibilityPayload = {
  collection_name: string;
  owner_user_id?: string;
  visibility: "private" | "public" | "shared";
  visible_users: string[];
  can_manage?: boolean;
  can_view?: boolean;
};

export type KbBuildStatus =
  | "queued"
  | "parsing"
  | "rendering"
  | "indexing"
  | "validating"
  | "publishing"
  | "succeeded"
  | "failed"
  | "interrupted"
  | "deleted";

export type KbBuildTask = {
  task_id: string;
  owner_user_id?: string;
  original_name: string;
  display_name: string;
  kb_id: string;
  visibility: "private" | "public" | "shared";
  size: number;
  sha256?: string;
  status: KbBuildStatus;
  stage: string;
  progress: number;
  message: string;
  error_code?: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
};

export type KbBuildUploadError = {
  file_name: string;
  code: string;
  message: string;
};

export type KbBuildBatchResponse = {
  tasks: KbBuildTask[];
  errors: KbBuildUploadError[];
};

export async function fetchKbFiles(): Promise<KbFilesResponse> {
  return httpRequest<KbFilesResponse>("/api/kb/files");
}

export async function fetchKbPdfMetadata(collectionName: string): Promise<KbPdfMetadata> {
  return httpRequest<KbPdfMetadata>(
    `/api/kb/${encodeURIComponent(collectionName)}/pdf/metadata`,
  );
}

export function kbPdfUrl(collectionName: string): string {
  return `${buildApiUrl(`/api/kb/${encodeURIComponent(collectionName)}/pdf`)}#toolbar=1&navpanes=1&view=FitH`;
}

function kbBuildErrorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") return fallback;
  const record = payload as Record<string, unknown>;
  const detail = record.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const message = (detail as Record<string, unknown>).message;
    if (typeof message === "string" && message.trim()) return message;
  }
  const message = record.message;
  return typeof message === "string" && message.trim() ? message : fallback;
}

export async function uploadKbFiles(
  files: File[],
  visibility: "private" | "public" | "shared" = "private",
): Promise<KbBuildBatchResponse> {
  if (!files.length) throw new Error("请选择要上传的文件。");
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));
  formData.append("visibility", visibility);
  const response = await fetch(buildApiUrl("/api/kb/builds/batch"), {
    method: "POST",
    credentials: "include",
    body: formData,
  });
  if (!response.ok) {
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    throw new Error(kbBuildErrorMessage(payload, `上传失败（HTTP ${response.status}）`));
  }
  return response.json() as Promise<KbBuildBatchResponse>;
}

export async function fetchKbBuilds(): Promise<KbBuildTask[]> {
  return httpRequest<KbBuildTask[]>("/api/kb/builds");
}

export async function fetchKbBuild(taskId: string): Promise<KbBuildTask> {
  return httpRequest<KbBuildTask>(`/api/kb/builds/${encodeURIComponent(taskId)}`);
}

export async function retryKbBuild(taskId: string): Promise<KbBuildTask> {
  return httpRequest<KbBuildTask>(`/api/kb/builds/${encodeURIComponent(taskId)}/retry`, {
    method: "POST",
    body: {},
  });
}

export async function deleteKbFile(category: string, filename: string): Promise<unknown> {
  return httpRequest<unknown>(`/api/kb/files/${encodeURIComponent(category)}/${encodeURIComponent(filename)}`, {
    method: "DELETE",
  });
}

export async function syncMemoryToKb(indexMode: "append" | "overwrite" = "append"): Promise<{
  task_id: string;
  status: string;
}> {
  return httpRequest<{ task_id: string; status: string }>("/api/kb/sync-memory", {
    method: "POST",
    body: { index_mode: indexMode },
  });
}

export async function clearMemoryVectors(): Promise<{
  status: string;
  cleared_count?: number;
  collection_name?: string;
}> {
  return httpRequest<{
    status: string;
    cleared_count?: number;
    collection_name?: string;
  }>("/api/kb/clear-memory", {
    method: "POST",
    body: {},
  });
}

export async function fetchKbTaskStatus(taskId: string): Promise<Record<string, unknown>> {
  return httpRequest<Record<string, unknown>>(`/api/kb/status/${encodeURIComponent(taskId)}`);
}

export async function fetchKbConfig(): Promise<Record<string, unknown>> {
  return httpRequest<Record<string, unknown>>("/api/kb/config");
}

export async function saveKbConfig(payload: Record<string, unknown>): Promise<{ status: string }> {
  return httpRequest<{ status: string }>("/api/kb/config", {
    method: "POST",
    body: payload,
  });
}

export async function listKbVisibilityUsers(): Promise<{ users: string[] }> {
  return httpRequest<{ users: string[] }>("/api/kb/visibility/users");
}

export async function fetchKbVisibility(collectionName: string): Promise<KbVisibilityPayload> {
  return httpRequest<KbVisibilityPayload>(`/api/kb/visibility/${encodeURIComponent(collectionName)}`);
}

export async function saveKbVisibility(
  collectionName: string,
  payload: {
    visibility: "private" | "public" | "shared";
    visible_users?: string[];
  },
): Promise<KbVisibilityPayload> {
  return httpRequest<KbVisibilityPayload>(`/api/kb/visibility/${encodeURIComponent(collectionName)}`, {
    method: "POST",
    body: payload,
  });
}

export async function inspectKbFolder(
  category: "raw" | "corpus" | "chunks",
  name: string,
): Promise<{ files: Array<{ name: string; size: number }> }> {
  const query = `category=${encodeURIComponent(category)}&name=${encodeURIComponent(name)}`;
  return httpRequest<{ files: Array<{ name: string; size: number }> }>(`/api/kb/files/inspect?${query}`);
}

export async function clearKbStaging(): Promise<Record<string, unknown>> {
  return httpRequest<Record<string, unknown>>("/api/kb/staging/clear", {
    method: "POST",
    body: {},
  });
}

export async function runKbTask(payload: {
  pipeline_name: string;
  target_file: string;
  collection_name?: string;
  index_mode?: "new" | "append" | "overwrite";
  chunk_backend?: string;
  tokenizer_or_token_counter?: string;
  chunk_size?: number;
  use_title?: boolean;
  emb_api_key?: string;
  emb_base_url?: string;
  emb_model_name?: string;
}): Promise<{ status: string; task_id: string; message?: string }> {
  return httpRequest<{ status: string; task_id: string; message?: string }>("/api/kb/run", {
    method: "POST",
    body: payload,
  });
}
