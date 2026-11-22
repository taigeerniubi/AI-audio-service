// 注入合成麦克风并观测真实 WebSocket 与 AudioWorklet 的时间点。

/** 在测试页面初始化观测器；不会申请或读取实体麦克风。 */
export function installInstrumentation() {
  const bench = (window.__voiceBench = {
    events: [], inputs: [], currentInput: null, playing: {}, sockets: [],
    timeOrigin: performance.timeOrigin, fixtureContext: null, fixtureOutput: null,
  });
  /**
   * 记录统一浏览器单调时钟上的观测事件。
   *
   * @param kind - 输入、播放或网络等事件分类。
   * @param detail - 不含原始音频和凭证的观测字段。
   * @returns 刚加入观测列表的时间线记录。
   */
  const mark = (kind, detail = {}) => {
    const row = { kind, at: performance.now(), ...detail };
    bench.events.push(row);
    return row;
  };
  const NativeSocket = window.WebSocket;
  window.WebSocket = class ObservedSocket extends NativeSocket {
    /**
     * 登记真实连接，观测脱敏消息并在断线时中止合成输入。
     *
     * @param args - 转发给原生 WebSocket 的参数。
     */
    constructor(...args) {
      super(...args);
      bench.sockets.push(this);
      this.addEventListener('message', ({ data }) => {
        const event = JSON.parse(data);
        const { audio, ...safe } = event;
        mark('server', { event: { ...safe, ...(audio ? { audio_bytes: Math.floor(audio.length * 3 / 4) } : {}) } });
      });
      this.addEventListener('close', ({ code }) => {
        mark('socket_close', { code });
        bench.currentInput?.abort?.(new Error(`Socket closed during synthetic input: ${code}`));
      });
    }
    /**
     * 记录 JSON 控制事件并原样发送，二进制音频不写入日志。
     *
     * @param data - 应用提交给 WebSocket 的消息。
     */
    send(data) {
      if (typeof data === 'string') mark('client', { event: JSON.parse(data) });
      super.send(data);
    }
  };
  const NativeWorklet = window.AudioWorkletNode;
  window.AudioWorkletNode = class ObservedWorklet extends NativeWorklet {
    /**
     * 监听真实采集与播放回调，记录有效输入边界和清空时间。
     *
     * @param context - 应用实际使用的音频上下文。
     * @param name - 采集或播放处理器名称。
     * @param args - 其余原生构造参数。
     */
    constructor(context, name, ...args) {
      super(context, name, ...args);
      this.port.addEventListener('message', ({ data }) => {
        if (name === 'capture-pcm') {
          const input = bench.currentInput;
          if (input && data.type === 'capture' && data.rms >= 0.002) {
            input.captureStartAt ??= performance.now();
            input.captureEndAt = performance.now();
            input.nonSilentFrames = (input.nonSilentFrames ?? 0) + 1;
          }
          return;
        }
        if (data.type === 'started') bench.playing[data.response_id] = { epoch: data.epoch, at: performance.now() };
        if (data.type === 'complete') delete bench.playing[data.response_id];
        if (data.type === 'cleared') {
          data.cleared_responses = Object.keys(bench.playing);
          bench.playing = {};
        }
        mark('playback', { event: data, context_time: context.currentTime, sample_rate: context.sampleRate });
      });
      this.port.start();
    }
  };
  navigator.mediaDevices.getUserMedia = async () => {
    bench.fixtureContext = new AudioContext({ sampleRate: 48000, latencyHint: 'interactive' });
    await bench.fixtureContext.resume();
    bench.fixtureOutput = bench.fixtureContext.createMediaStreamDestination();
    // 零值常量让输入在语料间隙持续产生静音 PCM，不依赖真实设备。
    const silence = bench.fixtureContext.createConstantSource();
    silence.offset.value = 0;
    silence.connect(bench.fixtureOutput);
    silence.start();
    return bench.fixtureOutput.stream;
  };
  bench.play = async (name, label = name) => {
    if (bench.currentInput) throw new Error('Another synthetic utterance is active');
    const manifest = await fetch('/acceptance/fixtures/manifest.json').then((response) => response.json());
    const raw = await fetch(`/acceptance/fixtures/${name}.wav`).then((response) => response.arrayBuffer());
    const buffer = await bench.fixtureContext.decodeAudioData(raw);
    const source = bench.fixtureContext.createBufferSource();
    source.buffer = buffer;
    source.connect(bench.fixtureOutput);
    const input = { name, label, ...manifest[name], at: performance.now(), playingAtStart: Object.keys(bench.playing) };
    bench.inputs.push(input);
    bench.currentInput = input;
    mark('fixture_start', { name, label });
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => input.abort(new Error('Synthetic input did not finish within its time budget')), buffer.duration * 1000 + 5000);
      input.abort = (error) => {
        clearTimeout(timer);
        source.onended = null;
        source.stop();
        input.error = error.message;
        delete input.abort;
        bench.currentInput = null;
        reject(error);
      };
      source.onended = () => {
        clearTimeout(timer);
        delete input.abort;
        resolve();
      };
      source.start();
    });
    // 等待流适配和 20ms 采集帧排空；边界取实际发出的最后一个非静音帧。
    await new Promise((resolve) => setTimeout(resolve, 150));
    input.finishedAt = performance.now();
    bench.currentInput = null;
    mark('fixture_end', { name, label, captureStartAt: input.captureStartAt, captureEndAt: input.captureEndAt });
    return input;
  };
  bench.snapshot = () => ({ events: bench.events, inputs: bench.inputs, playing: bench.playing, timeOrigin: bench.timeOrigin });
  bench.shutdown = async () => {
    for (const socket of bench.sockets) {
      if (socket.readyState === NativeSocket.OPEN) socket.send(JSON.stringify({ type: 'stop' }));
    }
    if (bench.fixtureContext) await bench.fixtureContext.close();
  };
}
