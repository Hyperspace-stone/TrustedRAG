import { buildApiUrl, httpRequest } from "@/shared/api/http";

export type CorpusVariant = {
  name: string;
  extension: string;
  mime_type: string;
  size: number;
  preferred: boolean;
};

export type CorpusMetadata = {
  collection_id: string;
  display_name: string;
  preferred_file_name: string;
  file_type: string;
  mime_type: string;
  size: number;
  preview_supported: boolean;
  preview_kind: "pdf" | "xlsx" | "unavailable";
  preview_available: boolean;
  preview_file_name: string;
  preview_size: number;
  preview_selection_method: "direct" | "origin" | "layout" | "xlsx_fallback";
  preview_url: string;
  variants: CorpusVariant[];
  metadata_url: string;
  content_url: string;
  inline_url: string;
  download_url: string;
};

export type CorpusDocumentBlock =
  | { type: "paragraph"; text: string }
  | { type: "table"; rows: string[][] };

export type CorpusPreview = {
  status: "ok" | "preview_unavailable";
  message?: string;
  collection_id: string;
  display_name: string;
  file_name: string;
  file_type: string;
  kind?: "pdf" | "xlsx" | "unavailable";
  sheet?: string;
  sheets?: string[];
  offset?: number;
  limit?: number;
  total_rows?: number;
  total_columns?: number;
  rows?: string[][];
  located_row?: number | null;
  merged_ranges?: number[][];
  format?: "doc" | "docx";
  total_blocks?: number;
  blocks?: CorpusDocumentBlock[];
  located_block?: number | null;
  page_count?: number | null;
  metadata_url: string;
  content_url: string;
  inline_url: string;
  download_url: string;
};

export function fetchCorpusMetadata(path: string, signal?: AbortSignal): Promise<CorpusMetadata> {
  return httpRequest<CorpusMetadata>(path, { signal });
}

export function fetchCorpusPreview(
  path: string,
  options: {
    variant?: string;
    sheet?: string;
    offset?: number;
    limit?: number;
    signal?: AbortSignal;
  } = {},
): Promise<CorpusPreview> {
  const params = new URLSearchParams();
  if (options.variant) params.set("variant", options.variant);
  if (options.sheet) params.set("sheet", options.sheet);
  if (options.offset) params.set("offset", String(options.offset));
  if (options.limit) params.set("limit", String(options.limit));
  const separator = path.includes("?") ? "&" : "?";
  const url = params.size ? `${path}${separator}${params.toString()}` : path;
  return httpRequest<CorpusPreview>(url, { signal: options.signal });
}

export function corpusAssetUrl(path: string, variant = ""): string {
  if (!variant) return buildApiUrl(path);
  const separator = path.includes("?") ? "&" : "?";
  return buildApiUrl(`${path}${separator}variant=${encodeURIComponent(variant)}`);
}

export function formatCorpusSize(size: number): string {
  if (!Number.isFinite(size) || size < 0) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
