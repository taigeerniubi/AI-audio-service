// 展示资料处理阶段、已用时间和明确的入库或失败反馈。
import { useEffect, useState } from "react";
import { AlertCircle, Check, CheckCircle2, LoaderCircle } from "lucide-react";
import type { DocumentProcessing } from "../../types/api";
import { isProcessing } from "./useDocumentUpload";

const phases = ["上传文件", "解析内容", "建立索引", "入库完成"];
const steps = {
  uploading: 0,
  validating: 0,
  parsing: 1,
  embedding: 2,
  saving: 3,
  completed: 4,
  failed: -1,
};
const titles = {
  uploading: "正在上传文件",
  validating: "正在检查文件",
  parsing: "正在解析内容",
  embedding: "正在建立检索索引",
  saving: "正在保存资料",
  completed: "已入库，可以检索",
  failed: "资料处理未完成",
};

/** 展示实际步骤与耗时，不把解析或云端索引伪装成可计算的百分比。 */
export function UploadStatus({
  state,
  productName,
  retry,
  onSearch,
}: {
  state: DocumentProcessing;
  productName: string;
  retry?: () => void;
  onSearch: () => void;
}) {
  const [now, setNow] = useState(Date.now());
  const running = isProcessing(state);
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running, state.id]);
  const seconds = Math.max(
    0,
    Math.floor(
      (state.finished_at ? state.finished_at * 1000 : now) / 1000 -
        state.started_at,
    ),
  );
  const failed = state.stage === "failed";
  const complete = state.stage === "completed";
  const active = steps[state.stage];
  return (
    <section
      className={`upload-status ${complete ? "complete" : failed ? "failed" : "processing"}`}
      aria-label="资料处理状态"
    >
      <div className="upload-status-heading">
        <div role={failed ? "alert" : "status"}>
          {complete ? (
            <CheckCircle2 size={20} />
          ) : failed ? (
            <AlertCircle size={20} />
          ) : (
            <LoaderCircle className="spin" size={20} />
          )}
          <strong>{titles[state.stage]}</strong>
        </div>
        <span>
          {running ? "已用 " : "用时 "}
          {seconds} 秒
        </span>
      </div>
      <p className="upload-filename">{state.name}</p>
      <p className="upload-destination">所属产品：{productName}</p>
      {!failed && (
        <ol className="upload-steps" aria-label="处理步骤">
          {phases.map((phase, index) => (
            <li
              key={phase}
              className={
                index < active ? "done" : index === active ? "current" : ""
              }
              aria-current={index === active ? "step" : undefined}
            >
              <span>{index < active ? <Check size={12} /> : index + 1}</span>
              {phase}
            </li>
          ))}
        </ol>
      )}
      {state.stage === "embedding" && (
        <p>
          已解析{state.chunks ? ` ${state.chunks} 个片段` : "内容"}
          ，正在生成检索索引，请稍候。
        </p>
      )}
      {state.stage === "parsing" && (
        <p>正在提取文字与表格，文件越复杂，处理时间越长。</p>
      )}
      {complete && (
        <div className="upload-result">
          <p>
            {state.document?.chunks ?? 0} 个片段 ·{" "}
            {state.document?.characters.toLocaleString() ?? 0} 字符
            <br />
            现在可以试跑检索；新对话会使用这份资料。
          </p>
          <button className="secondary" onClick={onSearch}>
            试跑检索
          </button>
        </div>
      )}
      {failed && (
        <div className="upload-result">
          <p>{state.error || "请检查文件后重试。"}</p>
          {retry && (
            <button className="secondary" onClick={retry}>
              重试
            </button>
          )}
        </div>
      )}
    </section>
  );
}
