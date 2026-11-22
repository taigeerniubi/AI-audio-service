// 管理当前产品资料、展示真实入库进度并提供有来源的检索试跑。
import { useEffect, useRef, useState } from "react";
import {
  BookOpen,
  CheckCircle2,
  FileText,
  LoaderCircle,
  Trash2,
  Upload,
} from "lucide-react";
import { api } from "../../api/client";
import type { Source } from "../../types/api";
import { UploadStatus } from "./UploadStatus";
import { useDocumentUpload } from "./useDocumentUpload";

/** 绑定一个编辑产品；父组件通过 key 在切换时隔离异步结果和本地状态。 */
export function KnowledgePanel({
  editingId,
  productName,
  onError,
}: {
  editingId: string | null;
  productName: string;
  onError: (message: string) => void;
}) {
  const {
    docs,
    processing,
    busy,
    loaded,
    deleting,
    notice,
    attempt,
    submit,
    remove,
  } = useDocumentUpload(editingId, onError);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [matches, setMatches] = useState<Source[] | null>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    setMatches(null);
  }, [docs]);

  /** 检索当前产品资料，展示原文与来源；页面离开后忽略迟到结果。 */
  async function searchKnowledge(e: React.FormEvent) {
    e.preventDefault();
    if (!editingId || !query.trim()) return;
    setSearching(true);
    try {
      const result = await api<{ matches: Source[] }>(
        `/api/products/${editingId}/search`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query }),
        },
      );
      if (mounted.current) setMatches(result.matches);
    } catch (error) {
      if (mounted.current) onError((error as Error).message);
    } finally {
      if (mounted.current) setSearching(false);
    }
  }

  /** 将用户带到检索输入框，让入库后的下一步明确可见。 */
  function focusSearch() {
    searchInput.current?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
    searchInput.current?.focus({ preventScroll: true });
  }

  const disabled = !editingId || !loaded || busy || deleting;
  return (
    <section className="card knowledge-card">
      <div className="panel-top">
        <span>产品资料</span>
        <span className="small-badge">已入库 {docs.length} / 20</span>
      </div>
      <div className="knowledge-body">
        <label className={`upload-zone ${disabled ? "disabled" : ""}`}>
          {busy ? (
            <LoaderCircle className="spin" size={26} />
          ) : (
            <Upload size={26} />
          )}
          <strong>
            {busy
              ? "文件处理中，请稍候"
              : editingId
                ? "选择文件，加入知识库"
                : "先保存产品，再添加资料"}
          </strong>
          <span>PDF / DOCX / XLSX / TXT / MD · 最多 10 MB</span>
          <small>文字版 PDF 最多 50 页 · Excel 请粘贴为值</small>
          <input
            type="file"
            accept=".txt,.md,.pdf,.docx,.xlsx"
            disabled={disabled}
            aria-label="上传产品资料"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void submit({ file });
              e.target.value = "";
            }}
          />
        </label>
        {processing && (
          <UploadStatus
            state={processing}
            productName={productName}
            retry={
              attempt && !busy && !deleting
                ? () => {
                    void submit(attempt);
                  }
                : undefined
            }
            onSearch={focusSearch}
          />
        )}
        {notice && (
          <p className="upload-notice" role="status">
            {notice}
          </p>
        )}
        {docs.map((doc) => (
          <div className="document" key={doc.id}>
            <FileText size={20} />
            <div className="document-info">
              <strong>{doc.name}</strong>
              <small>
                {doc.characters.toLocaleString()} 字符 ·{" "}
                {doc.index_status === "ready"
                  ? `${doc.chunks} 个片段`
                  : "旧版关键词索引"}
              </small>
              {doc.warnings?.length ? (
                <details className="document-warnings">
                  <summary>{doc.warnings.length} 条解析提示</summary>
                  {doc.warnings.map((warning, index) => (
                    <p key={index}>{warning}</p>
                  ))}
                </details>
              ) : null}
              {doc.index_status !== "ready" && (
                <button
                  className="secondary"
                  disabled={disabled}
                  onClick={() => {
                    void submit({ document: doc });
                  }}
                >
                  建立向量索引
                </button>
              )}
            </div>
            {doc.index_status === "ready" && (
              <span className="document-ready">
                <CheckCircle2 size={13} />
                可检索
              </span>
            )}
            <button
              aria-label={`删除 ${doc.name}`}
              disabled={disabled}
              onClick={() => {
                void remove(doc);
              }}
            >
              <Trash2 size={16} />
            </button>
          </div>
        ))}
        {loaded && !docs.length && !busy && !processing && (
          <p className="knowledge-empty">
            还没有资料。入库完成后，文件会显示在这里。
          </p>
        )}
        <div className="knowledge-tip">
          <BookOpen size={18} />
          <p>
            资料供顾问检索问答，不会自动填写左侧产品配置。上传成功后，开始新对话即可使用；进行中的对话保留原资料。
          </p>
        </div>
        <div className="retrieval-test">
          <h3>检索试跑</h3>
          <p className="subtle">
            输入具体问题，确认能找到资料后再开始语音对话。
          </p>
          <form onSubmit={searchKnowledge}>
            <input
              ref={searchInput}
              aria-label="检索问题"
              value={query}
              maxLength={500}
              placeholder="例如：9700X 是几核几线程，功耗多少？"
              onChange={(e) => setQuery(e.target.value)}
            />
            <button
              className="primary"
              disabled={disabled || searching || !query.trim()}
            >
              {searching ? "检索中…" : "检索资料"}
            </button>
          </form>
          {matches?.length === 0 && (
            <p>没有找到相关资料，请补充文件或调整问题。</p>
          )}
          {matches?.map((match, index) => (
            <article className="source" key={index}>
              <strong>
                {index + 1}. {match.source}
                {match.pages?.length
                  ? ` · 第 ${match.pages.join("、")} 页`
                  : ""}
              </strong>
              <small>
                关键词第 {match.bm25_rank ?? "—"} 名 · 向量第{" "}
                {match.vector_rank ?? "—"} 名 · 融合分 {match.score?.toFixed(5)}
              </small>
              <p>{match.text}</p>
              {match.source_urls
                ?.filter((url) => /^https?:\/\//.test(url))
                .map((url) => (
                  <a href={url} key={url} target="_blank" rel="noreferrer">
                    查看官网来源 ↗
                  </a>
                ))}
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
