import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import {
  corpusAssetUrl,
  fetchCorpusMetadata,
  fetchCorpusPreview,
  formatCorpusSize,
  type CorpusMetadata,
  type CorpusPreview,
} from "@/shared/api/corpus";
import type { SourceDoc } from "@/shared/api/types";

type SourceDetailPanelProps = {
  open: boolean;
  source: SourceDoc | null;
  displayId: number | null;
  title: string;
  evidenceHtml: string;
  panelRef: RefObject<HTMLElement | null>;
  onClose: () => void;
};

const PAGE_SIZE = 80;
const DEFAULT_PANEL_WIDTH = 760;
const MIN_PANEL_WIDTH = 380;
const PANEL_WIDTH_STORAGE_KEY = "trustedrag-source-panel-width";

function maximumPanelWidth(): number {
  return Math.min(Math.max(MIN_PANEL_WIDTH, window.innerWidth * 0.82), 1200);
}

function clampPanelWidth(width: number): number {
  return Math.min(Math.max(width, MIN_PANEL_WIDTH), maximumPanelWidth());
}

function initialPanelWidth(): number {
  if (typeof window === "undefined") return DEFAULT_PANEL_WIDTH;
  const saved = Number(window.localStorage.getItem(PANEL_WIDTH_STORAGE_KEY));
  return clampPanelWidth(Number.isFinite(saved) && saved > 0 ? saved : DEFAULT_PANEL_WIDTH);
}

function previewErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return "原始文件加载失败。";
}

export function SourceDetailPanel({
  open,
  source,
  displayId,
  title,
  evidenceHtml,
  panelRef,
  onClose,
}: SourceDetailPanelProps) {
  const hasOriginal = Boolean(source?.collection_id && source?.original_metadata_url && source?.original_content_url);
  const [activeTab, setActiveTab] = useState<"original" | "evidence">(hasOriginal ? "original" : "evidence");
  const [metadata, setMetadata] = useState<CorpusMetadata | null>(null);
  const [preview, setPreview] = useState<CorpusPreview | null>(null);
  const [variant, setVariant] = useState("");
  const [sheet, setSheet] = useState("");
  const [offset, setOffset] = useState(0);
  const [metadataLoading, setMetadataLoading] = useState(open && hasOriginal);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [error, setError] = useState("");
  const [retryToken, setRetryToken] = useState(0);
  const [panelWidth, setPanelWidth] = useState(initialPanelWidth);
  const resizeStart = useRef({ x: 0, width: DEFAULT_PANEL_WIDTH });

  useEffect(() => {
    const clampToViewport = () => setPanelWidth((current) => clampPanelWidth(current));
    window.addEventListener("resize", clampToViewport);
    return () => window.removeEventListener("resize", clampToViewport);
  }, []);

  const updatePanelWidth = (width: number) => {
    const nextWidth = clampPanelWidth(width);
    setPanelWidth(nextWidth);
    window.localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, String(Math.round(nextWidth)));
  };
  useEffect(() => {
    if (!open || !source?.original_metadata_url || !hasOriginal) return;

    const controller = new AbortController();
    void fetchCorpusMetadata(source.original_metadata_url, controller.signal)
      .then((result) => {
        setMetadata(result);
        setVariant(result.preferred_file_name);
        if (result.preview_kind === "xlsx") setPreviewLoading(true);
      })
      .catch((loadError: unknown) => {
        if (loadError instanceof DOMException && loadError.name === "AbortError") return;
        setError(previewErrorMessage(loadError));
      })
      .finally(() => {
        if (!controller.signal.aborted) setMetadataLoading(false);
      });
    return () => controller.abort();
  }, [hasOriginal, open, retryToken, source?.original_metadata_url]);

  useEffect(() => {
    if (!open || activeTab !== "original" || !source?.preview_content_url || !metadata || !variant) return;
    if (metadata.preview_kind === "pdf") return;
    if (metadata.preview_kind !== "xlsx") return;
    const controller = new AbortController();
    void fetchCorpusPreview(source.preview_content_url, {
      sheet,
      offset,
      limit: PAGE_SIZE,
      signal: controller.signal,
    })
      .then((result) => {
        setPreview(result);
        if (result.sheet && result.sheet !== sheet) setSheet(result.sheet);
        if (typeof result.offset === "number" && result.offset !== offset) setOffset(result.offset);
      })
      .catch((loadError: unknown) => {
        if (loadError instanceof DOMException && loadError.name === "AbortError") return;
        setError(previewErrorMessage(loadError));
      })
      .finally(() => {
        if (!controller.signal.aborted) setPreviewLoading(false);
      });
    return () => controller.abort();
  }, [activeTab, metadata, offset, open, retryToken, sheet, source?.preview_content_url, variant]);

  const downloadUrl = source?.original_download_url
    ? corpusAssetUrl(source.original_download_url, variant)
    : preview?.download_url
      ? corpusAssetUrl(preview.download_url)
      : "";
  const inlineUrl = source?.preview_file_url
    ? corpusAssetUrl(source.preview_file_url)
    : metadata?.preview_url
      ? corpusAssetUrl(metadata.preview_url)
      : "";
  const pageOffset = preview?.offset ?? offset;
  const pageLimit = preview?.limit ?? PAGE_SIZE;
  const totalItems = preview?.kind === "xlsx" ? preview.total_rows ?? 0 : 0;
  const canPrevious = pageOffset > 0;
  const canNext = pageOffset + pageLimit < totalItems;

  if (!open) return null;

  return (
    <div
      className="source-detail-overlay"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <aside
        className="chat-detail-sidebar show"
        id="source-detail-panel"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
        style={{ width: `${panelWidth}px` }}
      >
      <div
        className="source-detail-resize-handle"
        role="separator"
        aria-label="拖动调整证据面板宽度"
        aria-orientation="vertical"
        aria-valuemin={MIN_PANEL_WIDTH}
        aria-valuemax={Math.round(maximumPanelWidth())}
        aria-valuenow={Math.round(panelWidth)}
        tabIndex={0}
        onPointerDown={(event) => {
          event.preventDefault();
          event.stopPropagation();
          resizeStart.current = { x: event.clientX, width: panelWidth };
          event.currentTarget.setPointerCapture(event.pointerId);
          document.body.classList.add("source-panel-resizing");
        }}
        onPointerMove={(event) => {
          if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
          updatePanelWidth(resizeStart.current.width + resizeStart.current.x - event.clientX);
        }}
        onPointerUp={(event) => {
          if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            event.currentTarget.releasePointerCapture(event.pointerId);
          }
          document.body.classList.remove("source-panel-resizing");
        }}
        onPointerCancel={() => document.body.classList.remove("source-panel-resizing")}
        onKeyDown={(event) => {
          if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
          event.preventDefault();
          updatePanelWidth(panelWidth + (event.key === "ArrowLeft" ? 24 : -24));
        }}
      />
      <div className="detail-header">
        <div>
          <h3 className="detail-title">{title}</h3>
          {source?.collection_id ? (
            <div className="corpus-reference-id">知识库 {source.collection_id} · 来源 [{displayId ?? source.id}]</div>
          ) : null}
        </div>
        <button type="button" className="btn btn-icon btn-sm" onClick={onClose} aria-label="关闭">
          ✕
        </button>
      </div>

      <div className="corpus-detail-tabs" role="tablist" aria-label="引用详情视图">
        <button
          type="button"
          className={activeTab === "original" ? "active" : ""}
          disabled={!hasOriginal}
          onClick={() => setActiveTab("original")}
        >
          原始版式
        </button>
        <button
          type="button"
          className={activeTab === "evidence" ? "active" : ""}
          onClick={() => setActiveTab("evidence")}
        >
          检索证据
        </button>
      </div>

      {activeTab === "evidence" ? (
        <div className="detail-content corpus-evidence-content" dangerouslySetInnerHTML={{ __html: evidenceHtml }} />
      ) : (
        <div className="detail-content corpus-original-content">
          {metadataLoading ? <div className="corpus-preview-state">正在读取文件信息…</div> : null}
          {metadata ? (
            <div className="corpus-file-card">
              <strong>{metadata.display_name}</strong>
              <span>预览：{metadata.preview_file_name}</span>
              <span>
                {metadata.preview_kind === "pdf" ? "生成的 PDF 版式预览" : "XLSX 表格预览"} · {formatCorpusSize(metadata.preview_size)}
              </span>
              <span>原始文件：{variant || metadata.preferred_file_name}</span>
              <div className="corpus-file-actions">
                {metadata.variants.length > 1 ? (
                  <select
                    aria-label="原始文件版本"
                    value={variant}
                    onChange={(event) => {
                      setVariant(event.target.value);
                      setSheet("");
                      setOffset(0);
                      setPreview(null);
                      setPreviewLoading(true);
                      setError("");
                    }}
                  >
                    {metadata.variants.map((item) => (
                      <option key={item.name} value={item.name}>
                        {item.name}{item.preferred ? "（推荐）" : ""}
                      </option>
                    ))}
                  </select>
                ) : null}
                {downloadUrl ? (
                  <a className="corpus-download-link" href={downloadUrl} download>
                    下载原始文件
                  </a>
                ) : null}
              </div>
            </div>
          ) : null}

          {error ? (
            <div className="corpus-preview-state error">
              <span>{error}</span>
              <button type="button" onClick={() => {
                setError("");
                if (metadata) setPreviewLoading(true);
                else setMetadataLoading(true);
                setRetryToken((current) => current + 1);
              }}>
                重试
              </button>
            </div>
          ) : null}
          {previewLoading ? <div className="corpus-preview-state">正在加载原始文件内容…</div> : null}
          {!previewLoading && preview?.status === "preview_unavailable" ? (
            <div className="corpus-preview-state warning">
              {preview.message || "该文件暂时无法生成在线预览，请下载原始文件查看。"}
            </div>
          ) : null}

          {!previewLoading && preview?.status === "ok" && preview.kind === "xlsx" ? (
            <div className="corpus-excel-preview">
              <div className="corpus-preview-toolbar">
                <label>
                  工作表
                  <select
                    value={preview.sheet ?? sheet}
                    onChange={(event) => {
                      setSheet(event.target.value);
                      setOffset(0);
                      setPreviewLoading(true);
                      setError("");
                    }}
                  >
                    {(preview.sheets ?? []).map((item) => (
                      <option key={item} value={item}>{item}</option>
                    ))}
                  </select>
                </label>
                <span>
                  第 {pageOffset + 1}-{Math.min(pageOffset + pageLimit, preview.total_rows ?? 0)} 行 / 共 {preview.total_rows ?? 0} 行
                </span>
              </div>
              <div className="corpus-location-note">该编号没有生成 PDF，当前显示原始 XLSX 的分页表格。</div>
              <div className="corpus-table-scroll">
                <table>
                  <tbody>
                    {(preview.rows ?? []).map((row, rowIndex) => (
                      <tr key={`${pageOffset}-${rowIndex}`}>
                        <th>{pageOffset + rowIndex + 1}</th>
                        {row.map((cell, cellIndex) => (
                          <td key={cellIndex}>{cell}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}

          {!previewLoading && metadata?.preview_kind === "pdf" && inlineUrl ? (
            <div className="corpus-pdf-preview">
              <iframe title={`PDF 版式预览 ${metadata.preview_file_name}`} src={inlineUrl} />
            </div>
          ) : null}

          {preview?.status === "ok" && preview.kind === "xlsx" && totalItems > 0 ? (
            <div className="corpus-pagination">
              <button type="button" disabled={!canPrevious} onClick={() => {
                setOffset(Math.max(0, pageOffset - pageLimit));
                setPreviewLoading(true);
                setError("");
              }}>
                上一页
              </button>
              <button type="button" disabled={!canNext} onClick={() => {
                setOffset(pageOffset + pageLimit);
                setPreviewLoading(true);
                setError("");
              }}>
                下一页
              </button>
            </div>
          ) : null}
        </div>
      )}
      </aside>
    </div>
  );
}
