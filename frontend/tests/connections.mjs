// 在真实浏览器中注入断线与心跳丢失，验证提示、音频清理和手动重启。
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { resolve, extname, sep } from 'node:path';
import { chromium } from 'playwright';

const root = resolve('dist');
const output = resolve('../output/connection-audit-20261111');
mkdirSync(output, { recursive: true });
const server = createServer(async (request, response) => {
  try {
    const path = resolve(root, '.' + new URL(request.url, 'http://localhost').pathname.replace(/\/$/, '/index.html'));
    if (!path.startsWith(root + sep)) throw new Error('Invalid path');
    const content = await readFile(path);
    response.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' })[extname(path)] || 'application/octet-stream');
    response.end(content);
  } catch {
    response.writeHead(404).end();
  }
});
await new Promise((done) => server.listen(0, '127.0.0.1', done));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({
  executablePath: ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(existsSync),
  headless: true,
  args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio'],
});
const results = [];

/** 单独打开一个故障场景；API 和模型传输全在本地模拟。 */
async function scenario(name, check) {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.addInitScript(() => {
    window.__sockets = []; window.__appAudio = []; window.__micTracks = []; window.__audioPackets = 0;
    window.__respondToPing = true;
    const NativeContext = window.AudioContext;
    window.AudioContext = class extends NativeContext {
      /**
       * 记录应用创建的音频上下文，便于检查断线后是否关闭。
       *
       * @param args - 转发给原生 AudioContext 的构造参数。
       */
      constructor(...args) { super(...args); window.__appAudio.push(this); }
    };
    const NativeWorklet = window.AudioWorkletNode;
    window.AudioWorkletNode = class extends NativeWorklet {
      /**
       * 记录应用投递的音频包数量，检查清理后是否仍有音频。
       *
       * @param args - 转发给原生 AudioWorkletNode 的构造参数。
       */
      constructor(...args) {
        super(...args);
        const original = this.port.postMessage.bind(this.port);
        this.port.postMessage = (event, ...rest) => {
          if (event.type === 'audio') window.__audioPackets++;
          return original(event, ...rest);
        };
      }
    };
    navigator.mediaDevices.getUserMedia = async () => {
      const sourceContext = new NativeContext();
      window.__sourceContext = sourceContext;
      await sourceContext.resume();
      const output = sourceContext.createMediaStreamDestination();
      const silence = sourceContext.createConstantSource();
      silence.offset.value = 0; silence.connect(output); silence.start();
      window.__micTracks.push(...output.stream.getTracks());
      return output.stream;
    };
    window.WebSocket = class {
      static OPEN = 1;
      readyState = 1;
      bufferedAmount = 0;
      frames = 0;
      events = [];
      /**
       * 建立假连接并异步报告 ready，不访问网络。
       */
      constructor() {
        window.__sockets.push(this);
        setTimeout(() => { this.onopen?.({}); this.push({ type: 'ready', session_id: 'test' }); }, 20);
      }
      /**
       * 向页面派发一条假服务端事件。
       *
       * @param event - 会被 JSON 编码的浏览器协议事件。
       */
      push(event) { this.onmessage?.({ data: JSON.stringify(event) }); }
      /**
       * 记录控制事件、统计音频帧并模拟心跳和结束确认。
       *
       * @param data - 页面发送的 PCM 帧或 JSON 字符串。
       */
      send(data) {
        if (typeof data !== 'string') { this.frames++; return; }
        const event = JSON.parse(data); this.events.push(event);
        if (event.type === 'ping' && window.__respondToPing) this.push({ type: 'pong', sequence: event.sequence });
        if (event.type === 'stop') {
          /**
           * 在连接仍打开时标记服务端结束并发送最终确认。
           */
          const finish = () => { if (this.readyState === 1) { window.__serverEnded = true; this.push({ type: 'ended', reason: 'user_ended' }); } };
          if (window.__delayStopMs) setTimeout(finish, window.__delayStopMs);
          else finish();
        }
      }
      /**
       * 模拟异步正常关闭，重复调用不再次派发事件。
       *
       * @param code - WebSocket 正常关闭码，默认 1000。
       */
      close(code = 1000) { if (this.readyState !== 3) { this.readyState = 3; queueMicrotask(() => this.onclose?.({ code, wasClean: true })); } }
      /**
       * 注入 1006 异常关闭以验证用户提示和资源清理。
       */
      fail() { this.readyState = 3; this.onclose?.({ code: 1006, wasClean: false }); }
    };
  });
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const product = { id: 'demo', name: '本地断线测试', version: 1, features: '', pricing: '', audience: '' };
    let body;
    if (path === '/api/health') body = { configured: true, model: 'mock-no-cloud', missing: [] };
    else if (path === '/api/products') body = [product];
    else if (path === '/api/sessions' && route.request().method() === 'POST') body = { id: 'test', websocket_path: '/ws/sessions/test' };
    else if (path.endsWith('/report')) body = { session_id: 'test', transcripts: [], metrics: [], actions: [] };
    else body = [];
    await route.fulfill({ json: body });
  });
  try {
    await page.goto(base);
    assert.equal(await page.title(), 'AI audio service');
    await page.locator('.brand-name').filter({ hasText: 'AI audio service' }).waitFor();
    await page.getByRole('button', { name: '开始对话' }).click();
    await page.getByRole('heading', { name: '正在聆听', exact: true }).waitFor();
    await check(page);
    assert.deepEqual(errors, []);
    results.push({ name, passed: true });
  } catch (e) {
    results.push({ name, passed: false, error: e.message, page_errors: errors });
  } finally {
    await page.screenshot({ path: resolve(output, `${name}.png`) });
    await page.evaluate(() => window.__sourceContext?.close()).catch(() => {});
    await page.close();
  }
}

/** 检查麦克风、输出上下文和后续音频均已停用。 */
async function assertClean(page) {
  await page.waitForFunction(() => window.__micTracks.every((track) => track.readyState === 'ended') && window.__appAudio.every((context) => context.state === 'closed'));
  const count = await page.evaluate(() => window.__audioPackets);
  await page.evaluate(() => window.__sockets[0].push({ type: 'audio', epoch: 0, response_id: 'late', audio: 'AAA=' }));
  assert.equal(await page.evaluate(() => window.__audioPackets), count);
  assert.equal(await page.evaluate(() => window.__sockets.length), 1);
}

try {
  await scenario('abnormal-close', async (page) => {
    await page.evaluate(() => window.__sockets[0].fail());
    await page.getByText('连接已中断，请重新开始。已收到的文字保留在本页。', { exact: true }).waitFor({ timeout: 3000 });
    await assertClean(page);
    await page.getByRole('button', { name: '开始对话' }).click();
    await page.getByRole('heading', { name: '正在聆听', exact: true }).waitFor();
    await page.evaluate(() => window.__sockets[0].fail());
    await page.getByRole('heading', { name: '正在聆听', exact: true }).waitFor();
    assert.equal(await page.evaluate(() => window.__sockets.length), 2);
  });
  if (!process.env.CONNECTION_TEST_ABRUPT_ONLY) {
    await scenario('heartbeat-timeout', async (page) => {
      await page.evaluate(() => { window.__respondToPing = false; });
      await page.getByText('连接长时间无响应，已停止收音和播放，请重新开始。', { exact: true }).waitFor({ timeout: 16000 });
      await assertClean(page);
      assert.equal(await page.evaluate(() => window.__sockets[0].readyState), 3);
    });
    await scenario('healthy-heartbeat-and-stop', async (page) => {
      await page.waitForTimeout(13000);
      await page.getByRole('heading', { name: '正在聆听', exact: true }).waitFor();
      assert((await page.evaluate(() => window.__sockets[0].events.filter((e) => e.type === 'ping').length)) >= 5);
      await page.getByRole('button', { name: '结束', exact: true }).click();
      await page.getByRole('heading', { name: '对话已结束', exact: true }).waitFor();
      await assertClean(page);
      assert.equal(await page.getByText('连接已中断，请重新开始。已收到的文字保留在本页。', { exact: true }).count(), 0);
    });
    await scenario('provider-error-kept', async (page) => {
      await page.evaluate(() => { const socket = window.__sockets[0]; socket.push({ type: 'error', message: '模型连接中断，请重新开始。' }); socket.push({ type: 'ended', reason: 'model_disconnected' }); socket.close(); });
      await page.getByText('模型连接中断，请重新开始。', { exact: true }).waitFor();
      await assertClean(page);
    });
    await scenario('stop-waits-for-final-report', async (page) => {
      await page.evaluate(() => { window.__delayStopMs = 400; });
      await page.getByRole('button', { name: '结束', exact: true }).click();
      await page.waitForFunction(() => window.__serverEnded === true, null, { timeout: 2500 });
      await assertClean(page);
    });
    await scenario('mobile-brand-layout', async (page) => {
      await page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.equal(await page.getByText('声场', { exact: true }).count(), 0);
    });
  }
} finally {
  await browser.close();
  await new Promise((done) => server.close(done));
  writeFileSync(resolve(output, process.env.CONNECTION_TEST_ABRUPT_ONLY ? 'browser-red.json' : 'browser-tests.json'), JSON.stringify({ mock_only: true, results }, null, 2));
}
console.log(JSON.stringify(results, null, 2));
if (results.some((result) => !result.passed)) process.exitCode = 1;
