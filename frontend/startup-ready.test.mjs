import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { announceWhenReady } from './startup-ready.mjs';

const originalFetch = globalThis.fetch;
const originalLog = console.log;
const originalError = console.error;
afterEach(() => {
  globalThis.fetch = originalFetch;
  console.log = originalLog;
  console.error = originalError;
});

const settings = { appUrl: 'http://127.0.0.1:4173', ollamaUrl: 'http://ollama:11434', model: 'qwen3:1.7b' };
const json = (data, status = 200) => new Response(JSON.stringify(data), { status });

test('announces only after the page, backend, and configured model are ready', async () => {
  const output = [];
  console.log = (line) => output.push(line);
  let pageAttempts = 0;
  let healthAttempts = 0;
  let modelAttempts = 0;
  globalThis.fetch = async (url, options = {}) => {
    assert.equal(output.length, 0, 'A readiness message appeared before a completed model response');
    if (url.pathname === '/') {
      pageAttempts++;
      return new Response('Decision Study', { status: pageAttempts === 1 ? 503 : 200 });
    }
    if (url.pathname === '/api/health') {
      healthAttempts++;
      return json({ status: healthAttempts === 1 ? 'unavailable' : 'ok' });
    }
    assert.equal(url.href, 'http://ollama:11434/api/chat');
    const payload = JSON.parse(options.body);
    assert.equal(payload.model, 'qwen3:1.7b');
    assert.equal(payload.think, false);
    assert.equal(payload.options.num_predict, 8);
    modelAttempts++;
    if (modelAttempts === 1) return json({ error: 'model warming up' }, 503);
    if (modelAttempts === 2) return json({ model: 'another-model', done: true, message: { content: 'READY' } });
    if (modelAttempts === 3) return json({ model: settings.model, done: true, message: { content: ' ' } });
    return json({ model: settings.model, done: true, message: { content: 'READY' } });
  };
  await announceWhenReady(settings);
  assert.equal(modelAttempts, 4);
  assert.deepEqual(output, ['Application ready\nOpen http://127.0.0.1:3000/']);
});

test('server shutdown cancels the check without a false announcement', async () => {
  const controller = new AbortController();
  const output = [];
  console.log = console.error = (line) => output.push(line);
  globalThis.fetch = async () => {
    controller.abort();
    throw new Error('Stopped');
  };
  await announceWhenReady({ ...settings, signal: controller.signal });
  assert.deepEqual(output, []);
});

test('rechecks web health after generation before announcing readiness', async () => {
  const output = [];
  console.log = (line) => output.push(line);
  let healthChecks = 0;
  let modelCalls = 0;
  globalThis.fetch = async (url) => {
    assert.equal(output.length, 0);
    if (url.pathname === '/') return new Response('Decision Study');
    if (url.pathname === '/api/health') {
      healthChecks++;
      return json({ status: healthChecks === 2 ? 'unavailable' : 'ok' });
    }
    modelCalls++;
    return json({ model: settings.model, done: true, message: { content: 'READY' } });
  };
  await announceWhenReady(settings);
  assert.equal(modelCalls, 2);
  assert.equal(healthChecks, 4);
  assert.deepEqual(output, ['Application ready\nOpen http://127.0.0.1:3000/']);
});

test('Windows model readiness uses its configured URL and model', async () => {
  const output = [];
  console.log = (line) => output.push(line);
  globalThis.fetch = async (url, options = {}) => {
    if (url.pathname === '/') return new Response('Decision Study');
    if (url.pathname === '/api/health') return json({ status: 'ok' });
    assert.equal(url.href, 'http://host.docker.internal:11434/api/chat');
    const payload = JSON.parse(options.body);
    assert.equal(payload.model, 'llama3:latest');
    assert.equal(payload.think, undefined);
    return json({ model: 'llama3:latest', done: true, message: { content: 'READY' } });
  };
  await announceWhenReady({ ...settings, ollamaUrl: 'http://host.docker.internal:11434', model: 'llama3:latest' });
  assert.deepEqual(output, ['Application ready\nOpen http://127.0.0.1:3000/']);
});
