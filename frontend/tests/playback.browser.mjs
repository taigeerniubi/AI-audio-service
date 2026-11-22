// 用真实浏览器音频线程与模拟模型验证长队列、随播放字幕、暂停和打断后的继续对话。
import assert from "node:assert/strict";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { chromium } from "playwright";

const out = resolve("../output/playback-browser");
mkdirSync(out, { recursive: true });
const executablePath = [
  process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
].find((path) => path && existsSync(path));
const browser = await chromium.launch({
  headless: true,
  executablePath,
  args: [
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
  ],
});
const page = await browser.newPage({
  permissions: ["microphone"],
  viewport: { width: 1440, height: 1000 },
});
const errors = [];
const controls = [];
page.on("pageerror", (error) => errors.push(error.message));
await page.addInitScript(() => {
  const NativeContext = window.AudioContext;
  window.testContexts = [];
  window.AudioContext = class extends NativeContext {
    /** 保存测试创建的真实音频上下文，用于验证暂停时字幕也停止。 */
    constructor(...args) {
      super(...args);
      window.testContexts.push(this);
    }
  };
});
const product = {
  id: "demo",
  version: 1,
  name: "字幕测试",
  audience: "测试",
  features: "测试",
  pricing: "未知",
  limitations: "测试",
  opening: "你好",
  goal: "测试",
  style: "自然",
};
await page.route("**/api/**", async (route) => {
  const path = new URL(route.request().url()).pathname;
  if (!path.startsWith("/api/")) return route.continue();
  const value =
    path === "/api/health"
      ? { configured: true, missing: [], model: "mock", region: "cn-beijing" }
      : path === "/api/products"
        ? [product]
        : path === "/api/sessions"
          ? { id: "playback", websocket_path: "/ws/sessions/playback" }
          : {
              session_id: "playback",
              transcripts: [],
              actions: [],
              metrics: [],
            };
  await route.fulfill({ json: value });
});
let send;
const text =
  "这是一个较长的硬件介绍，用来验证声音还没播放的时候字幕不会提前出现。".repeat(
    7,
  );

/** 发送一个完整生成的响应，可一次性排入超过旧二十秒上限的音频。 */
function reply(rid, itemId, epoch, content, seconds) {
  send({ type: "response_started", response_id: rid, epoch });
  send({
    type: "transcript",
    message: {
      id: itemId,
      response_id: rid,
      epoch,
      role: "assistant",
      text: content,
      final: true,
    },
  });
  if (seconds) {
    send({
      type: "audio",
      response_id: rid,
      item_id: itemId,
      epoch,
      audio: Buffer.alloc(48000 * seconds).toString("base64"),
    });
    send({ type: "response_done", response_id: rid, epoch, cancelled: false });
  }
}

await page.routeWebSocket("**/ws/sessions/*", (socket) => {
  send = (event) => socket.send(JSON.stringify(event));
  setTimeout(() => send({ type: "ready" }), 20);
  socket.onMessage((message) => {
    if (typeof message !== "string") return;
    const event = JSON.parse(message);
    controls.push(event);
    if (event.type === "ping") send({ type: "pong", sequence: event.sequence });
    if (event.type === "interrupt") {
      send({
        type: "clear_audio",
        epoch: 1,
        interruption_id: "cut-1",
        source: "button",
      });
      send({
        type: "transcript",
        message: {
          id: "a1",
          response_id: "r1",
          epoch: 0,
          role: "assistant",
          text: text + "迟到的未播放内容",
          final: true,
          interrupted: true,
        },
      });
      send({
        type: "audio",
        response_id: "r1",
        item_id: "a1",
        epoch: 0,
        audio: Buffer.alloc(48000).toString("base64"),
      });
      send({
        type: "response_done",
        response_id: "r1",
        epoch: 0,
        cancelled: true,
      });
      send({ type: "state", state: "listening" });
    }
    if (event.type === "stop") {
      send({ type: "ended", reason: "user_ended" });
      socket.close();
    }
  });
});

try {
  await page.goto(process.env.BASE_URL || "http://127.0.0.1:5173");
  await page.getByRole("button", { name: "开始对话", exact: true }).click();
  await page.getByRole("button", { name: "结束", exact: true }).waitFor();
  await page.waitForTimeout(150);
  assert.ok(controls.some((event) => event.type === "playback_sync"));
  reply("r1", "a1", 0, text, 0);
  await page.waitForTimeout(300);
  assert.equal(await page.locator(".message.assistant").count(), 0);
  send({
    type: "audio",
    response_id: "r1",
    item_id: "a1",
    epoch: 0,
    audio: Buffer.alloc(48000 * 45).toString("base64"),
  });
  send({
    type: "response_done",
    response_id: "r1",
    epoch: 0,
    cancelled: false,
  });
  const caption = page.locator(".message.assistant p").first();
  await caption.waitFor();
  const first = await caption.innerText();
  assert.ok(first.length > 0 && first.length < text.length / 4);
  await page.waitForTimeout(500);
  const advanced = await caption.innerText();
  assert.ok(advanced.length > first.length);
  await page.evaluate(() => window.testContexts[0].suspend());
  await page.waitForTimeout(150);
  const paused = await caption.innerText();
  await page.waitForTimeout(400);
  assert.equal(await caption.innerText(), paused);
  await page.evaluate(() => window.testContexts[0].resume());
  await page.waitForTimeout(600);
  assert.ok((await caption.innerText()).length > paused.length);
  await page.screenshot({ path: resolve(out, "playing.png"), fullPage: true });
  await page.getByRole("button", { name: "停止回答", exact: true }).click();
  await page.getByText("已打断 · 后续内容已丢弃", { exact: true }).waitFor();
  const stopped = await caption.innerText();
  await page.waitForTimeout(450);
  assert.equal(await caption.innerText(), stopped);
  const receipt = controls.find(
    (event) =>
      event.type === "playback_cleared" && event.interruption_id === "cut-1",
  );
  assert.ok(receipt);
  assert.equal(
    receipt.playback[0].heard_characters,
    Array.from(stopped).length,
  );
  assert.ok(receipt.playback[0].heard_characters < text.length);
  assert.equal(
    controls.some((event) => event.type === "client_error"),
    false,
  );
  assert.equal(
    await page
      .getByText("音频设备或播放缓冲异常，请重新开始。", { exact: true })
      .count(),
    0,
  );
  reply("r2", "a2", 1, "可以，我们继续聊。", 2);
  await page.getByText("可以，我们继续聊。", { exact: true }).waitFor();
  await page.getByText("播放完成", { exact: true }).waitFor();
  assert.equal(await caption.innerText(), stopped);
  await page.screenshot({
    path: resolve(out, "interrupted-and-continued.png"),
    fullPage: true,
  });
  assert.deepEqual(errors, []);
  writeFileSync(
    resolve(out, "result.json"),
    JSON.stringify(
      { ok: true, queued_seconds: 45, first, paused, stopped, receipt, errors },
      null,
      2,
    ),
  );
  await page.getByRole("button", { name: "结束", exact: true }).click();
  await page.getByRole("heading", { name: "对话已结束" }).waitFor();
  console.log(
    "Playback browser checks passed: 45s queue, audio-driven subtitles, pause/resume, interruption receipt, late-event rejection and next response.",
  );
} finally {
  await browser.close();
}
