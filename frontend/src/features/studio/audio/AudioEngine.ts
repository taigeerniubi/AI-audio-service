// 连接浏览器麦克风和 AudioWorklet 并转发播放确认事件。
import type { Message } from "../../../types/api";
import { PlaybackCaptions } from "./PlaybackCaptions";
type Send = (data: object | ArrayBuffer) => void;

export class AudioEngine {
  context?: AudioContext;
  stream?: MediaStream;
  capture?: AudioWorkletNode;
  playback?: AudioWorkletNode;
  epoch = 0;
  private clearTimes = new Map<string, number>();
  private send: Send;
  private level: (value: number) => void;
  private stopped = false;
  private captions: PlaybackCaptions;

  /**
   * 绑定会话消息发送与麦克风电平更新回调。
   *
   * @param send - 发送音频帧或 JSON 控制事件的回调。
   * @param level - 更新当前麦克风音量的回调。
   * @param caption - 接收按实际播放进度揭示的助手字幕。
   */
  constructor(
    send: Send,
    level: (value: number) => void,
    caption: (message: Message) => void,
  ) {
    this.send = send;
    this.level = level;
    this.captions = new PlaybackCaptions(caption);
  }

  /**
   * 申请麦克风并连接采集和播放 Worklet，异步取消时及时释放资源。
   *
   * @returns 设备和节点准备完成后兑现的 Promise；权限或设备错误向调用方传播。
   */
  async open() {
    this.context = new AudioContext({ latencyHint: "interactive" });
    await this.context.resume();
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });
    if (this.stopped) {
      this.close();
      throw new Error("已取消连接");
    }
    await this.context.audioWorklet.addModule("/audio-worklet.js");
    if (this.stopped) {
      this.close();
      throw new Error("已取消连接");
    }
    this.capture = new AudioWorkletNode(this.context, "capture-pcm");
    this.playback = new AudioWorkletNode(this.context, "playback-pcm");
    this.context.createMediaStreamSource(this.stream).connect(this.capture);
    const silent = this.context.createGain();
    silent.gain.value = 0;
    this.capture.connect(silent).connect(this.context.destination);
    this.playback.connect(this.context.destination);
    let frames = 0;
    this.capture.port.onmessage = ({ data }) => {
      if (data.type === "capture") {
        this.send(data.buffer);
        if (++frames % 5 === 0) this.level(data.rms);
      }
    };
    this.playback.port.onmessage = ({ data }) => {
      if (this.stopped) return;
      if (data.type === "progress")
        this.captions.progress(
          data.item_id,
          data.response_id,
          data.epoch,
          data.played_samples,
        );
      if (data.type === "started")
        this.send({
          type: "playback_started",
          response_id: data.response_id,
          epoch: data.epoch,
          queue_wait_ms: performance.now() - data.received_at,
        });
      if (data.type === "complete") {
        this.captions.complete(data.response_id, data.epoch);
        this.send({
          type: "playback_complete",
          response_id: data.response_id,
          epoch: data.epoch,
        });
      }
      if (data.type === "cleared" && data.interruption_id) {
        const start =
          this.clearTimes.get(data.interruption_id) ?? performance.now();
        const playback = this.captions.interrupt(data.ack_epoch ?? data.epoch);
        this.send({
          type: "playback_cleared",
          interruption_id: data.interruption_id,
          clear_ms: performance.now() - start,
          playback,
        });
        this.captions.acknowledge(playback);
        this.clearTimes.delete(data.interruption_id);
      }
    };
  }
  /**
   * 模型会话配置完成后才允许采集节点上送音频帧。
   */
  enable() {
    this.send({ type: "playback_sync", version: 1 });
    this.capture?.port.postMessage({ type: "enable", enabled: true });
  }
  /**
   * 同步采集端和服务端的静音状态，保留会话连接。
   *
   * @param muted - 是否把麦克风输入替换为零值音频。
   */
  mute(muted: boolean) {
    this.capture?.port.postMessage({ type: "mute", muted });
    this.send({ type: "mute", muted });
    if (muted) this.level(0);
  }
  /**
   * 提升播放版本并立即请求清空队列，记录可选的打断确认计时。
   *
   * @param epoch - 目标播放版本，不能回退到旧版本。
   * @param interruption_id - 服务端打断标识；手动本地清空时可省略。
   */
  clear(epoch: number, interruption_id?: string) {
    this.epoch = Math.max(this.epoch, epoch);
    const playback = this.captions.interrupt(epoch);
    if (interruption_id)
      this.clearTimes.set(interruption_id, performance.now());
    this.playback?.port.postMessage({
      type: "clear",
      epoch: this.epoch,
      ack_epoch: epoch,
      interruption_id,
    });
    return playback;
  }

  /** 缓存助手生成文本，只有实际播放进度允许时才向页面展示。 */
  transcript(message: Message) {
    this.captions.text(message);
  }
  /**
   * 丢弃旧版本音频，将当前 Base64 PCM 转为可转移缓冲发送给 Worklet。
   *
   * @param event - 包含 audio、epoch 和 response_id 的下行音频事件。
   */
  audio(event: {
    audio: string;
    epoch: number;
    response_id: string;
    item_id?: string;
  }) {
    if (event.epoch !== this.epoch) return;
    const bytes = Uint8Array.from(atob(event.audio), (c) => c.charCodeAt(0));
    const itemId = event.item_id || event.response_id;
    this.captions.audio(
      itemId,
      event.response_id,
      event.epoch,
      bytes.byteLength / 2,
    );
    this.playback?.port.postMessage(
      {
        type: "audio",
        buffer: bytes.buffer,
        epoch: event.epoch,
        response_id: event.response_id,
        item_id: itemId,
        received_at: performance.now(),
      },
      [bytes.buffer],
    );
  }
  /**
   * 为正常生成结束追加播放完成标记，等待队列真正消费完。
   *
   * @param event - 包含响应 ID、播放版本和取消状态的完成事件。
   */
  done(event: { epoch: number; response_id: string; cancelled: boolean }) {
    if (!event.cancelled && event.epoch === this.epoch) {
      this.captions.generated(event.response_id);
      this.playback?.port.postMessage({ ...event, type: "end" });
    }
  }
  /**
   * 停止麦克风轨道、断开节点并关闭音频上下文，防止迟到初始化恢复采集。
   */
  close() {
    this.stopped = true;
    this.captions.interrupt(this.epoch + 1);
    this.playback?.port.postMessage({ type: "clear", epoch: this.epoch + 1 });
    this.stream?.getTracks().forEach((track) => track.stop());
    this.capture?.disconnect();
    this.playback?.disconnect();
    if (this.context && this.context.state !== "closed")
      void this.context.close();
    this.level(0);
  }
}
