// 使用延迟模拟接口验证上传阶段、失败重试、断线恢复及切换产品时的隔离。
import assert from "node:assert/strict";
import { existsSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { chromium } from "playwright";

const out = resolve("../output/knowledge-browser");
mkdirSync(out, { recursive: true });
const executablePath = [
  process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
].find((path) => path && existsSync(path));
const browser = await chromium.launch({ headless: true, executablePath });
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
const product = {
  id: "demo",
  version: 1,
  name: "知答 AI 客服",
  audience: "产品咨询客户",
  features: "资料问答",
  pricing: "待询价",
  limitations: "不虚构价格",
  opening: "您好",
  goal: "了解需求",
  style: "简洁",
};
const products = [product, { ...product, id: "other", name: "另一个产品" }];
const docs = { demo: [], other: [] };
const states = { demo: null, other: null };
const pending = {};
let delayedPoll = null;
let delayNextPoll = false;

/** 等待模拟请求进入指定状态，超时即报告失败，避免依赖固定网络速度。 */
async function until(predicate) {
  const end = Date.now() + 7000;
  while (!predicate()) {
    assert.ok(Date.now() < end, "mock operation timed out");
    await new Promise((done) => setTimeout(done, 30));
  }
}

/** 为指定产品设置真实接口形状的阶段快照。 */
function stage(pid, value, details = {}) {
  states[pid] = {
    id: pending[pid]?.id ?? "resumed",
    name: pending[pid]?.name ?? "恢复上传.pdf",
    started_at: Date.now() / 1000 - 3,
    stage: value,
    ...details,
  };
}

/** 完成一次模拟上传并把文档加入对应产品，不调用本地真实数据库或云模型。 */
async function finish(pid, id) {
  const doc = {
    id,
    name: pending[pid].name,
    characters: 2742,
    chunks: 4,
    index_status: "ready",
    warnings: [],
  };
  docs[pid].push(doc);
  stage(pid, "completed", { document: doc, finished_at: Date.now() / 1000 });
  await pending[pid].route.fulfill({ status: 201, json: doc });
  pending[pid].release();
  delete pending[pid];
}

await page.route("**/api/**", async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  if (!url.pathname.startsWith("/api/")) return route.continue();
  const method = request.method();
  if (url.pathname === "/api/products")
    return route.fulfill({ json: products });
  if (url.pathname === "/api/health")
    return route.fulfill({
      json: {
        configured: true,
        missing: [],
        model: "mock",
        region: "cn-beijing",
      },
    });
  const pid = url.pathname.split("/")[3];
  if (url.pathname.endsWith("/document-processing")) {
    const json = states[pid];
    if (delayNextPoll) {
      delayNextPoll = false;
      return new Promise((release) => {
        delayedPoll = { route, json, release };
      });
    }
    return route.fulfill({ json });
  }
  if (url.pathname.endsWith("/documents")) {
    if (method === "GET") return route.fulfill({ json: docs[pid] });
    if (method === "POST") {
      return new Promise((release) => {
        pending[pid] = {
          route,
          release,
          id: url.searchParams.get("upload_id"),
          name:
            /filename="([^"]+)"/.exec(
              request.postDataBuffer().toString(),
            )?.[1] || "unknown.pdf",
        };
      });
    }
  }
  if (url.pathname.endsWith("/search"))
    return route.fulfill({
      json: {
        matches: [
          {
            source: "9700X.pdf",
            text: "8 核 16 线程，默认 TDP 65 W，AM5 插槽。",
            pages: [1],
          },
        ],
      },
    });
  return route.fulfill({ json: [] });
});

try {
  await page.goto(process.env.BASE_URL || "http://127.0.0.1:5173");
  await page.getByRole("button", { name: "产品与知识", exact: true }).click();
  const fileInput = page.getByLabel("上传产品资料", { exact: true });
  await page.getByText("还没有资料。入库完成后，文件会显示在这里。").waitFor();
  await fileInput.setInputFiles({
    name: "01_AMD_Ryzen_7_9700X.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("mock-pdf"),
  });
  await until(() => pending.demo);
  await page.getByText("正在上传文件", { exact: true }).waitFor();
  assert.equal(await fileInput.isDisabled(), true);
  stage("demo", "parsing");
  await page.getByText("正在解析内容", { exact: true }).waitFor();
  assert.equal(
    await page.getByText("已入库 0 / 20", { exact: true }).count(),
    1,
  );
  await page
    .locator(".knowledge-card")
    .screenshot({ path: resolve(out, "parsing.png") });
  stage("demo", "embedding", { chunks: 4 });
  await page.getByText("正在建立检索索引", { exact: true }).waitFor();
  await page
    .locator(".knowledge-card")
    .screenshot({ path: resolve(out, "indexing.png") });
  stage("demo", "saving");
  await page.getByText("正在保存资料", { exact: true }).waitFor();
  // 保存时挂起一次状态响应，检验 POST 成功后不会被迟到阶段退回处理中。
  delayNextPoll = true;
  await until(() => delayedPoll);
  await finish("demo", "doc-1");
  await page.getByText("已入库，可以检索", { exact: true }).waitFor();
  await delayedPoll.route.fulfill({ json: delayedPoll.json });
  delayedPoll.release();
  delayedPoll = null;
  await page.getByRole("button", { name: "试跑检索", exact: true }).click();
  assert.equal(
    await page
      .getByLabel("检索问题")
      .evaluate((node) => node === document.activeElement),
    true,
  );
  assert.equal(await fileInput.isEnabled(), true);
  assert.equal(await page.locator(".document-ready").count(), 1);
  await page
    .locator(".knowledge-card")
    .screenshot({ path: resolve(out, "completed.png") });

  await fileInput.setInputFiles({
    name: "memory.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("mock-memory"),
  });
  await until(() => pending.demo);
  const failedId = pending.demo.id;
  // 旧的成功状态不能把新上传提前标为已完成。
  await page.waitForTimeout(950);
  assert.equal(
    await page.getByText("正在上传文件", { exact: true }).count(),
    1,
  );
  stage("demo", "failed", {
    error: "向量接口暂不可用",
    finished_at: Date.now() / 1000,
  });
  await pending.demo.route.fulfill({
    status: 502,
    json: { detail: "向量接口暂不可用" },
  });
  pending.demo.release();
  delete pending.demo;
  await page.getByRole("button", { name: "重试", exact: true }).waitFor();
  assert.equal(await page.locator(".document").count(), 1);
  await page
    .locator(".knowledge-card")
    .screenshot({ path: resolve(out, "failed.png") });
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await until(() => pending.demo);
  assert.notEqual(pending.demo.id, failedId);
  await finish("demo", "doc-2");
  await page.getByText("已入库 2 / 20", { exact: true }).waitFor();

  await page.getByLabel("编辑的产品").selectOption("other");
  await page.getByText("还没有资料。入库完成后，文件会显示在这里。").waitFor();
  await fileInput.setInputFiles({
    name: "other.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("mock-other"),
  });
  await until(() => pending.other);
  stage("other", "embedding", { chunks: 4 });
  await page.getByText("正在建立检索索引", { exact: true }).waitFor();
  await page.getByLabel("编辑的产品").selectOption("demo");
  await finish("other", "other-1");
  await page.getByText("已入库 2 / 20", { exact: true }).waitFor();
  assert.equal(await page.getByText("other.pdf", { exact: true }).count(), 0);
  await page.getByLabel("编辑的产品").selectOption("other");
  await page.getByText("已入库 1 / 20", { exact: true }).waitFor();

  // 模拟页面重新打开时仍在处理，轮询完成后无需重新上传。
  stage("other", "embedding", { chunks: 4 });
  await page.getByLabel("编辑的产品").selectOption("demo");
  await page.getByLabel("编辑的产品").selectOption("other");
  await page.getByText("正在建立检索索引", { exact: true }).waitFor();
  const resumed = {
    id: "resumed-doc",
    name: "恢复上传.pdf",
    characters: 1200,
    chunks: 4,
    index_status: "ready",
  };
  docs.other.push(resumed);
  stage("other", "completed", {
    document: resumed,
    finished_at: Date.now() / 1000,
  });
  await page.getByText("已入库 2 / 20", { exact: true }).waitFor();

  // 模拟 POST 断开但服务端仍处理，不能显示错误或丢失最终成功。
  await fileInput.setInputFiles({
    name: "network.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("mock-network"),
  });
  await until(() => pending.other);
  stage("other", "embedding", { chunks: 4 });
  await pending.other.route.abort("failed");
  pending.other.release();
  const networkState = states.other;
  delete pending.other;
  await page.getByText("正在建立检索索引", { exact: true }).waitFor();
  const networkDoc = { ...resumed, id: "network-doc", name: "network.pdf" };
  docs.other.push(networkDoc);
  states.other = {
    ...networkState,
    stage: "completed",
    document: networkDoc,
    finished_at: Date.now() / 1000,
  };
  await page.getByText("已入库 3 / 20", { exact: true }).waitFor();
  await page.getByLabel("检索问题").fill("9700X 功耗是多少？");
  await page.getByRole("button", { name: "检索资料", exact: true }).click();
  await page
    .getByText("8 核 16 线程，默认 TDP 65 W，AM5 插槽。", { exact: true })
    .waitFor();
  await page.screenshot({ path: resolve(out, "desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  await page
    .locator(".knowledge-card")
    .screenshot({ path: resolve(out, "mobile.png") });
  await page.getByRole("button", { name: "新建产品", exact: true }).click();
  assert.equal(
    await page.getByLabel("产品名称", { exact: true }).inputValue(),
    "知答 AI 客服",
  );
  assert.match(
    await page.getByLabel("价格与套餐", { exact: true }).inputValue(),
    /未提供售价/,
  );
  assert.equal(await fileInput.isDisabled(), true);
  assert.deepEqual(errors, []);
  console.log(
    "Knowledge browser checks passed: phases, stale responses, success, retry, product switch, recovery, search, defaults and mobile. Mock APIs only.",
  );
} finally {
  await browser.close();
}
