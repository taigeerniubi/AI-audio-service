// 在音频渲染线程中重采样麦克风帧，并按播放版本处理回复音频。
class Capture extends AudioWorkletProcessor {
  /**
   * 初始化连续重采样状态、20 ms 帧缓冲和采集控制消息。
   */
  constructor() {
    super();
    this.acc = 0;
    this.sum = 0;
    this.count = 0;
    this.frame = new Int16Array(320);
    this.offset = 0;
    this.enabled = false;
    this.muted = false;
    this.port.onmessage = ({ data }) => {
      if (data.type === "enable") this.enabled = data.enabled;
      if (data.type === "mute") this.muted = data.muted;
    };
  }
  /**
   * 将输入单声道降采样为 16 kHz PCM16，静音时发送零值帧。
   *
   * @param inputs - 浏览器提供的各输入节点和声道样本。
   * @returns 持续处理时返回 true。
   */
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input || !this.enabled) return true;
    const ratio = sampleRate / 16000;
    for (let i = 0; i < input.length; i++) {
      const value = this.muted ? 0 : input[i];
      this.sum += value;
      this.count++;
      this.acc++;
      if (this.acc >= ratio) {
        const v = Math.max(-1, Math.min(1, this.sum / this.count));
        this.frame[this.offset++] = v < 0 ? v * 32768 : v * 32767;
        this.acc -= ratio;
        this.sum = 0;
        this.count = 0;
        if (this.offset === 320) {
          let energy = 0;
          for (const n of this.frame) energy += (n / 32768) ** 2;
          this.port.postMessage(
            {
              type: "capture",
              buffer: this.frame.buffer,
              rms: Math.sqrt(energy / 320),
            },
            [this.frame.buffer],
          );
          this.frame = new Int16Array(320);
          this.offset = 0;
        }
      }
    }
    return true;
  }
}

class Playback extends AudioWorkletProcessor {
  /**
   * 初始化播放队列，并通过 epoch 拒绝打断前的迟到音频。
   */
  constructor() {
    super();
    this.queue = [];
    this.head = 0;
    this.epoch = 0;
    this.queued = 0;
    this.started = new Set();
    this.positions = new Map();
    this.progressFrames = 0;
    this.port.onmessage = ({ data }) => {
      if (data.type === "clear") {
        this.queue = [];
        this.head = 0;
        this.queued = 0;
        this.epoch = data.epoch;
        this.started.clear();
        this.positions.clear();
        this.port.postMessage({ ...data, type: "cleared" });
      } else if (data.epoch === this.epoch && data.type === "audio") {
        const samples = new Int16Array(data.buffer);
        if (!samples.length) return;
        this.queue.push({ ...data, samples, cursor: 0 });
        this.queued += samples.length;
      } else if (data.epoch === this.epoch && data.type === "end") {
        this.queue.push(data);
      }
    };
  }
  /**
   * 将 24 kHz PCM 插值到设备采样率，并回报首播放和完整播放事件。
   *
   * @param inputs - Worklet 协议输入，本播放节点不使用。
   * @param outputs - 待填充的设备输出样本，队列为空时输出静音。
   * @returns 持续处理时返回 true。
   */
  process(inputs, outputs) {
    const output = outputs[0]?.[0];
    if (!output) return true;
    output.fill(0);
    let i = 0;
    while (i < output.length && this.head < this.queue.length) {
      const item = this.queue[this.head];
      if (item.type === "end") {
        this.queue[this.head++] = null;
        this.reportProgress();
        this.port.postMessage({
          type: "complete",
          response_id: item.response_id,
          epoch: item.epoch,
        });
        this.started.delete(item.response_id);
        for (const [key, position] of this.positions) {
          if (position.response_id === item.response_id)
            this.positions.delete(key);
        }
        continue;
      }
      if (!this.started.has(item.response_id)) {
        this.started.add(item.response_id);
        this.port.postMessage({
          type: "started",
          response_id: item.response_id,
          epoch: item.epoch,
          received_at: item.received_at,
        });
      }
      // Interpolation for 24 kHz output onto the browser's actual render sample rate.
      const pos = Math.floor(item.cursor),
        fraction = item.cursor - pos;
      const next = item.samples[Math.min(pos + 1, item.samples.length - 1)];
      output[i++] =
        (item.samples[pos] * (1 - fraction) + next * fraction) / 32768;
      item.cursor += 24000 / sampleRate;
      const key = item.item_id || item.response_id;
      const position = this.positions.get(key) || {
        response_id: item.response_id,
        item_id: key,
        epoch: item.epoch,
        played_samples: 0,
      };
      const consumed = Math.min(
        24000 / sampleRate,
        item.samples.length - (item.cursor - 24000 / sampleRate),
      );
      position.played_samples += consumed;
      this.queued = Math.max(0, this.queued - consumed);
      this.positions.set(key, position);
      if (item.cursor >= item.samples.length) {
        this.queue[this.head++] = null;
      }
    }
    // 每五十毫秒报告实际渲染进度；队列空、设备暂停时字幕不会靠墙钟继续前进。
    this.progressFrames += output.length;
    if (this.progressFrames >= sampleRate / 20) {
      this.reportProgress();
      this.progressFrames = 0;
    }
    if (this.head === this.queue.length || this.head > 256) {
      this.queue = this.queue.slice(this.head);
      this.head = 0;
    }
    return true;
  }

  /** 将各输出项实际消费的 PCM 样本数发送到主线程。 */
  reportProgress() {
    for (const position of this.positions.values()) {
      this.port.postMessage({
        ...position,
        type: "progress",
        queued_ms: this.queued / 24,
      });
    }
  }
}
registerProcessor("capture-pcm", Capture);
registerProcessor("playback-pcm", Playback);
