// 用真实千问对话验收打断、回应语过滤、回复延迟及静音计时并保存完整证据。
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
import { installInstrumentation } from './instrumentation.mjs';

const base = process.env.ACCEPTANCE_URL || 'http://127.0.0.1:8001';
const runName = process.env.ACCEPTANCE_RUN || 'run';
const output = resolve('../output/voice-acceptance', runName);
mkdirSync(output, { recursive: true });
const browser = await chromium.launch({
  executablePath: ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(existsSync),
  headless: true,
  args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
});
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addInitScript(installInstrumentation);
const cases = [], sessions = [], errors = [];
/**
 * 读取验收服务已消耗的响应数，不触发模型生成。
 *
 * @returns 验收状态 JSON 的 Promise。
 */
const state = () => fetch(`${base}/acceptance/state`).then((response) => response.json());
/**
 * 读取真实验收服务的累计响应次数。
 *
 * @returns 已创建响应的数量。
 */
const count = async () => (await state()).responses;
/**
 * 等待指定时间，用于构造验收输入间隔。
 *
 * @param ms - 等待的毫秒数。
 * @returns 到期兑现的 Promise。
 */
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let page;

/** 等待真实浏览器事件，并在错误或额度用尽时保留现场。 */
async function waitEvent(type, since = 0, kind = 'server', timeout = 20000) {
  await page.waitForFunction(({ type, since, kind }) => window.__voiceBench.events.some((row) => row.at >= since && row.kind === kind && row.event?.type === type), { type, since, kind }, { timeout });
  return page.evaluate(({ type, since, kind }) => window.__voiceBench.events.find((row) => row.at >= since && row.kind === kind && row.event?.type === type), { type, since, kind });
}

/** 等待输出真正播放完，不把 response.done 误当作播放结束。 */
async function idle(since = 0) {
  const complete = await waitEvent('complete', since, 'playback', 25000);
  await page.waitForFunction(() => Object.keys(window.__voiceBench.playing).length === 0);
  return complete;
}

/** 注入一句离线合成中文并返回实际发送的有效 PCM 边界。 */
async function play(name, label) {
  return page.evaluate(({ name, label }) => window.__voiceBench.play(name, label), { name, label });
}

/** 验证一句输入后出现真实首音频播放，并保留失败样本。 */
async function normal(name, label) {
  const before = await count();
  if (before >= 10) throw new Error('Insufficient response budget for next complete test');
  const input = await play(name, label);
  const first = await waitEvent('started', input.at, 'playback');
  await idle(first.at);
  cases.push({ type: 'normal', label, input, first, responses_before: before, responses_after: await count() });
  console.log(JSON.stringify({ case: label, response_count: await count(), user_end_to_play_ms: first.at - input.captureEndAt }));
  return { input, first };
}

/** 在助手仍播放时注入无意义回应语或噪声，检查有无清空音频。 */
async function nonInterrupt(name, label) {
  const before = await count();
  const input = await play(name, label);
  await pause(1200);
  const snapshot = await page.evaluate(() => window.__voiceBench.snapshot());
  const clears = snapshot.events.filter((row) => row.at >= input.at && row.kind === 'server' && row.event.type === 'clear_audio');
  cases.push({ type: 'non_interruption', label, input, clears, responses_before: before, responses_after: await count() });
  console.log(JSON.stringify({ case: label, unwanted_clears: clears.length, response_count: await count() }));
}

/** 用真实语音插话验证清空播放、保留会话以及后续回复。 */
async function bargeIn(label, fixture = 'interrupt') {
  const before = await count();
  if (before >= 9) throw new Error('Insufficient response budget for barge-in pair');
  const prompt = await play('long_prompt', label + '_prompt');
  const first = await waitEvent('started', prompt.at, 'playback');
  await pause(180);
  const input = await play(fixture, label);
  const cleared = await waitEvent('cleared', input.at, 'playback');
  const resumed = await waitEvent('started', cleared.at + 0.1, 'playback');
  await idle(resumed.at);
  cases.push({ type: 'interruption', label, prompt, original_playback: first, input, cleared, resumed, responses_before: before, responses_after: await count() });
  console.log(JSON.stringify({ case: label, response_count: await count(), interrupt_stop_ms: cleared.at - input.captureStartAt, recovery_ms: resumed.at - input.captureEndAt }));
}

/** 保存当前页面的全部脱敏事件与应用报告。 */
async function saveSession(label) {
  if (!page) return;
  const snapshot = await page.evaluate(() => window.__voiceBench.snapshot()).catch(() => null);
  const ids = [...new Set((snapshot?.events || []).map((row) => row.event?.session_id).filter(Boolean))];
  const reports = [];
  for (const id of ids) reports.push(await fetch(`${base}/api/sessions/${id}/report`).then((response) => response.json()));
  sessions.push({ label, snapshot, reports });
  await page.screenshot({ path: resolve(output, `${label}.png`), fullPage: true }).catch(() => {});
  writeFileSync(resolve(output, 'browser-results.json'), JSON.stringify({ runName, cases, sessions, errors }, null, 2));
}

/** 创建只连接验收数据库的真实语音会话。 */
async function openSession() {
  page = await context.newPage();
  page.on('pageerror', (error) => errors.push({ type: 'page_error', message: error.message }));
  await page.goto(base);
  await page.getByRole('button', { name: '开始对话' }).click();
  await waitEvent('ready');
  return waitEvent('started', 0, 'playback');
}

try {
  if ((await count()) !== 0) throw new Error('This run already contains paid responses; refusing to reset the budget');
  let greeting = await openSession();
  await pause(250);
  await nonInterrupt('uh', '听开场时说嗯');
  await idle(greeting.at);
  await normal('identity', '普通完整问句');
  await normal('pause_600', '句中停顿600毫秒');
  await bargeIn('生成和播放期间插话');
  // 空闲提醒仍使用产品默认 8 秒；提醒播完后继续等待默认 20 秒。
  const snapshot = await page.evaluate(() => window.__voiceBench.snapshot());
  const lastComplete = snapshot.events.filter((row) => row.kind === 'playback' && row.event.type === 'complete').at(-1);
  await waitEvent('ended', lastComplete.at, 'server', 50000);
  const silenceEvents = await page.evaluate(() => window.__voiceBench.snapshot());
  cases.push({ type: 'silence', label: '默认8秒提醒与20秒结束', since: lastComplete.at, events: silenceEvents.events.filter((row) => row.at >= lastComplete.at) });
  console.log(JSON.stringify({ case: 'silence', response_count: await count() }));
  await saveSession('session-1');
  await page.evaluate(() => window.__voiceBench.shutdown());
  await page.close();
  page = null;

  if ((await count()) <= 6) {
    greeting = await openSession();
    await pause(250);
    await nonInterrupt('uhuh', '听开场时说嗯嗯');
    if (await page.evaluate(() => Object.keys(window.__voiceBench.playing).length > 0)) {
      await nonInterrupt('clicks', '播放期间合成键盘脉冲');
    }
    await idle(greeting.at);
    const mutedAt = await page.evaluate(() => performance.now());
    await page.getByRole('button', { name: '静音麦克风' }).click();
    await play('identity', '麦克风静音时输入语音');
    await pause(8000);
    const mutedEvents = await page.evaluate((at) => window.__voiceBench.events.filter((row) => row.at >= at), mutedAt);
    cases.push({ type: 'muted', label: '麦克风静音超过8秒不触发提醒', events: mutedEvents, responses_after: await count() });
    await page.getByRole('button', { name: '开启麦克风' }).click();
    if ((await count()) <= 7) await normal('pause_1200', '句中停顿1200毫秒');
    if ((await count()) <= 8) await bargeIn('第二次插话并继续', 'interrupt_two');
    while ((await count()) < 10) await normal('listening', '补充正常轮次');
    await page.getByRole('button', { name: '结束', exact: true }).click();
    await waitEvent('ended', 0, 'server');
    await saveSession('session-2');
  }
} catch (error) {
  errors.push({ type: 'harness', message: error.message });
  console.log(JSON.stringify({ error: error.message, response_count: await count() }));
} finally {
  if (page && !page.isClosed()) {
    await page.evaluate(() => window.__voiceBench.shutdown()).catch(() => {});
    await pause(700);
    const origin = await page.evaluate(() => performance.timeOrigin).catch(() => null);
    if (!sessions.some((row) => row.snapshot?.timeOrigin === origin)) await saveSession('final-state');
  }
  const provider = await state();
  writeFileSync(resolve(output, 'provider-results.json'), JSON.stringify(provider, null, 2));
  writeFileSync(resolve(output, 'browser-results.json'), JSON.stringify({ runName, browser_version: browser.version(), max_responses: 10, actual_responses: provider.responses, cases, sessions, errors }, null, 2));
  await browser.close();
  console.log(JSON.stringify({ complete: true, responses: provider.responses, cases: cases.length, errors: errors.length, output }));
}
