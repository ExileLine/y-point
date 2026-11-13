import { request } from "./api";

export type KnowledgeMode = "keyword" | "hybrid";
export type KnowledgeProcessingMode = "auto" | "original" | "enhanced";
export type KnowledgeDocumentOptions = { processing_mode?: KnowledgeProcessingMode | null; sheet_names?: string[]; header_rows?: number; pdf_mode?: "auto" | "text" | "ocr" };
export type KnowledgeDraft = {
  name: string;
  description: string | null;
  search_mode: KnowledgeMode;
  embedding_model_id: number | null;
  chunk_size: number;
  chunk_overlap: number;
  embedding_token_budget: number | null;
  embedding_cost_budget: string | null;
  processing_mode: KnowledgeProcessingMode;
  processing_model_id: number | null;
  processing_token_budget: number | null;
  processing_cost_budget: string | null;
};
export type KnowledgeBase = KnowledgeDraft & {
  id: number;
  owner_id: number;
  revision: number;
  active_index_id: number | null;
  create_time: string;
  update_time: string;
};
export type KnowledgePage = { items: KnowledgeBase[]; total: number; page: number; page_size: number };
export type KnowledgeDocument = {
  id: number;
  knowledge_base_id: number;
  filename: string;
  current_version: number;
  processing_options?: KnowledgeDocumentOptions;
  update_time: string;
};
export type KnowledgeJobState = "uploaded" | "parsing" | "chunking" | "enriching" | "embedding" | "indexing" | "completed" | "failed" | "cancel_requested" | "cancelled";
export type KnowledgeJob = {
  id: number;
  index_id: number;
  owner_id: number;
  state: KnowledgeJobState;
  attempts: number;
  lease_expires_at: string | null;
  failure_code: string | null;
  progress: { stage: string; time: string; skipped?: boolean }[];
  reserved_tokens: number;
  processing_reserved_tokens?: number;
  processing_tokens?: number | null;
  processing_call_count?: number;
  processing_estimated_cost?: string | null;
  processing_usage?: { model_id: number; total_tokens: number | null; cost: string | null; currency: string; elapsed_ms: number; failure_code: string | null }[];
  embedding_tokens: number;
  estimated_cost: string | null;
  create_time: string;
  update_time: string;
};
export type KnowledgeCitation = {
  citation: string;
  knowledge_base_id: number;
  index_id: number;
  document_id: number;
  document_version: number;
  chunk_id: number;
  filename: string;
  sha256: string;
  start_offset: number;
  end_offset: number;
  page: number | null;
  heading: string | null;
  content: string;
  source_locator?: {
    kind: "table" | "text" | "pdf" | "html" | "config" | "slide"; sheet?: string; range?: string; header_range?: string; columns?: string[]; hidden_excluded?: boolean; format?: string;
    page?: number; page_kind?: "text" | "scan" | "mixed" | "blank"; extraction_method?: "native" | "ocr"; bbox?: number[];
    pointer?: string; paths?: string[]; line_start?: number | null; line_end?: number | null;
    element?: string; element_id?: string | null; dom_path?: string; line?: number; heading_path?: string[];
    slide?: number; slide_title?: string; shape_id?: string; role?: "text" | "heading" | "table" | "code" | "notes";
    table?: number; row?: number; merged_cells?: boolean; reading_order?: string;
    page_width?: number; page_height?: number; confidence?: number | null; layout?: "text" | "table" | "unverified"; review_required?: boolean;
  } | null;
  knowledge_notes?: string | null;
};
export type KnowledgeSearchDraft = {
  query: string;
  top_k: number;
  max_bytes: number;
  keyword_only: boolean;
  min_similarity: number;
  rerank: "none" | "coverage";
};
export type KnowledgeSearchResult = {
  hits: (KnowledgeCitation & { score: number })[];
  no_answer: boolean;
  byte_count: number;
  untrusted_data: boolean;
};
export type KnowledgeMutation = { revision: number; job_id: number | null };

export const listKnowledgeBases = (page = 1, pageSize = 20): Promise<KnowledgePage> => request(`/knowledge-bases?page=${page}&page_size=${pageSize}`);
export async function listAllKnowledgeBases(): Promise<KnowledgeBase[]> {
  const items: KnowledgeBase[] = [];
  for (let page = 1; ; page++) {
    const result = await listKnowledgeBases(page, 100);
    items.push(...result.items);
    if (items.length >= result.total || result.items.length === 0) return items;
  }
}
export const getKnowledgeBase = (id: number): Promise<KnowledgeBase> => request(`/knowledge-bases/${id}`);
export const createKnowledgeBase = (draft: KnowledgeDraft): Promise<KnowledgeBase> => request("/knowledge-bases", { method: "POST", body: JSON.stringify(draft) });
export const updateKnowledgeBase = (id: number, draft: KnowledgeDraft, revision: number): Promise<{ knowledge_base: KnowledgeBase; job_id: number }> => request(`/knowledge-bases/${id}`, { method: "PUT", body: JSON.stringify({ ...draft, expected_revision: revision }) });
export const deleteKnowledgeBase = (id: number, revision: number): Promise<void> => request(`/knowledge-bases/${id}?expected_revision=${revision}`, { method: "DELETE" });
export const listKnowledgeDocuments = (id: number): Promise<KnowledgeDocument[]> => request(`/knowledge-bases/${id}/documents`);
export function uploadKnowledgeDocument(id: number, file: File, revision: number, documentId?: number, options?: KnowledgeDocumentOptions): Promise<KnowledgeMutation & { document: KnowledgeDocument; deduplicated: boolean }> {
  const body = new FormData();
  body.append("file", file);
  body.append("expected_revision", String(revision));
  if (options) body.append("processing_options", JSON.stringify(options));
  return request(`/knowledge-bases/${id}/documents${documentId ? `/${documentId}` : ""}`, { method: documentId ? "PUT" : "POST", body });
}
export type KnowledgeProcessingMetadata = {
  format?: string; block_count?: number; selection?: string; excluded_regions?: number; table_count?: number;
  slide_count?: number; hidden_slides?: number; notes_blocks?: number; unsupported_visual_objects?: number; warnings?: string[];
  parser?: string; ocr_pages?: number; blank_pages?: number; languages?: string | null;
  pages?: { page: number; kind: "text" | "scan" | "mixed" | "blank"; method: "native" | "ocr" | null; confidence: number | null; blocks: number }[];
};
export type KnowledgeProcessingPreview = {
  index_id: number | null; current_version: number; indexed_version: number | null;
  processing_options: KnowledgeDocumentOptions;
  processing_metadata?: KnowledgeProcessingMetadata; items: KnowledgeCitation[]; has_more: boolean; next_after: number;
};
export const getKnowledgeProcessingPreview = (id: number, documentId: number, after = -1, signal?: AbortSignal): Promise<KnowledgeProcessingPreview> =>
  request(`/knowledge-bases/${id}/documents/${documentId}/processing-preview?after=${after}&limit=10`, { signal, cache: "no-store" });
export const deleteKnowledgeDocument = (id: number, documentId: number, revision: number): Promise<KnowledgeMutation> => request(`/knowledge-bases/${id}/documents/${documentId}?expected_revision=${revision}`, { method: "DELETE" });
export const rebuildKnowledgeIndex = (id: number, revision: number): Promise<KnowledgeMutation> => request(`/knowledge-bases/${id}/rebuild`, { method: "POST", body: JSON.stringify({ expected_revision: revision }) });
export const listKnowledgeJobs = (id: number): Promise<KnowledgeJob[]> => request(`/knowledge-bases/${id}/jobs`);
export const cancelKnowledgeJob = (jobId: number): Promise<void> => request(`/knowledge-bases/jobs/${jobId}/cancel`, { method: "POST" });
export const retryKnowledgeJob = (jobId: number, attempts: number): Promise<void> => request(`/knowledge-bases/jobs/${jobId}/retry?expected_attempts=${attempts}`, { method: "POST" });
export const searchKnowledgeBase = (id: number, draft: KnowledgeSearchDraft): Promise<KnowledgeSearchResult> => request(`/knowledge-bases/${id}/search`, { method: "POST", body: JSON.stringify(draft) });
export const getKnowledgeCitation = (id: number, chunkId: number): Promise<KnowledgeCitation> => request(`/knowledge-bases/${id}/citations/${chunkId}`);
export const listKnowledgeShares = (id: number): Promise<{ user_ids: number[] }> => request(`/knowledge-bases/${id}/shares`);
export const grantKnowledgeShare = (id: number, userId: number): Promise<void> => request(`/knowledge-bases/${id}/shares`, { method: "POST", body: JSON.stringify({ user_id: userId }) });
export const revokeKnowledgeShare = (id: number, userId: number): Promise<void> => request(`/knowledge-bases/${id}/shares/${userId}`, { method: "DELETE" });
export const testEmbeddingModel = (id: number): Promise<{ success: boolean; failure_code: string | null; dimensions: number | null; latency_ms: number }> => request(`/models/${id}/embedding-test`, { method: "POST" });

export type KnowledgeReference = Pick<KnowledgeCitation, "knowledge_base_id" | "index_id" | "chunk_id" | "document_id" | "document_version">;
export type KnowledgeRetrieval = {
  id: number; run_id: string; references: KnowledgeReference[]; hit_count: number; byte_count: number; latency_ms: number;
  embedding_usage: { model_id: number; dimensions: number; tokens: number; cost: string | null }[];
};
export const listRunKnowledgeRetrievals = (runId: string, afterId = 0): Promise<KnowledgeRetrieval[]> => request(`/runs/${encodeURIComponent(runId)}/knowledge-retrievals?after_id=${afterId}`);
