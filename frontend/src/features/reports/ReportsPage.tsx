// 展示历史会话、业务结果、转录和可观测性能指标。
import { ArrowDownToLine, FileText } from "lucide-react";
import type { Product, Session, Report } from "../../types/api";
import { download } from "../../utils/download";

/** 根据选中的会话报告渲染记录，并支持本地导出。 */
export function ReportsPage({
  products,
  sessions,
  report,
  loadReport,
}: {
  products: Product[];
  sessions: Session[];
  report?: Report;
  loadReport: (id: string) => Promise<void>;
}) {
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>会话报告</h1>
          <p>选择一场对话，回顾交流内容与业务结果。</p>
        </div>
        {report && (
          <button
            className="secondary"
            onClick={() =>
              download(`session-${report.session_id}.json`, report)
            }
          >
            <ArrowDownToLine size={16} />
            导出 JSON
          </button>
        )}
      </div>
      <div className="reports-layout">
        <section className="card session-list">
          <div className="panel-top">最近会话</div>
          {sessions.length === 0 && (
            <p className="subtle padded">还没有会话，先开始一次语音对话。</p>
          )}
          {sessions.map((s) => (
            <button
              className={`session-item ${report?.session_id === s.id ? "selected" : ""}`}
              aria-pressed={report?.session_id === s.id}
              key={s.id}
              onClick={() => void loadReport(s.id)}
            >
              <strong>
                {products.find((p) => p.id === s.product_id)?.name ||
                  "产品会话"}
              </strong>
              <span>
                {new Date(s.created_at).toLocaleString("zh-CN", {
                  timeZone: "Asia/Shanghai",
                })}
              </span>
              <small>
                {s.status === "ended"
                  ? "已结束"
                  : s.status === "active"
                    ? "进行中"
                    : "待连接"}
              </small>
            </button>
          ))}
        </section>
        <section className="card report-detail">
          {!report ? (
            <div className="empty-transcript">
              <FileText size={35} />
              <h3>选择一场对话</h3>
              <p>真实指标会在这里呈现。</p>
            </div>
          ) : (
            <>
              <div className="panel-top">
                会话详情
                <span className="small-badge">
                  {report.duration_seconds ?? 0}s
                </span>
              </div>
              <div className="report-body">
                <details className="report-measurements">
                  <summary>响应指标与用量</summary>
                  <div className="report-stats">
                    <div>
                      <small>首音频 P50</small>
                      <strong>
                        {report.latency?.p50 != null
                          ? `${report.latency.p50} ms`
                          : "—"}
                      </strong>
                    </div>
                    <div>
                      <small>首音频 P95</small>
                      <strong>
                        {report.latency?.p95 != null
                          ? `${report.latency.p95} ms`
                          : "—"}
                      </strong>
                    </div>
                    <div>
                      <small>已报告用量估算</small>
                      <strong>
                        {report.estimated_cost_cny != null
                          ? `¥ ${report.estimated_cost_cny.toFixed(4)}`
                          : "—"}
                      </strong>
                    </div>
                  </div>
                  <p className="report-note">{report.measurement_note}</p>
                  <p className="report-note">
                    {report.cost_note}{" "}
                    {report.missing_usage_responses
                      ? `有 ${report.missing_usage_responses} 个响应未返回用量。`
                      : ""}
                  </p>
                </details>
                <h3>业务结果</h3>
                {report.actions.length === 0 ? (
                  <p className="subtle">本次会话没有已记录的工具结果。</p>
                ) : (
                  report.actions.map((a, i) => (
                    <div className="action-result" key={i}>
                      <strong>{a.name}</strong>
                      <pre>{JSON.stringify(a.result, null, 2)}</pre>
                    </div>
                  ))
                )}
                <h3>对话记录</h3>
                {report.transcripts.length === 0 && (
                  <p className="subtle">本次会话没有已保存的转录。</p>
                )}
                {report.transcripts.map((m) => (
                  <div className="report-message" key={m.id}>
                    <strong>
                      {m.role === "user" ? "你" : "AI"}{" "}
                      {m.interrupted && "· 已打断"}
                    </strong>
                    <p>{m.text}</p>
                  </div>
                ))}
              </div>
            </>
          )}
        </section>
      </div>
    </>
  );
}
