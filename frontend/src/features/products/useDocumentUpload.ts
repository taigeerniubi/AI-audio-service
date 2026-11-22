// 协调资料请求与真实阶段轮询，隔离产品切换、重试和迟到响应。
import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { Doc, DocumentProcessing } from "../../types/api";

type Attempt = { file: File } | { document: Doc };

/** 判断后台是否仍在处理，完成和失败均停止阶段轮询。 */
export function isProcessing(state: DocumentProcessing | null) {
  return !!state && state.stage !== "completed" && state.stage !== "failed";
}

/** 在当前产品面板生命周期内维护资料和上传状态；切换产品时由调用方重新挂载。 */
export function useDocumentUpload(
  productId: string | null,
  onError: (message: string) => void,
) {
  const [docs, setDocs] = useState<Doc[]>([]);
  const [processing, setProcessing] = useState<DocumentProcessing | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [pendingPost, setPendingPost] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [notice, setNotice] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const mounted = useRef(false);
  const operation = useRef<{ id: string; settled: boolean } | null>(null);
  const currentState = useRef<DocumentProcessing | null>(null);
  const busy = pendingPost || isProcessing(processing);

  /** 接纳一次确认状态，并且只在入库完成后把资料计入列表。 */
  function accept(state: DocumentProcessing | null) {
    currentState.current = state;
    setProcessing(state);
    if (state?.stage === "completed" && state.document) {
      const doc = state.document;
      setDocs((rows) => [...rows.filter((row) => row.id !== doc.id), doc]);
    }
  }

  useEffect(() => {
    let cancelled = false;
    mounted.current = true;
    if (!productId) setLoaded(true);
    else {
      void Promise.all([
        api<Doc[]>(`/api/products/${productId}/documents`),
        api<DocumentProcessing | null>(
          `/api/products/${productId}/document-processing`,
        ),
      ])
        .then(([rows, state]) => {
          if (cancelled || !mounted.current || operation.current) return;
          setDocs(rows);
          accept(state);
          setLoaded(true);
        })
        .catch(() => {
          if (!cancelled && mounted.current)
            setNotice("资料状态读取失败，请刷新页面后重试。");
        });
    }
    return () => {
      cancelled = true;
      mounted.current = false;
    };
  }, [productId]);

  useEffect(() => {
    if (!productId || !busy) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    /** 轮询真实阶段，关联本次请求，避免旧操作的成功覆盖正在上传的文件。 */
    async function poll() {
      try {
        const state = await api<DocumentProcessing | null>(
          `/api/products/${productId}/document-processing`,
        );
        if (stopped || !mounted.current || operation.current?.settled) return;
        if (operation.current && state?.id !== operation.current.id) return;
        if (state) {
          accept(state);
          setNotice("");
        } else {
          const previous = currentState.current;
          if (previous)
            accept({
              ...previous,
              stage: "failed",
              finished_at: Date.now() / 1000,
              error:
                "处理状态已丢失，请刷新资料列表确认是否入库，再决定是否重试。",
            });
        }
      } catch {
        if (!stopped && mounted.current)
          setNotice("暂时无法更新状态，正在重新连接；请勿重复上传。");
      } finally {
        if (!stopped) timer = setTimeout(poll, 800);
      }
    }
    timer = setTimeout(poll, 350);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [productId, busy, processing?.id]);

  /**
   * 上传文件或重建索引，以请求关联 ID 区分轮询结果和之前的操作。
   * @param next - 上传文件或需要重建索引的文档。
   */
  async function submit(next: Attempt) {
    if (!productId || !loaded || busy || deleting) return;
    const id = crypto.randomUUID();
    const initial: DocumentProcessing = {
      id,
      name: "file" in next ? next.file.name : next.document.name,
      stage: "file" in next ? "uploading" : "validating",
      started_at: Date.now() / 1000,
    };
    operation.current = { id, settled: false };
    setAttempt(next);
    setNotice("");
    accept(initial);
    setPendingPost(true);
    const data = new FormData();
    if ("file" in next) data.append("file", next.file);
    const path =
      "file" in next ? "documents" : `documents/${next.document.id}/index`;
    try {
      const document = await api<Doc>(
        `/api/products/${productId}/${path}?upload_id=${id}`,
        {
          method: "POST",
          ...("file" in next ? { body: data } : {}),
        },
      );
      if (!mounted.current) return;
      operation.current = { id, settled: true };
      accept({
        ...initial,
        stage: "completed",
        document,
        finished_at: Date.now() / 1000,
      });
      setNotice("");
    } catch (error) {
      if (!mounted.current) return;
      // 请求断开不等于后端失败；先核对同一次操作，防止将已入库文件误标为失败。
      let recovered: DocumentProcessing | null = null;
      try {
        recovered = await api<DocumentProcessing | null>(
          `/api/products/${productId}/document-processing`,
        );
      } catch {
        /* 原始请求错误仍会展示，用户可刷新后确认。 */
      }
      if (!mounted.current) return;
      const latest = currentState.current;
      const confirmed =
        latest?.id === id && latest.stage === "completed"
          ? latest
          : recovered?.id === id
            ? recovered
            : latest;
      if (
        confirmed?.id === id &&
        (recovered?.id === id || confirmed.stage !== initial.stage)
      ) {
        operation.current = isProcessing(confirmed)
          ? null
          : { id, settled: true };
        accept(confirmed);
        setNotice(
          isProcessing(confirmed)
            ? "上传请求已断开，正在确认后台处理结果。"
            : "",
        );
      } else {
        operation.current = { id, settled: true };
        accept({
          ...initial,
          stage: "failed",
          finished_at: Date.now() / 1000,
          error:
            error instanceof TypeError
              ? "连接中断，未确认入库结果。请刷新列表确认后再重试。"
              : (error as Error).message,
        });
      }
    } finally {
      if (mounted.current) setPendingPost(false);
    }
  }

  /** 删除当前产品的资料，并清除对应的历史成功提示。 */
  async function remove(doc: Doc) {
    if (!productId || busy || deleting) return;
    setDeleting(true);
    try {
      await api(`/api/products/${productId}/documents/${doc.id}`, {
        method: "DELETE",
      });
      if (!mounted.current) return;
      setDocs((rows) => rows.filter((row) => row.id !== doc.id));
      if (currentState.current?.document?.id === doc.id) accept(null);
    } catch (error) {
      if (mounted.current) onError((error as Error).message);
    } finally {
      if (mounted.current) setDeleting(false);
    }
  }

  return {
    docs,
    processing,
    busy,
    loaded,
    deleting,
    notice,
    attempt,
    submit,
    remove,
  };
}
