// 使用真实千问与浏览器复测提醒话术、静音结束及恢复说话的边界。
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
import { installInstrumentation } from './instrumentation.mjs';

const base = process.env.ACCEPTANCE_URL || 'http://127.0.0.1:8001';
const runName = process.env.ACCEPTANCE_RUN;
if (!runName || runName === 'run') throw new Error('Set a new, explicitly authorized ACCEPTANCE_RUN');
const output = resolve('../output/voice-acceptance', runName);
mkdirSync(output, { recursive: true });
/**
 * 读取验收服务已消耗的响应数，不触发模型生成。
 *
 * @returns 验收状态 JSON 的 Promise。
 */
const state = () => fetch(`${base}/acceptance/state`).then((r) => r.json());
const resumeAfter = Number(process.env.ACCEPTANCE_RESUME_AFTER || 0);
if (![0, 6].includes(resumeAfter) || (await state()).responses !== resumeAfter) throw new Error('Response count differs from explicitly selected starting point; refusing replay');
const previous = resumeAfter ? JSON.parse(readFileSync(resolve(output, 'browser-results.json'), 'utf8')) : null;
const browser = await chromium.launch({
  executablePath: ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(existsSync),
  headless: true,
  args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
});
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addInitScript(installInstrumentation);
const cases = previous?.cases || [], sessions = previous?.sessions || [], errors = previous?.errors || [];
const oldErrorCount = errors.length;
/**
 * 等待指定时间，用于构造验收输入间隔。
 *
 * @param ms - 等待的毫秒数。
 * @returns 到期兑现的 Promise。
 */
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let page, label;

/** 按响应 ID 和本地时间等待事件，遇到应用错误立即保留失败。 */
async function wait(kind, type, since = 0, responseId = null, timeout = 25000) {
  const spec = { kind, type, since, responseId };
  await page.waitForFunction((s) => window.__voiceBench.events.some((r) =>
    r.at >= s.since && ((r.kind === 'server' && r.event?.type === 'error') ||
    (r.kind === s.kind && (s.kind === 'trace' ? r.event?.event?.type : r.event?.type) === s.type &&
    (!s.responseId || r.event?.response_id === s.responseId)))) ||
    (s.kind === 'trace' && window.__voiceBench.events.some((r) => r.at >= s.since && r.kind === 'server' && r.event?.type === 'trace' && r.event.event?.type === s.type)),
  spec, { timeout });
  const rows = await page.evaluate(() => window.__voiceBench.events);
  const error = rows.find((r) => r.at >= since && r.kind === 'server' && r.event?.type === 'error');
  if (error) throw new Error(error.event.message);
  return rows.find((r) => r.at >= since &&
    (kind === 'trace' ? r.kind === 'server' && r.event?.type === 'trace' && r.event.event?.type === type : r.kind === kind && r.event?.type === type) &&
    (!responseId || r.event?.response_id === responseId));
}

/** 等到指定响应真正播完，避免把生成结束当作播放结束。 */
async function complete(first) {
  return wait('playback', 'complete', first.at, first.event.response_id);
}

/** 注入离线合成语音，返回实际采集到的非静音帧边界。 */
async function play(name, inputLabel) {
  return page.evaluate(({ name, inputLabel }) => window.__voiceBench.play(name, inputLabel), { name, inputLabel });
}

/** 创建隔离的真实会话，并等待开场播放完。 */
async function open(sessionLabel) {
  label = sessionLabel;
  page = await context.newPage();
  page.on('pageerror', (e) => errors.push({ type: 'page_error', label, message: e.message }));
  await page.goto(base);
  await page.getByRole('button', { name: '开始对话' }).click();
  await wait('server', 'ready');
  return complete(await wait('playback', 'started'));
}

/** 保存全部脱敏事件、会话报告和页面截图，不保存模型原始音频。 */
async function save() {
  const snapshot = await page.evaluate(() => window.__voiceBench.snapshot());
  const ids = [...new Set(snapshot.events.map((r) => r.event?.session_id).filter(Boolean))];
  const reports = await Promise.all(ids.map((id) => fetch(`${base}/api/sessions/${id}/report`).then((r) => r.json())));
  sessions.push({ label, snapshot, reports });
  await page.screenshot({ path: resolve(output, `${label}.png`), fullPage: true });
  writeFileSync(resolve(output, 'browser-results.json'), JSON.stringify({ runName, cases, sessions, errors }, null, 2));
}

/** 关闭本次会话并保存最终状态，后续场景使用新浏览器页面。 */
async function close(alreadyEnded = false) {
  if (!alreadyEnded) {
    const since = await page.evaluate(() => performance.now());
    await page.evaluate(() => window.__voiceBench.shutdown());
    await wait('server', 'ended', since);
  } else {
    await page.evaluate(() => window.__voiceBench.shutdown());
  }
  await save();
  await page.close();
  page = null;
}

/** 记录每一场景，任何未覆盖或失败均留在证据中。 */
async function record(result) {
  cases.push({ label, ...result });
  console.log(JSON.stringify({ case: label, type: result.type, responses: (await state()).responses }));
}

try {
  if (resumeAfter === 6) {
    // 断线样本仍保留；只使用本轮余下四个响应，避免重跑已完成场景。
    const greetingEnd = await open('speech-during-reminder-retry');
    const trigger = await wait('trace', 'idle_reminder', greetingEnd.at);
    const reminder = await wait('playback', 'started', trigger.at);
    await pause(120);
    const input = await play('interrupt_two', '提醒播放时恢复说话（断线后独立重试）');
    const cleared = await wait('playback', 'cleared', input.at);
    const resumed = await wait('playback', 'started', cleared.at + 0.1);
    const replyEnd = await complete(resumed);
    await record({ type: 'reminder_interruption', greetingEnd, trigger, reminder, input, cleared, resumed });
    // 提前五秒恢复发声，保留一个完整用户回复的额度；不宣称覆盖 ACK 竞态。
    await page.evaluate(async (deadline) => {
      await new Promise((resolve) => setTimeout(resolve, Math.max(0, deadline - performance.now())));
    }, replyEnd.at + 5000);
    const recovery = await play('identity', '八秒提醒之前恢复说话');
    const stopped = await wait('trace', 'input_audio_buffer.speech_stopped', recovery.captureEndAt);
    const next = await wait('playback', 'started', stopped.at);
    await complete(next);
    await record({ type: 'boundary_speech', greetingEnd: replyEnd, input: recovery, stopped, resumed: next });
    await close();
  } else {
  // 3 次响应：开场、旧问题回复、固定提醒；继续静音至自然结束。
  await open('normal-reminder');
  const input = await play('listening', '复现上一句能听清的上下文');
  const first = await wait('playback', 'started', input.at);
  const previousEnd = await complete(first);
  const queued = await wait('trace', 'idle_reminder_queued', previousEnd.at);
  const triggered = await wait('trace', 'idle_reminder', queued.at);
  const reminderFirst = await wait('playback', 'started', triggered.at);
  const reminderEnd = await complete(reminderFirst);
  const result = await wait('trace', 'idle_reminder_result', triggered.at);
  const ended = await wait('server', 'ended', reminderEnd.at, null, 35000);
  await record({ type: 'normal_reminder', input, first, previousEnd, queued, triggered, reminderFirst, reminderEnd, result, ended });
  await close(true);

  // 4 次响应：开场、普通回复、提醒、插话回复；验证同一会话继续。
  await open('speech-during-reminder');
  const question = await play('identity', '提醒前身份问题');
  const answer = await wait('playback', 'started', question.at);
  const answerEnd = await complete(answer);
  const trigger = await wait('trace', 'idle_reminder', answerEnd.at);
  const reminder = await wait('playback', 'started', trigger.at);
  await pause(120);
  const interruption = await play('interrupt_two', '提醒播放时恢复说话');
  const cleared = await wait('playback', 'cleared', interruption.at);
  const resumed = await wait('playback', 'started', cleared.at + 0.1);
  await complete(resumed);
  await record({ type: 'reminder_interruption', question, answer, answerEnd, trigger, reminder, input: interruption, cleared, resumed });
  await close();

  // 最多 3 次响应：开场、可能恰好触发的提醒、用户问题回复。
  const greetingEnd = await open('speech-at-idle-boundary');
  await page.evaluate(async (deadline) => {
    await new Promise((resolve) => setTimeout(resolve, Math.max(0, deadline - performance.now())));
  }, greetingEnd.at + 7700);
  const boundaryInput = await play('interrupt_two', '接近八秒阈值时恢复说话');
  // 有效发声停止后产生的回答才是用户回复，不能把竞争中的提醒当回复。
  const stopped = await wait('trace', 'input_audio_buffer.speech_stopped', boundaryInput.captureEndAt);
  const boundaryReply = await wait('playback', 'started', stopped.at);
  await complete(boundaryReply);
  await record({ type: 'boundary_speech', greetingEnd, input: boundaryInput, stopped, resumed: boundaryReply });
  await close();
  }
} catch (e) {
  errors.push({ type: 'harness', label, message: e.message });
  console.log(JSON.stringify({ error: e.message, label, responses: (await state()).responses }));
} finally {
  if (page && !page.isClosed()) {
    await page.evaluate(() => window.__voiceBench.shutdown()).catch(() => {});
    await pause(1200);
    await save().catch((e) => errors.push({ type: 'save', message: e.message }));
  }
  const provider = await state();
  writeFileSync(resolve(output, 'provider-results.json'), JSON.stringify(provider, null, 2));
  writeFileSync(resolve(output, 'browser-results.json'), JSON.stringify({ runName, browser_version: browser.version(), max_responses: provider.limit, actual_responses: provider.responses, cases, sessions, errors }, null, 2));
  await browser.close();
  console.log(JSON.stringify({ complete: true, responses: provider.responses, cases: cases.length, errors: errors.length, output }));
  if (errors.length > oldErrorCount) process.exitCode = 1;
}
