#!/usr/bin/env node
// Host-private Playwright bridge for the browser run-target example.
// It deliberately emits no endpoint, module path, or application URL diagnostics.
import { createHash, randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import { lstat, link, open, readFile, unlink, writeFile, chmod } from 'node:fs/promises';
import { dirname, isAbsolute, join } from 'node:path';
import { pathToFileURL } from 'node:url';

const LIMIT = 1024 * 1024;
const TOKEN = /^[A-Za-z0-9_-]{16,128}$/;
const DIGEST = /^[0-9a-f]{64}$/;
const ENGINES = new Set(['chromium', 'firefox', 'webkit']);

function fail() {
  process.stderr.write('browser driver operation failed\n');
  process.exitCode = 2;
  throw new Error('browser driver operation failed');
}

async function input() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > LIMIT) fail();
    chunks.push(chunk);
  }
  try {
    const parsed = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) fail();
    return parsed;
  } catch {
    fail();
  }
}

function text(value, maximum = 4096) {
  return typeof value === 'string' && value.length > 0 && value.length <= maximum && !value.includes('\0');
}

function sha(value) {
  return createHash('sha256').update(value).digest('hex');
}

function output(value) {
  return new Promise((resolve) => process.stdout.write(JSON.stringify(value), resolve));
}

async function playwright(modulePath) {
  if (!text(modulePath) || !isAbsolute(modulePath)) fail();
  try {
    const module = await import(pathToFileURL(modulePath).href);
    const api = module.default ?? module;
    if (!api || !api.chromium || !api.firefox || !api.webkit) fail();
    return api;
  } catch {
    fail();
  }
}

async function privateDirectory(path) {
  if (!text(path) || !isAbsolute(path)) fail();
  try {
    const info = await lstat(path);
    if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o077) !== 0) fail();
  } catch {
    fail();
  }
}

async function writeExclusivePrivate(path, value) {
  await privateDirectory(dirname(path));
  const temporary = `${path}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`;
  let handle;
  try {
    handle = await open(temporary, 'wx', 0o600);
    await handle.writeFile(JSON.stringify(value));
    await handle.sync();
    await handle.close();
    handle = undefined;
    await link(temporary, path);
    await unlink(temporary);
  } catch {
    if (handle) await handle.close().catch(() => undefined);
    await unlink(temporary).catch(() => undefined);
    fail();
  }
}

function validRecord(record) {
  return record && typeof record === 'object'
    && TOKEN.test(record.token) && ENGINES.has(record.engine)
    && typeof record.headless === 'boolean' && text(record.page_url)
    && (record.page_url.startsWith('https://') || record.page_url.startsWith('http://'))
    && DIGEST.test(record.record_fingerprint);
}

function validReceipt(receipt) {
  return receipt && typeof receipt === 'object'
    && receipt.version === 1 && text(receipt.run_id, 256)
    && TOKEN.test(receipt.instance_token) && ENGINES.has(receipt.engine)
    && DIGEST.test(receipt.record_fingerprint)
    && text(receipt.endpoint) && receipt.endpoint.startsWith('ws')
    && text(receipt.page_url) && text(receipt.page_marker, 512);
}

async function executableProbe(browserType) {
  const executable = browserType.executablePath();
  if (!text(executable)) return { ready: false, version: '', fingerprint: sha('missing') };
  try {
    const info = await lstat(executable);
    if (!info.isFile()) return { ready: false, version: '', fingerprint: sha('missing') };
    const child = await new Promise((resolve) => {
      const process = spawn(executable, ['--version'], {
        detached: true,
        stdio: ['ignore', 'pipe', 'ignore'],
      });
      const chunks = [];
      let size = 0;
      let force;
      const stop = () => {
        if (process.exitCode !== null) return;
        try { process.kill(-process.pid, 'SIGTERM'); } catch {}
        force = setTimeout(() => {
          try { process.kill(-process.pid, 'SIGKILL'); } catch {}
        }, 1000);
      };
      const timer = setTimeout(stop, 5000);
      process.stdout.on('data', (chunk) => {
        size += chunk.length;
        if (size <= 4096) chunks.push(chunk);
        else stop();
      });
      process.on('close', (code) => {
        clearTimeout(timer);
        clearTimeout(force);
        resolve({ code, output: Buffer.concat(chunks).toString('utf8').trim() });
      });
      process.on('error', () => {
        clearTimeout(timer);
        clearTimeout(force);
        resolve({ code: 1, output: '' });
      });
    });
    if (child.code !== 0 || !text(child.output, 256)) {
      return { ready: false, version: '', fingerprint: sha(`${executable}:${info.size}:${info.mtimeMs}`) };
    }
    return {
      ready: true,
      version: child.output,
      fingerprint: sha(`${executable}:${info.dev}:${info.ino}:${info.size}:${info.mtimeMs}:${child.output}`),
    };
  } catch {
    return { ready: false, version: '', fingerprint: sha('missing') };
  }
}

async function selectedPage(browser, receipt) {
  const pages = browser.contexts().flatMap((context) => context.pages())
    .filter((page) => page.url() === receipt.page_url);
  if (pages.length !== 1) fail();
  const marker = await pages[0].evaluate(() => window.name);
  if (marker !== receipt.page_marker) fail();
  return pages[0];
}

async function closeServer(server) {
  const child = server.process();
  let closed = false;
  let timeout;
  await Promise.race([
    server.close().then(() => { closed = true; }).catch(() => undefined),
    new Promise((resolve) => { timeout = setTimeout(resolve, 1500); }),
  ]);
  clearTimeout(timeout);
  if (closed) return true;
  if (!closed && child && child.exitCode === null) {
    try {
      child.kill('SIGKILL');
    } catch {
      return false;
    }
    let reaped = false;
    let reapTimeout;
    await Promise.race([
      new Promise((resolve) => {
        child.once('close', () => {
          reaped = true;
          resolve(undefined);
        });
      }),
      new Promise((resolve) => { reapTimeout = setTimeout(resolve, 1500); }),
    ]);
    clearTimeout(reapTimeout);
    return reaped;
  }
  return false;
}

async function probe(payload) {
  const api = await playwright(payload.playwright_module);
  const engines = {};
  for (const name of ENGINES) engines[name] = await executableProbe(api[name]);
  await output({ engines });
}

async function launch(payload) {
  let record;
  let receiptPath;
  let server;
  let cleanupServer;
  let stopping = false;
  let releaseStop;
  const stopped = new Promise((resolve) => { releaseStop = resolve; });
  const requestStop = () => {
    stopping = true;
    releaseStop(undefined);
    if (server && !cleanupServer) cleanupServer = closeServer(server);
  };
  process.once('SIGINT', requestStop);
  process.once('SIGTERM', requestStop);
  try {
    record = payload.record;
    if (!validRecord(record) || !text(payload.run_id, 256) || !text(payload.receipt_path)
        || !isAbsolute(payload.receipt_path) || !text(payload.page_marker, 512)) fail();
    const expected = `${dirname(payload.receipt_path)}/${sha(payload.run_id)}.json`;
    if (payload.receipt_path !== expected) fail();
    receiptPath = payload.receipt_path;
    const api = await playwright(payload.playwright_module);
    const type = api[record.engine];
    server = await type.launchServer({ headless: record.headless });
    if (stopping) return;
    const browser = await type.connect(server.wsEndpoint());
    const page = await browser.newPage();
    await page.goto(record.page_url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    if (stopping) return;
    const marker = await page.evaluate((value) => {
      window.name = value;
      return window.name;
    }, payload.page_marker);
    if (marker !== payload.page_marker || page.url() !== record.page_url) fail();
    await writeExclusivePrivate(receiptPath, {
      version: 1,
      run_id: payload.run_id,
      instance_token: record.token,
      record_fingerprint: record.record_fingerprint,
      engine: record.engine,
      endpoint: server.wsEndpoint(),
      page_url: record.page_url,
      page_marker: payload.page_marker,
    });
    await output({ ready: true });
    await stopped;
  } finally {
    const cleanupKnown = server
      ? await (cleanupServer ?? closeServer(server))
      : true;
    const receipt = receiptPath
      ? await readFile(receiptPath, 'utf8').then(JSON.parse).catch(() => null)
      : null;
    if (
      cleanupKnown
      && receipt
      && record
      && receipt.endpoint === server?.wsEndpoint()
      && receipt.run_id === payload.run_id
      && receipt.page_marker === payload.page_marker
      && receipt.instance_token === record.token
      && receipt.record_fingerprint === record.record_fingerprint
    ) {
      await unlink(receiptPath).catch(() => undefined);
    }
    await output({ cleanup: cleanupKnown });
    launchCleanupReported = true;
  }
}

async function observe(payload) {
  if (!validReceipt(payload.receipt)) fail();
  const api = await playwright(payload.playwright_module);
  const browser = await api[payload.receipt.engine].connect(payload.receipt.endpoint);
  await selectedPage(browser, payload.receipt);
  await output({ page_url: payload.receipt.page_url, page_marker: payload.receipt.page_marker });
}

async function capture(payload) {
  if (!validReceipt(payload.receipt) || !text(payload.directory) || !isAbsolute(payload.directory)
      || !Array.isArray(payload.kinds) || !payload.kinds.every((kind) => kind === 'image' || kind === 'layout')
      || typeof payload.capture_layout !== 'boolean') fail();
  const api = await playwright(payload.playwright_module);
  const browser = await api[payload.receipt.engine].connect(payload.receipt.endpoint);
  const page = await selectedPage(browser, payload.receipt);
  if (payload.kinds.includes('image')) await page.screenshot({ path: join(payload.directory, 'screen.png') });
  if (payload.kinds.includes('layout')) {
    if (!payload.capture_layout) fail();
    const html = await page.content();
    if (Buffer.byteLength(html, 'utf8') > LIMIT) fail();
    await writeFile(join(payload.directory, 'layout.html'), html, { mode: 0o600, flag: 'w' });
    await chmod(join(payload.directory, 'layout.html'), 0o600);
  }
  await output({ page_url: payload.receipt.page_url, page_marker: payload.receipt.page_marker });
}


async function logs(payload) {
  if (!validReceipt(payload.receipt)) fail();
  const api = await playwright(payload.playwright_module);
  const browser = await api[payload.receipt.engine].connect(payload.receipt.endpoint);
  const page = await selectedPage(browser, payload.receipt);
  page.on('console', (message) => {
    process.stdout.write(JSON.stringify({
      type: 'console', level: message.type(), text: message.text().slice(0, 4096),
    }) + '\n');
  });
  page.on('pageerror', (error) => {
    process.stdout.write(JSON.stringify({ type: 'pageerror', text: String(error.message).slice(0, 4096) }) + '\n');
  });
  await new Promise((resolve) => {
    const stop = () => resolve(undefined);
    process.once('SIGINT', stop);
    process.once('SIGTERM', stop);
  });
}
let launchCleanupReported = false;

const action = process.argv[2];
let completed = false;
try {
  const payload = await input();
  if (action === 'probe') await probe(payload);
  else if (action === 'launch') await launch(payload);
  else if (action === 'observe') await observe(payload);
  else if (action === 'capture') await capture(payload);
  else if (action === 'logs') await logs(payload);
  else fail();
  completed = true;
} catch {
  if (action === 'launch' && !launchCleanupReported) {
    await output({ cleanup: true });
    launchCleanupReported = true;
  }
  if (!process.exitCode) {
    process.stderr.write('browser driver operation failed\n');
    process.exitCode = 2;
  }
}
if (completed) process.exit(0);
