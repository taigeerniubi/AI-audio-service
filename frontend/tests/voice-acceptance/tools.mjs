// 测量真实工具调用问题从最后有效输入帧到浏览器首个有效回答音频的等待时间。
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { chromium } from 'playwright';
import { installInstrumentation } from './instrumentation.mjs';

const base = process.env.ACCEPTANCE_URL || 'http://127.0.0.1:8001';
const runName = process.env.ACCEPTANCE_RUN || '';
if (!runName.startsWith('tool-latency-')) throw new Error('Select a separate tool-latency- run');
const output = resolve('../output/voice-acceptance', runName);
const fixtures = resolve('../.cache/voice-acceptance/fixtures');
const fixtureName = process.env.ACCEPTANCE_FIXTURE || 'rag_pricing';
if (!['rag_pricing', 'rag_pricing_continuous'].includes(fixtureName)) throw new Error('Unknown fixture');
const trialCount = Number(process.env.ACCEPTANCE_TRIALS || 3);
if (![1, 2, 3].includes(trialCount)) throw new Error('Expected one to three trials');
mkdirSync(output, { recursive: true });
/** 读取脱敏观测状态，不触发模型调用。 */
const state = () => fetch(`${base}/acceptance/state`).then((response) => response.json());
const resumeAfter = Number(process.env.ACCEPTANCE_RESUME_AFTER || 0);
if (![0, 4].includes(resumeAfter) || (await state()).responses !== resumeAfter) throw new Error('Response count differs from the explicitly selected starting point');
const previous = resumeAfter ? JSON.parse(readFileSync(resolve(output, 'browser-results.json'), 'utf8')) : null;
let experiment = null;
if (runName.startsWith('tool-latency-coldwarm-')) {
  const probe = JSON.parse(readFileSync(resolve(output, 'cold-warm-probe.json'), 'utf8'));
  if (probe.metadata.application?.startup_prewarm) {
    throw new Error('Current app prewarms at startup; use an optimized run instead of labelling it cold');
  }
  if (resumeAfter || probe.events.length || probe.metadata.tokenizer_cache_at_install.currsize !== 0) {
    throw new Error('Cold/warm comparison requires a fresh, unmeasured backend process');
  }
  experiment = {
    type: 'local_process_first_query_then_two_warm_queries',
    server_pid: probe.metadata.pid,
    fixture_sha256: createHash('sha256').update(readFileSync(resolve(fixtures, `${fixtureName}.wav`))).digest('hex'),
    sessions: 'new realtime connection and identical product snapshot for each question',
    cloud_cold_start_controlled: false,
    greeting_precedes_each_question: true,
  };
}
if (runName.startsWith('tool-latency-optimized-')) {
  const probe = JSON.parse(readFileSync(resolve(output, 'cold-warm-probe.json'), 'utf8'));
  experiment = {
    type: 'prewarmed_process_first_connection_then_keepalive',
    server_pid: probe.metadata.pid,
    fixture_name: fixtureName,
    fixture_sha256: createHash('sha256').update(readFileSync(resolve(fixtures, `${fixtureName}.wav`))).digest('hex'),
    configuration: probe.metadata.application,
    sessions: 'new realtime connection and identical product snapshot for each question',
    cloud_cold_start_controlled: false,
    greeting_precedes_each_question: true,
  };
}
const probeResponse = await fetch(`${base}/audio-worklet.js`);
if (probeResponse.headers.get('X-Acceptance-Playback-Probe') !== '1') throw new Error('Playback probe is not served by the test backend');
const browser = await chromium.launch({
  executablePath: ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(existsSync),
  headless: true,
  args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
});
const context = await browser.newContext();
await context.addInitScript(installInstrumentation);

await context.route('**/acceptance/fixtures/manifest.json', (route) => route.fulfill({
  contentType: 'application/json', body: readFileSync(resolve(fixtures,
    fixtureName === 'rag_pricing' ? 'tools-manifest.json' : `${fixtureName}-manifest.json`), 'utf8').replace(/^\uFEFF/, ''),
}));
await context.route(`**/acceptance/fixtures/${fixtureName}.wav`, (route) => route.fulfill({
  contentType: 'audio/wav', body: readFileSync(resolve(fixtures, `${fixtureName}.wav`)),
}));
const cases = previous?.cases || [], sessions = previous?.sessions || [], errors = previous?.errors || [];
const oldErrorCount = errors.length;
let page;

/**
 * 等待目标事件，发生错误或连接关闭时立即结束等待。
 *
 * @param kind - server 或 playback。
 * @param type - 目标事件类型。
 * @param since - 本页面单调时钟上的最早时刻。
 * @param responseId - 可选的响应标识。
 * @returns 匹配的观测事件。
 */
async function wait(kind, type, since = 0, responseId = null) {
  const spec = { kind, type, since, responseId };
  await page.waitForFunction((s) => window.__voiceBench.events.some((row) => row.at >= s.since && (
    (row.kind === s.kind && row.event?.type === s.type && (!s.responseId || row.event.response_id === s.responseId)) ||
    (row.kind === 'server' && ['error', 'ended'].includes(row.event?.type)) || row.kind === 'socket_close'
  )), spec, { timeout: 45000 });
  const rows = await page.evaluate(() => window.__voiceBench.events);
  const match = rows.find((row) => row.at >= since && row.kind === kind && row.event?.type === type && (!responseId || row.event.response_id === responseId));
  if (!match) throw new Error(`Session ended before ${kind}:${type}`);
  return match;
}

/** 停止真实会话并保存全部时间线、转录和业务报告，包含失败样本。 */
async function saveSession() {
  if (!page || page.isClosed()) return;
  const alreadyEnded = await page.evaluate(() => window.__voiceBench.events.some((row) => row.kind === 'server' && row.event?.type === 'ended'));
  const since = await page.evaluate(() => performance.now());
  await page.evaluate(() => window.__voiceBench.shutdown()).catch(() => {});
  if (!alreadyEnded) await wait('server', 'ended', since).catch(() => {});
  const snapshot = await page.evaluate(() => window.__voiceBench.snapshot());
  const ids = [...new Set(snapshot.events.map((row) => row.event?.session_id).filter(Boolean))];
  const reports = await Promise.all(ids.map((id) => fetch(`${base}/api/sessions/${id}/report`).then((response) => response.json())));
  sessions.push({ snapshot, reports });
  writeFileSync(resolve(output, 'browser-results.json'), JSON.stringify({ runName, experiment, cases, sessions, errors }, null, 2));
  await page.close();
  page = null;
}

try {
  for (let trial = sessions.length + 1; trial <= trialCount; trial++) {
    // 一次开场、一次工具决策、一次依据工具结果回答，三轮共预计九次响应。
    const budget = await state();
    if (budget.responses + 3 > budget.limit) throw new Error('Insufficient remaining response budget');
    page = await context.newPage();
    page.on('pageerror', (error) => errors.push({ trial, message: error.message }));
    await page.goto(base);
    await page.getByRole('button', { name: '开始对话' }).click();
    await wait('server', 'ready');
    const greeting = await wait('playback', 'started');
    await wait('playback', 'audible', greeting.at, greeting.event.response_id);
    await wait('playback', 'complete', greeting.at, greeting.event.response_id);
    const input = await page.evaluate((name) => window.__voiceBench.play(name, '查询 Pro 月付价格'), fixtureName);
    const tool = await wait('server', 'tool_result', input.at);
    const answer = await wait('server', 'response_started', tool.at);
    const groundedFirst = await wait('playback', 'audible', answer.at, answer.event.response_id);
    await wait('playback', 'complete', groundedFirst.at, groundedFirst.event.response_id);
    const snapshot = await page.evaluate(() => window.__voiceBench.snapshot());
    const first = snapshot.events.find((row) => row.at >= input.captureEndAt && row.kind === 'playback' && row.event?.type === 'audible');
    const packet = snapshot.events.find((row) => row.kind === 'server' && row.event?.type === 'audio' && row.event.response_id === groundedFirst.event.response_id);
    const transcript = snapshot.events.filter((row) => row.kind === 'server' && row.event?.type === 'transcript' && row.event.message?.response_id === groundedFirst.event.response_id).at(-1)?.event.message?.text;
    const result = {
      trial, input, tool, first, groundedFirst, transcript,
      startup_condition: experiment?.type === 'prewarmed_process_first_connection_then_keepalive'
        ? (trial === 1 ? 'prewarmed_first_http_request' : 'prewarmed_keepalive_candidate')
        : experiment ? (trial === 1 ? 'local_first_query' : 'local_warm_query') : null,
      end_to_first_audible_ms: first.at - input.captureEndAt,
      end_to_grounded_audible_ms: groundedFirst.at - input.captureEndAt,
      end_to_first_audio_packet_ms: packet.at - input.captureEndAt,
      end_to_tool_result_ms: tool.at - input.captureEndAt,
      tool_result_to_grounded_audible_ms: groundedFirst.at - tool.at,
      early_speech_before_tool: first.at < tool.at,
      tool_success: tool.event.name === 'search_product_knowledge' && !tool.event.cancelled && tool.event.result?.matches?.length > 0,
    };
    cases.push(result);
    console.log(JSON.stringify({ trial, total_ms: result.end_to_grounded_audible_ms, early_speech: result.early_speech_before_tool, tool_success: result.tool_success, responses: (await state()).responses }));
    await saveSession();
    if (!result.tool_success) throw new Error('No successful retrieval; sample cannot establish tool latency');
  }
} catch (error) {
  errors.push({ type: 'harness', message: error.message });
  console.log(JSON.stringify({ error: error.message }));
} finally {
  await saveSession().catch((error) => errors.push({ type: 'save', message: error.message }));
  const provider = await state();
  writeFileSync(resolve(output, 'provider-results.json'), JSON.stringify(provider, null, 2));
  writeFileSync(resolve(output, 'browser-results.json'), JSON.stringify({ runName, experiment, browser_version: browser.version(), actual_responses: provider.responses, cases, sessions, errors }, null, 2));
  await browser.close();
  console.log(JSON.stringify({ complete: true, cases: cases.length, responses: provider.responses, errors: errors.length }));
  if (errors.length > oldErrorCount || sessions.length !== trialCount) process.exitCode = 1;
}
