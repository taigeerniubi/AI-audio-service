// 将产品选择、语音操作和实时转录作为主要任务，按需展开运行观测。
import { useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowRight,
  AudioLines,
  BookOpen,
  ChevronDown,
  FileText,
  Headphones,
  Mic,
  MicOff,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Square,
} from "lucide-react";
import type { Product, Health } from "../../types/api";
import type { VoiceSession } from "./useVoiceSession";
import { MetricCard } from "../../components/MetricCard";

const labels: Record<string, string> = {
  idle: "准备开始",
  connecting: "正在连接",
  listening: "正在聆听",
  thinking: "正在组织回答",
  speaking: "顾问正在回答",
  ended: "对话已结束",
};

/** 展示当前对话任务，低频技术信息在用户展开后呈现。 */
export function StudioPage({
  voice,
  products,
  selected,
  setSelected,
  health,
  product,
  onConfigure,
  onReport,
}: {
  voice: VoiceSession;
  products: Product[];
  selected: string;
  setSelected: (id: string) => void;
  health?: Health;
  product?: Product;
  onConfigure: () => void;
  onReport: () => void;
}) {
  const {
    state,
    live,
    messages,
    traces,
    metrics,
    sources,
    muted,
    volume,
    elapsed,
    start,
    stop,
    interrupt,
    toggleMute,
    latestLatency,
    sessionProduct,
    selectedVoice,
    setSelectedVoice,
  } = voice;
  const [technical, setTechnical] = useState(false);
  const transcriptScroll = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const container = transcriptScroll.current;
    if (container) container.scrollTop = container.scrollHeight;
  }, [messages]);
  const statusText =
    state === "idle"
      ? "选择产品后，开始与 AI 顾问交流。"
      : state === "ended"
        ? "转录已保留，可以查看本次会话报告。"
        : state === "connecting"
          ? "正在建立语音连接，请稍候。"
          : muted
            ? "麦克风已静音，点击下方按钮恢复。"
            : "直接说话即可，也可以随时插话。";
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>产品语音对话</h1>
          <p>选择一个产品，与了解它的 AI 顾问自然交流。</p>
        </div>
        <button className="secondary" onClick={onConfigure}>
          <SlidersHorizontal size={16} />
          配置产品
        </button>
      </div>
      <div className="session-setup card">
        <label>
          <span className="field-label">对话产品</span>
          <select
            aria-label="选择产品"
            value={selected}
            disabled={live}
            onChange={(event) => setSelected(event.target.value)}
          >
            {!products.length && <option value="">请先创建产品</option>}
            {products.map((item) => (
              <option value={item.id} key={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="field-label">顾问音色</span>
          <select
            aria-label="选择音色"
            value={selectedVoice}
            disabled={live}
            onChange={(event) => setSelectedVoice(event.target.value)}
          >
            {(
              health?.voices || [{ id: selectedVoice, name: "龙安欢 · 女声" }]
            ).map((option) => (
              <option key={option.id} value={option.id}>
                {option.name}
              </option>
            ))}
          </select>
        </label>
        <div className="setup-state" role="status">
          <span
            className={`dot ${live || health?.configured ? "green" : ""}`}
          />
          <div>
            <strong>
              {state === "connecting"
                ? "正在连接语音服务"
                : live
                  ? "对话进行中"
                  : health?.configured
                    ? "服务已配置"
                    : health
                      ? "服务未就绪"
                      : "正在读取配置"}
            </strong>
            <small>
              {live
                ? "产品与音色在本次对话中固定"
                : health?.configured
                  ? "开始对话时验证语音连接"
                  : "请配置语音服务凭证"}
            </small>
          </div>
        </div>
      </div>
      {health && !health.configured && (
        <div className="setup-note">
          <Settings2 size={17} />
          <span>
            开始前，在项目 <code>.env</code> 中配置百炼 API Key，然后重启后端。
          </span>
        </div>
      )}
      {!products.length && health && (
        <div className="setup-note">
          <BookOpen size={17} />
          <span>还没有产品。先点击“配置产品”创建产品，再开始对话。</span>
        </div>
      )}
      <div className="studio-grid">
        <section
          className={`voice-panel card ${live ? "is-live" : ""}`}
          aria-label="语音连接"
        >
          <div className="panel-top">
            <h2 className="section-title">
              <Mic size={17} />
              语音连接
            </h2>
            <span className="live-badge">
              <span
                className={`dot ${live && state !== "connecting" ? "green" : ""}`}
              />
              {live ? (state === "connecting" ? "连接中" : "已连接") : "未连接"}
            </span>
          </div>
          <div className="voice-stage">
            <div className="voice-visual" aria-hidden="true">
              <div className="voice-disc">
                <AudioLines size={48} strokeWidth={1.5} />
              </div>
              <div className="waveform">
                {[8, 14, 22, 12, 32, 44, 26, 52, 34, 18, 38, 24, 14, 20, 8].map(
                  (height, index) => (
                    <i
                      key={index}
                      style={{
                        height: `${live && !muted ? Math.min(58, 8 + height * Math.min(1, volume * 8)) : height * 0.44}px`,
                      }}
                    />
                  ),
                )}
              </div>
            </div>
            <div className="voice-status" role="status">
              <h2>{labels[state] || state}</h2>
              <p>{statusText}</p>
            </div>
            <div className="voice-controls">
              {!live ? (
                <button
                  className="primary start-button"
                  onClick={() => void start()}
                  disabled={!health?.configured || !selected}
                >
                  <Mic size={18} />
                  开始对话
                  <ArrowRight size={17} />
                </button>
              ) : (
                <>
                  <button
                    className={`round-control ${muted ? "muted" : ""}`}
                    title={muted ? "开启麦克风" : "静音麦克风"}
                    aria-label={muted ? "开启麦克风" : "静音麦克风"}
                    onClick={toggleMute}
                  >
                    {muted ? <MicOff size={19} /> : <Mic size={19} />}
                  </button>
                  <button className="secondary" onClick={interrupt}>
                    停止回答
                  </button>
                  <button className="end-button" onClick={stop}>
                    <Square size={13} />
                    结束
                  </button>
                </>
              )}
            </div>
            {state === "ended" && (
              <button className="text-button report-link" onClick={onReport}>
                <FileText size={15} />
                查看会话报告
                <ArrowRight size={14} />
              </button>
            )}
          </div>
          <div className="voice-footer">
            <Headphones size={15} />
            <span>佩戴耳机，对话更清晰</span>
            <time className="timer" aria-label="对话时长">
              {Math.floor(elapsed / 60)
                .toString()
                .padStart(2, "0")}
              :{(elapsed % 60).toString().padStart(2, "0")}
            </time>
          </div>
        </section>
        <section className="transcript-panel card" aria-label="实时转录">
          <div className="panel-top">
            <h2 className="section-title">
              <AudioLines size={18} />
              实时转录
            </h2>
            <span className="small-badge">{messages.length} 条消息</span>
          </div>
          <div
            ref={transcriptScroll}
            className="transcript-scroll"
            role="log"
            aria-label="对话记录"
            aria-live="polite"
            aria-relevant="additions text"
          >
            {messages.length === 0 ? (
              <div className="empty-transcript">
                <div className="empty-icon">
                  <AudioLines size={28} strokeWidth={1.5} />
                </div>
                <h3>等待第一句对话</h3>
                <p>
                  点击“开始对话”，顾问会先向你问好。
                  <br />
                  你们的交流会实时显示在这里。
                </p>
                <div className="prompt-chips">
                  <span className="prompt-label">你可以这样问</span>
                  <span>介绍一下这个产品的主要功能</span>
                  <span>它适合什么样的团队？</span>
                </div>
              </div>
            ) : (
              messages.map((message) => (
                <div key={message.id} className={`message ${message.role}`}>
                  <div className="message-meta">
                    <span className="message-avatar">
                      {message.role === "user" ? "你" : "AI"}
                    </span>
                    <strong>
                      {message.role === "user" ? "你" : "产品顾问"}
                    </strong>
                    <small>
                      {message.interrupted
                        ? "已打断 · 后续内容已丢弃"
                        : message.final
                          ? message.role === "assistant"
                            ? "播放完成"
                            : "转录完成"
                          : message.role === "assistant"
                            ? "随播放显示"
                            : "转录中"}
                    </small>
                  </div>
                  <p>{message.text || "…"}</p>
                </div>
              ))
            )}
          </div>
          <div className="transcript-footer">
            <ShieldCheck size={14} />
            仅保存转录与运行指标，默认不保存录音
          </div>
        </section>
      </div>
      <section className="technical card">
        <button
          className="technical-toggle"
          aria-expanded={technical}
          aria-controls="runtime-details"
          onClick={() => setTechnical(!technical)}
        >
          <span>
            <Activity size={17} />
            <strong>运行观测</strong>
            <span className="subtle">响应指标、事件与知识来源</span>
          </span>
          <span className="toggle-action">
            {technical ? "收起" : "展开"}
            <ChevronDown size={16} className={technical ? "rotate" : ""} />
          </span>
        </button>
        {technical && (
          <div className="technical-body" id="runtime-details">
            <div className="model-strip">
              <div>
                <strong>千问实时语音</strong>
                <code>{health?.model || "qwen-audio-3.1-realtime-plus"}</code>
              </div>
              <span className="subtle">
                {state === "connecting"
                  ? "正在验证连接"
                  : live
                    ? "语音服务已连接"
                    : health?.configured
                      ? "凭证已配置 · 待连接验证"
                      : "等待配置凭证"}
              </span>
            </div>
            <div className="metrics-row">
              <MetricCard
                icon={<Activity size={16} />}
                title="首音频响应"
                value={
                  typeof latestLatency === "number"
                    ? `${(latestLatency / 1000).toFixed(2)} s`
                    : "—"
                }
                caption="检测结束事件 → 播放确认"
              />
              <MetricCard
                icon={<AudioLines size={16} />}
                title="有效对话轮次"
                value={String(
                  metrics.filter(
                    (item) => item.speech_stop_event_to_play_ack_ms != null,
                  ).length,
                )}
                caption="已收到首音频播放确认"
              />
              <MetricCard
                icon={<Mic size={16} />}
                title="打断记录"
                value={String(
                  metrics.filter((item) => item.kind === "interruption").length,
                )}
                caption="含语音插话与手动停止"
              />
              <MetricCard
                icon={<BookOpen size={16} />}
                title="当前产品版本"
                value={`v${(live ? sessionProduct : product)?.version || 1}`}
                caption="会话使用独立的资料快照"
              />
            </div>
            <div className="signal-line">
              麦克风输入
              <span className="meter">
                <i style={{ width: `${Math.min(100, volume * 500)}%` }} />
              </span>
              <code>{muted ? "MUTED" : "16 kHz PCM"}</code>
            </div>
            <div className="trace-grid">
              <div>
                <h3>事件时间线</h3>
                {traces.length === 0 ? (
                  <p className="subtle">开始对话后显示真实运行事件。</p>
                ) : (
                  traces.slice(-20).map((trace, index) => (
                    <div className="trace-line" key={index}>
                      <code>{(trace.elapsed_ms / 1000).toFixed(2)}s</code>
                      <span>{trace.type}</span>
                    </div>
                  ))
                )}
              </div>
              <div>
                <h3>知识来源</h3>
                {sources.length === 0 ? (
                  <p className="subtle">
                    顾问检索产品知识后，引用资料显示在这里。
                  </p>
                ) : (
                  sources.map((source, index) => (
                    <div className="source" key={index}>
                      <strong>{source.source}</strong>
                      <small>
                        {source.pages?.length
                          ? ` · 第 ${source.pages.join("、")} 页`
                          : ""}
                        {source.retrieval_mode === "bm25_legacy"
                          ? " · 旧版关键词索引"
                          : source.retrieval_mode
                            ? " · 混合检索"
                            : ""}
                      </small>
                      <p>{source.text}</p>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        )}
      </section>
    </>
  );
}
