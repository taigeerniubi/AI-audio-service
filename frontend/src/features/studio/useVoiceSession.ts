// 管理麦克风、WebSocket、实时字幕及单次对话的生命周期。
import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { AudioEngine } from "./audio/AudioEngine";
import type {
  Product,
  Message,
  Metric,
  Source,
  Report,
  Health,
} from "../../types/api";

/**
 * 持有跨页面存活的语音会话，并按连接版本隔离迟到回调。
 *
 * @param options - 当前产品、服务器音色与判停默认值，以及统一错误显示回调。
 * @returns 会话状态、字幕、观测数据及开始、结束、打断等操作。
 */
export function useVoiceSession({
  selected,
  product,
  health,
  setError,
}: {
  selected: string;
  product?: Product;
  health?: Health;
  setError: (message: string) => void;
}) {
  const [voiceChoice, setSelectedVoice] = useState("");
  const selectedVoice = voiceChoice || health?.voice || "longanhuan_v3.1";
  const turnDetection = health?.turn_detection || "smart_turn_v2";
  const [state, setState] = useState("idle");
  const [messages, setMessages] = useState<Message[]>([]);
  const [traces, setTraces] = useState<{ type: string; elapsed_ms: number }[]>(
    [],
  );
  const [metrics, setMetrics] = useState<Metric[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [muted, setMuted] = useState(false);
  const [volume, setVolume] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [report, setReport] = useState<Report>();
  const ws = useRef<WebSocket | undefined>(undefined);
  const engine = useRef<AudioEngine | undefined>(undefined);
  const sessionId = useRef("");
  const sessionProduct = useRef<Product | undefined>(undefined);
  const alive = useRef(false);
  const attempt = useRef(0);
  const heartbeatTimer = useRef<number | undefined>(undefined);
  const stopTimer = useRef<number | undefined>(undefined);
  const expectedClose = useRef(false);
  const connectionFailure = useRef("");

  const live = !["idle", "ended"].includes(state);

  /** 合并用户实时转录或已经播放的助手字幕，删除完全未播放的取消项。 */
  function updateMessage(message: Message) {
    setMessages((old) => {
      if (message.interrupted && !message.text)
        return old.filter((row) => row.id !== message.id);
      const index = old.findIndex((row) => row.id === message.id);
      return index === -1
        ? [...old, message]
        : old.map((row, i) => (i === index ? message : row));
    });
  }
  useEffect(
    () => () => {
      alive.current = false;
      stopHeartbeat();
      window.clearTimeout(stopTimer.current);
      engine.current?.close();
      ws.current?.close();
    },
    [],
  );
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => setElapsed((e) => e + 1), 1000);
    return () => clearInterval(timer);
  }, [live]);
  /**
   * 发送当前连接的音频或控制帧，积压超过上限时结束会话。
   *
   * @param data - 原始 PCM ArrayBuffer 或可序列化的控制对象。
   */
  const send = (data: object | ArrayBuffer) => {
    const socket = ws.current;
    if (!alive.current || !socket || socket.readyState !== WebSocket.OPEN)
      return;
    if (socket.bufferedAmount > 128000) {
      connectionFailure.current = "音频发送积压，请检查网络后重新开始。";
      setError(connectionFailure.current);
      cleanup();
      socket.close();
      return;
    }
    socket.send(data instanceof ArrayBuffer ? data : JSON.stringify(data));
  };
  /**
   * 读取会话报告，并防止旧连接结果覆盖新会话。
   *
   * @param id - 目标会话 ID，默认当前会话。
   * @param quiet - 失败时是否保留现有提示，避免覆盖主要断线原因。
   */
  async function loadReport(id = sessionId.current, quiet = false) {
    if (!id) return;
    const generation = attempt.current;
    try {
      const result = await api<Report>(`/api/sessions/${id}/report`);
      if (generation === attempt.current) setReport(result);
    } catch (e) {
      if (!quiet && generation === attempt.current)
        setError((e as Error).message);
    }
  }
  /** 清除连接探测定时器，避免已结束会话继续发送心跳。 */
  function stopHeartbeat() {
    window.clearInterval(heartbeatTimer.current);
    heartbeatTimer.current = undefined;
  }
  /**
   * 清除计时器和音频资源并切换为结束状态，保留已有文字。
   */
  function cleanup() {
    alive.current = false;
    stopHeartbeat();
    window.clearTimeout(stopTimer.current);
    stopTimer.current = undefined;
    const audio = engine.current;
    engine.current = undefined;
    audio?.close();
    setState("ended");
    setMuted(false);
  }
  /**
   * 创建固定产品版本的会话，连接麦克风和 WebSocket 并启动心跳探测。
   */
  async function start() {
    if (!selected || live) return;
    const generation = ++attempt.current;
    stopHeartbeat();
    window.clearTimeout(stopTimer.current);
    ws.current?.close();
    expectedClose.current = false;
    connectionFailure.current = "";
    setError("");
    setMessages([]);
    setTraces([]);
    setMetrics([]);
    setSources([]);
    setReport(undefined);
    setElapsed(0);
    setState("connecting");
    alive.current = true;
    sessionProduct.current = product;
    const audio = new AudioEngine(
      (data) => {
        if (generation === attempt.current) send(data);
      },
      (value) => {
        if (generation === attempt.current) setVolume(value);
      },
      (message) => {
        if (generation === attempt.current) updateMessage(message);
      },
    );
    engine.current = audio;
    try {
      await audio.open();
      if (!alive.current || generation !== attempt.current) {
        audio.close();
        return;
      }
      const session = await api<{ id: string; websocket_path: string }>(
        "/api/sessions",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            product_id: selected,
            voice: selectedVoice,
            turn_detection: turnDetection,
          }),
        },
      );
      if (!alive.current || generation !== attempt.current) {
        audio.close();
        return;
      }
      sessionId.current = session.id;
      const socket = new WebSocket(
        `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${session.websocket_path}`,
      );
      ws.current = socket;
      const connectedAt = performance.now();
      let lastPong = connectedAt;
      let sequence = 0;
      let acknowledged = 0;
      heartbeatTimer.current = window.setInterval(() => {
        if (generation !== attempt.current || !alive.current) return;
        if (performance.now() - lastPong >= 12000) {
          connectionFailure.current =
            "连接长时间无响应，已停止收音和播放，请重新开始。";
          setError(connectionFailure.current);
          setTraces((rows) => [
            ...rows.slice(-99),
            {
              type: "browser_heartbeat_timeout",
              elapsed_ms: performance.now() - connectedAt,
            },
          ]);
          cleanup();
          socket.close(4000, "heartbeat_timeout");
          return;
        }
        if (socket.readyState === WebSocket.OPEN)
          send({ type: "ping", sequence: ++sequence });
      }, 2000);
      socket.onmessage = ({ data }) => {
        if (generation !== attempt.current) return;
        const event = JSON.parse(data);
        // 已结束连接的迟到音频、状态和工具事件不得恢复播放。
        if (!alive.current && event.type !== "ended") return;
        switch (event.type) {
          case "pong":
            if (
              Number.isSafeInteger(event.sequence) &&
              event.sequence > acknowledged &&
              event.sequence <= sequence
            ) {
              acknowledged = event.sequence;
              lastPong = performance.now();
            }
            break;
          case "ready":
            audio.enable();
            setState("listening");
            break;
          case "state":
            setState(event.state);
            break;
          case "response_started":
            setState("thinking");
            break;
          case "audio":
            audio.audio(event);
            setState("speaking");
            break;
          case "clear_audio":
            audio.clear(event.epoch, event.interruption_id);
            break;
          case "response_done":
            audio.done(event);
            break;
          case "transcript":
            if (event.message.role === "assistant")
              audio.transcript(event.message);
            else updateMessage(event.message);
            break;
          case "trace":
            setTraces((t) => [...t.slice(-99), event.event]);
            break;
          case "metric":
            setMetrics((m) => [...m, event.metric]);
            break;
          case "tool_result":
            if (event.result.matches) setSources(event.result.matches);
            break;
          case "error":
            connectionFailure.current = event.message;
            setError(event.message);
            cleanup();
            socket.close();
            break;
          case "ended":
            expectedClose.current = true;
            cleanup();
            void loadReport(session.id, true);
            socket.close();
            break;
        }
      };
      socket.onerror = () => {
        if (generation !== attempt.current) return;
        if (!connectionFailure.current) {
          connectionFailure.current =
            "语音连接失败，请确认后端和网络可用后重新开始。";
          setError(connectionFailure.current);
        }
        cleanup();
        socket.close();
      };
      socket.onclose = ({ code, wasClean }) => {
        if (generation !== attempt.current) return;
        setTraces((rows) => [
          ...rows.slice(-99),
          {
            type: "browser_connection_closed",
            elapsed_ms: performance.now() - connectedAt,
            close_code: code,
            was_clean: wasClean,
          },
        ]);
        if (!expectedClose.current && !connectionFailure.current) {
          connectionFailure.current =
            "连接已中断，请重新开始。已收到的文字保留在本页。";
          setError(connectionFailure.current);
        }
        cleanup();
        void loadReport(session.id, true);
      };
    } catch (e) {
      if (generation !== attempt.current) return;
      const err = e as Error;
      setError(
        err.name === "NotAllowedError"
          ? "麦克风权限未开启，请允许访问后重试。"
          : err.message,
      );
      cleanup();
    }
  }
  /**
   * 发送结束请求并停止本地音频，最多等待四秒获取服务端结束确认。
   */
  function stop() {
    expectedClose.current = true;
    const playback = engine.current?.clear((engine.current?.epoch ?? 0) + 1);
    send({ type: "stop", playback });
    cleanup();
    // 正常情况等待 ended，确保读取最终报告；失联时四秒后关闭本地连接。
    const socket = ws.current;
    if (socket?.readyState === WebSocket.OPEN) {
      stopTimer.current = window.setTimeout(
        () => socket.close(1000, "user_ended"),
        4000,
      );
    }
  }
  /**
   * 立即清空本地播放并通知后端取消旧响应，保留麦克风和连接。
   */
  function interrupt() {
    engine.current?.clear((engine.current?.epoch ?? 0) + 1);
    send({ type: "interrupt" });
  }
  const lastMetric = [...metrics].reverse().find((m) => m.kind === "response");
  const latestLatency = lastMetric?.speech_stop_event_to_play_ack_ms;

  /** 同步采集端和服务端的麦克风状态。 */
  function toggleMute() {
    engine.current?.mute(!muted);
    setMuted(!muted);
  }
  return {
    state,
    live,
    messages,
    traces,
    metrics,
    sources,
    muted,
    volume,
    elapsed,
    selectedVoice,
    setSelectedVoice,
    report,
    start,
    stop,
    interrupt,
    toggleMute,
    loadReport,
    latestLatency,
    sessionProduct: sessionProduct.current,
  };
}

export type VoiceSession = ReturnType<typeof useVoiceSession>;
