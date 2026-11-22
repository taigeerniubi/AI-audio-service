// 用模拟接口和合成麦克风验证浏览器交互，不调用云模型。
import { resolve } from "node:path";
process.env.PLAYWRIGHT_BROWSERS_PATH ||= resolve(".cache/playwright");
const { chromium } = await import("playwright");
import { existsSync, mkdirSync } from "node:fs";
import assert from "node:assert/strict";

const out = resolve("../output/ui-redesign");
mkdirSync(out, { recursive: true });

// All /api and voice sockets are mocked: this test cannot spend model credits.
const executablePath = [
  process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
].find((p) => p && existsSync(p));
const browser = await chromium.launch({
  headless: true,
  executablePath,
  args: [
    "--use-fake-device-for-media-stream",
    "--use-fake-ui-for-media-stream",
  ],
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  permissions: ["microphone"],
  recordVideo: {
    dir: resolve(out, "videos"),
    size: { width: 1440, height: 1000 },
  },
});
const page = await context.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
let socketCount = 0;
let sessionOptions;
let products = [
  {
    id: "demo",
    version: 1,
    name: "知答 AI 客服 · 示例产品",
    audience: "中小企业客服团队",
    features: "资料问答、人工转接、知识溯源",
    pricing: "基础版 299 元/月",
    limitations: "虚构演示产品",
    opening: "您好，我是知答的 AI 产品顾问。",
    goal: "了解需求并预约演示",
    style: "简洁自然",
  },
];
let docs = [];
const report = {
  session_id: "test",
  model: "qwen-audio-3.1-realtime-plus",
  duration_seconds: 5,
  transcripts: [],
  metrics: [],
  events: [],
  actions: [],
  estimated_cost_cny: null,
  measurement_note: "浏览器自动化模拟数据；非真实模型性能。",
};
await page.route("**/api/**", async (route) => {
  const url = new URL(route.request().url()),
    method = route.request().method();
  // Vite 开发页会请求 /src/api/client.ts，不能把模块源码当成后端接口拦截。
  if (!url.pathname.startsWith("/api/")) return route.continue();
  let value = {};
  if (url.pathname === "/api/health")
    value = {
      configured: true,
      missing: [],
      model: report.model,
      region: "cn-beijing",
      voice: "longanhuan_v3.1",
      voices: [
        { id: "longanhuan_v3.1", name: "龙安欢 · 女声" },
        { id: "xunanchuan_v3.1", name: "许南川 · 男声" },
      ],
      turn_detection: "smart_turn_v2",
    };
  else if (url.pathname === "/api/products" && method === "GET")
    value = products;
  else if (url.pathname === "/api/products" && method === "POST") {
    const p = { ...route.request().postDataJSON(), id: "second", version: 1 };
    products.push(p);
    value = p;
  } else if (url.pathname.endsWith("/document-processing")) {
    value = null;
  } else if (url.pathname.endsWith("/documents")) {
    if (method === "POST")
      docs.push({
        id: "doc",
        name: "faq.md",
        characters: 30,
        index_status: "ready",
        chunks: 1,
      });
    value = method === "GET" ? docs : docs.at(-1);
  } else if (url.pathname === "/api/sessions" && method === "POST") {
    sessionOptions = route.request().postDataJSON();
    value = { id: "test", websocket_path: "/ws/sessions/test" };
  } else if (url.pathname === "/api/sessions")
    value = [
      {
        id: "test",
        created_at: new Date().toISOString(),
        status: "ended",
        product_id: "demo",
      },
    ];
  else if (url.pathname.endsWith("/report")) value = report;
  await route.fulfill({ json: value });
});
await page.routeWebSocket("**/ws/sessions/*", (socket) => {
  socketCount++;
  /**
   * 将假模型事件编码后发给页面 WebSocket。
   *
   * @param obj - 模拟的服务端协议事件。
   */
  const send = (obj) => socket.send(JSON.stringify(obj));
  let frames = 0;
  setTimeout(() => send({ type: "ready" }), 50);
  socket.onMessage((message) => {
    if (typeof message !== "string") {
      if (++frames !== 2) return;
      send({ type: "response_started", response_id: "r1", epoch: 0 });
      send({
        type: "transcript",
        message: {
          id: "i1",
          response_id: "r1",
          epoch: 0,
          role: "assistant",
          final: true,
          text: "您好，我是知答的 AI 产品顾问。您团队目前最想改善哪类客户咨询？",
        },
      });
      // Silence PCM tests real AudioWorklet processing without impersonating model speech.
      send({
        type: "audio",
        response_id: "r1",
        item_id: "i1",
        epoch: 0,
        audio: Buffer.alloc(48000).toString("base64"),
      });
      send({
        type: "response_done",
        response_id: "r1",
        epoch: 0,
        cancelled: false,
      });
      return;
    }
    const event = JSON.parse(message);
    if (event.type === "interrupt") {
      send({
        type: "clear_audio",
        epoch: 1,
        interruption_id: "i1",
        source: "button",
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
  await page.goto(process.env.APP_URL || "http://127.0.0.1:8000");
  await page
    .getByRole("heading", { name: "产品语音对话", exact: true })
    .waitFor();
  await page.getByText("服务已配置", { exact: true }).waitFor();
  await page.evaluate(() => document.fonts.ready);
  assert.equal(
    await page.evaluate(() =>
      document.fonts.check('14px "Source Han Sans CN"', "产品语音对话"),
    ),
    true,
  );
  assert.equal(
    await page.getByLabel("选择音色").inputValue(),
    "longanhuan_v3.1",
  );
  assert.equal(
    await page
      .getByRole("button", { name: /运行观测/ })
      .getAttribute("aria-expanded"),
    "false",
  );
  assert.equal(
    await page.getByText("首音频响应", { exact: true }).isVisible(),
    false,
  );
  for (const [width, height] of [
    [1440, 900],
    [1024, 768],
    [820, 1180],
    [768, 1024],
    [390, 844],
  ]) {
    await page.setViewportSize({ width, height });
    await page.evaluate(() => window.scrollTo(0, 0));
    const startBounds = await page
      .getByRole("button", { name: "开始对话", exact: true })
      .boundingBox();
    assert.ok(
      startBounds && startBounds.y + startBounds.height <= height,
      `start action below fold at ${width}`,
    );
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
      false,
      `studio overflow at ${width}`,
    );
    await page.screenshot({
      path: resolve(out, `studio-${width}.png`),
      fullPage: true,
      animations: "disabled",
    });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole("button", { name: /运行观测/ }).click();
  await page.getByText("首音频响应", { exact: true }).waitFor();
  await page.getByRole("button", { name: /运行观测/ }).click();
  assert.equal(await page.getByLabel("回复节奏").count(), 0);
  await page.getByLabel("选择音色").selectOption("xunanchuan_v3.1");
  await page.getByRole("button", { name: "开始对话" }).click();
  await page
    .getByText(
      "您好，我是知答的 AI 产品顾问。您团队目前最想改善哪类客户咨询？",
      { exact: true },
    )
    .waitFor();
  assert.equal(sessionOptions.voice, "xunanchuan_v3.1");
  assert.equal(sessionOptions.turn_detection, "smart_turn_v2");
  assert.equal(await page.getByLabel("选择音色").isDisabled(), true);
  await page.getByRole("button", { name: "停止回答", exact: true }).click();
  await page.getByRole("button", { name: "静音麦克风" }).click();
  await page.getByRole("button", { name: "开启麦克风" }).click();
  await page.getByRole("button", { name: "结束", exact: true }).click();
  await page.getByRole("heading", { name: "对话已结束" }).waitFor();
  assert.equal(await page.getByLabel("选择音色").isEnabled(), true);
  await page.getByRole("button", { name: "查看会话报告", exact: true }).click();
  await page.getByRole("button", { name: "导出 JSON" }).waitFor();
  assert.equal(socketCount, 1);
  await page.getByRole("button", { name: "产品与知识", exact: true }).click();
  assert.equal(
    await page.getByLabel("开场白", { exact: true }).isVisible(),
    false,
  );
  await page.getByText("顾问表达与目标", { exact: true }).click();
  await page.getByLabel("开场白", { exact: true }).waitFor();
  await page.getByText("顾问表达与目标", { exact: true }).click();
  await page.getByRole("button", { name: "新建产品" }).click();
  await page.getByLabel("产品名称", { exact: true }).fill("新的演示产品");
  await page.getByRole("button", { name: "保存产品" }).click();
  await page.getByRole("button", { name: "已保存" }).waitFor();
  await page.getByLabel("上传产品资料", { exact: true }).setInputFiles({
    name: "faq.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# 常见问题\n测试资料"),
  });
  await page
    .locator(".document strong")
    .filter({ hasText: "faq.md" })
    .waitFor();
  for (const [width, height] of [
    [1440, 900],
    [1024, 768],
    [820, 1180],
    [768, 1024],
    [390, 844],
  ]) {
    await page.setViewportSize({ width, height });
    await page.evaluate(() => window.scrollTo(0, 0));
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
      false,
      `products overflow at ${width}`,
    );
    await page.screenshot({
      path: resolve(out, `products-${width}.png`),
      fullPage: true,
      animations: "disabled",
    });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole("button", { name: "会话报告", exact: true }).click();
  await page.getByRole("button", { name: /知答 AI 客服/ }).click();
  await page.getByRole("button", { name: "导出 JSON" }).waitFor();
  assert.equal(
    await page.getByText("首音频 P50", { exact: true }).isVisible(),
    false,
  );
  await page.getByText("响应指标与用量", { exact: true }).click();
  await page.getByText("首音频 P50", { exact: true }).waitFor();
  for (const [width, height] of [
    [1440, 900],
    [1024, 768],
    [820, 1180],
    [768, 1024],
    [390, 844],
  ]) {
    await page.setViewportSize({ width, height });
    await page.evaluate(() => window.scrollTo(0, 0));
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
      false,
      `reports overflow at ${width}`,
    );
    await page.screenshot({
      path: resolve(out, `reports-${width}.png`),
      fullPage: true,
      animations: "disabled",
    });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "语音演示", exact: true }).click();
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  await page.screenshot({
    path: resolve(out, "mobile-layout.png"),
    fullPage: true,
    animations: "disabled",
  });
  assert.deepEqual(errors, []);
  console.log(
    "Browser smoke passed: audio worklet, start/stop/interrupt/mute, products, upload, reports, narrow layout. Mock provider only.",
  );
} finally {
  await context.close();
  await browser.close();
}
