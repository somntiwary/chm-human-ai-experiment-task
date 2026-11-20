import { setTimeout as delay } from 'node:timers/promises';

export async function announceWhenReady({ appUrl, ollamaUrl, model, signal }) {
  const startupSignal = AbortSignal.any([
    AbortSignal.timeout(180_000),
    ...(signal ? [signal] : []),
  ]);
  let lastError = 'The services have not responded yet';

  async function checkWeb() {
    const httpSignal = AbortSignal.any([startupSignal, AbortSignal.timeout(5_000)]);
    const page = await fetch(new URL('/', appUrl), { signal: httpSignal });
    if (!page.ok) throw new Error(`Frontend returned HTTP ${page.status}`);
    await page.text();
    const health = await fetch(new URL('/api/health', appUrl), { signal: httpSignal });
    if (!health.ok || (await health.json()).status !== 'ok') {
      throw new Error('The backend or database is unavailable');
    }
  }

  while (!startupSignal.aborted) {
    try {
      await checkWeb();

      const payload = {
        model,
        messages: [{ role: 'user', content: 'Reply with READY only.' }],
        stream: false,
        keep_alive: '10m',
        options: { num_ctx: 2048, num_batch: 64, use_mmap: true, num_predict: 8, temperature: 0 },
      };
      if (model.split('/').at(-1).split(':')[0].toLowerCase() === 'qwen3') payload.think = false;
      const response = await fetch(new URL('/api/chat', ollamaUrl), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: AbortSignal.any([startupSignal, AbortSignal.timeout(120_000)]),
      });
      if (!response.ok) throw new Error(`The model returned HTTP ${response.status}`);
      const result = await response.json();
      if (result.model !== model || result.done !== true || !result.message?.content?.trim()) {
        throw new Error('The configured model did not return a completed reply');
      }
      await checkWeb();
      if (startupSignal.aborted) break;
      console.log('Application ready\nOpen http://127.0.0.1:3000/');
      return;
    } catch (error) {
      lastError = error.message;
    }
    try {
      await delay(1_000, undefined, { signal: startupSignal });
    } catch {
      break;
    }
  }
  if (!signal?.aborted) {
    console.error(`Application readiness check failed: ${lastError}. Check frontend, backend, and Ollama logs.`);
  }
}
