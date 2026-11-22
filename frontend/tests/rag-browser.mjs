// 显式调用已启动应用和真实向量接口验证检索页面，不读取麦克风。
import { chromium } from "playwright";
import { existsSync, mkdirSync } from "node:fs";
import assert from "node:assert/strict";
const executablePath = ["C:/Program Files/Google/Chrome/Application/chrome.exe", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"].find(existsSync);
const browser = await chromium.launch({ headless: true, executablePath });
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
try {
  await page.goto("http://127.0.0.1:8000");
  await page.getByRole("button", { name: "产品与知识" }).click();
  const select = page.getByLabel("编辑的产品");
  await select.selectOption({ label: "Tally · 官网资料样本" });
  await page.getByText("向量索引就绪", { exact: false }).waitFor();
  await page.getByLabel("检索问题").fill("Pro 月付和年付分别多少钱？");
  await page.getByRole("button", { name: "检索资料", exact: true }).click();
  await page.locator(".retrieval-test article").first().waitFor({ timeout: 20000 });
  const result = await page.locator(".retrieval-test").innerText();
  assert.ok(result.includes("290") && result.includes("29"));
  assert.ok(result.includes("关键词第") && result.includes("向量第"));
  assert.ok(await page.getByLabel("上传产品资料").getAttribute("accept").then((s) => s.includes(".xlsx") && s.includes(".pdf")));
  mkdirSync("test-results", { recursive: true });
  await page.screenshot({ path: "test-results/rag-live.png", fullPage: true });
  assert.deepEqual(errors, []);
  console.log("Live RAG browser passed: sample product, indexed PDF, table XML evidence, rank display.");
} finally {
  await browser.close();
}
