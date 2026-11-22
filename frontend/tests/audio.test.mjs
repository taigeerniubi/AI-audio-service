// 使用隔离 Worklet 验证采集、长音频排队、实际播放进度和打断释放。
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

/**
 * 在隔离 VM 中加载真实 Worklet 源码，并注入可观测的处理器基类。
 *
 * @param sampleRate - 模拟设备采样率，默认 48 kHz。
 * @returns 按注册名称索引的 Worklet 处理器类。
 */
function setup(sampleRate = 48000) {
  const processors = {};
  class Base {
    /**
     * 建立能收集 Worklet 输出事件的假消息端口。
     */
    constructor() {
      this.port = {
        events: [],
        /**
         * 收集处理器发送到主线程的消息。
         *
         * @param e - 待断言的 Worklet 事件。
         */
        postMessage(e) {
          this.events.push(e);
        },
      };
    }
  }
  vm.runInNewContext(fs.readFileSync("public/audio-worklet.js", "utf8"), {
    AudioWorkletProcessor: Base,
    registerProcessor: (n, p) => (processors[n] = p),
    sampleRate,
    Int16Array,
    Math,
    Set,
  });
  return processors;
}
test("capture emits exact 20ms PCM frames and silence when muted", () => {
  const processors = setup();
  const capture = new processors["capture-pcm"]();
  capture.port.onmessage({ data: { type: "enable", enabled: true } });
  for (let i = 0; i < 15; i++)
    capture.process([[new Float32Array(128).fill(0.5)]]);
  assert.equal(capture.port.events.length, 2);
  assert.equal(capture.port.events[0].buffer.byteLength, 640);
  capture.port.onmessage({ data: { type: "mute", muted: true } });
  for (let i = 0; i < 15; i++)
    capture.process([[new Float32Array(128).fill(0.5)]]);
  assert.equal(capture.port.events.at(-1).rms, 0);
});
test("playback clears in render thread and rejects stale audio epochs", () => {
  const processors = setup(24000);
  const player = new processors["playback-pcm"]();
  /**
   * 向播放节点注入主线程控制消息。
   *
   * @param data - 音频、清空或播放结束事件。
   */
  const send = (data) => player.port.onmessage({ data });
  send({
    type: "audio",
    epoch: 0,
    response_id: "old",
    buffer: new Int16Array(320).fill(16000).buffer,
  });
  /**
   * 执行一个渲染量子并返回实际输出样本。
   *
   * @returns 长度为 128 的 Float32Array。
   */
  const render = () => {
    const out = new Float32Array(128);
    player.process([], [[out]]);
    return out;
  };
  assert(render().some((x) => x > 0));
  send({ type: "clear", epoch: 1, interruption_id: "interrupt" });
  send({
    type: "audio",
    epoch: 0,
    response_id: "old",
    buffer: new Int16Array(320).fill(16000).buffer,
  });
  assert(render().every((x) => x === 0));
  assert(player.port.events.some((e) => e.type === "cleared"));
  send({
    type: "audio",
    epoch: 1,
    response_id: "new",
    buffer: new Int16Array(128).fill(16000).buffer,
  });
  send({ type: "end", epoch: 1, response_id: "new" });
  render();
  render();
  assert(
    player.port.events.some(
      (e) => e.type === "complete" && e.response_id === "new",
    ),
  );
});
test("long audio queues without overflow and releases consumed buffers", () => {
  const processors = setup();
  const player = new processors["playback-pcm"]();
  player.port.onmessage({
    data: {
      type: "audio",
      epoch: 0,
      response_id: "huge",
      item_id: "long-item",
      buffer: new Int16Array(24000 * 90).fill(16000).buffer,
    },
  });
  assert.equal(
    player.port.events.some((event) => event.type === "overflow"),
    false,
  );
  assert.equal(player.queued, 24000 * 90);
  for (let i = 0; i < 375; i++) player.process([], [[new Float32Array(128)]]);
  assert.equal(player.queued, 24000 * 89);
  const progress = player.port.events
    .filter((event) => event.type === "progress")
    .at(-1);
  assert.ok(
    progress.played_samples > 22000 && progress.played_samples <= 24000,
  );
  player.port.onmessage({ data: { type: "clear", epoch: 1 } });
  assert.equal(player.queue.length, 0);
  assert.equal(player.queued, 0);
  assert.equal(player.started.size, 0);
  assert.equal(player.positions.size, 0);
  player.port.onmessage({
    data: {
      type: "audio",
      epoch: 0,
      response_id: "huge",
      buffer: new Int16Array(24000).buffer,
    },
  });
  assert.equal(player.queued, 0);
});

test("playback completion follows all queued samples, including stalled arrival", () => {
  const player = new (setup(24000)["playback-pcm"])();
  player.port.onmessage({
    data: {
      type: "audio",
      epoch: 0,
      response_id: "r",
      item_id: "a",
      buffer: new Int16Array(256).buffer,
    },
  });
  player.process([], [[new Float32Array(128)]]);
  assert.equal(player.queued, 128);
  player.process([], [[new Float32Array(128)]]);
  assert.equal(player.queued, 0);
  assert.equal(player.queue.length, 0);
  for (let i = 0; i < 100; i++) player.process([], [[new Float32Array(128)]]);
  assert.equal(
    player.port.events.filter((event) => event.type === "progress").at(-1)
      .played_samples,
    256,
  );
  assert.equal(
    player.port.events.some((event) => event.type === "complete"),
    false,
  );
  player.port.onmessage({ data: { type: "end", epoch: 0, response_id: "r" } });
  player.process([], [[new Float32Array(128)]]);
  assert.equal(
    player.port.events.filter((event) => event.type === "complete").length,
    1,
  );
  assert.equal(player.positions.size, 0);
});
