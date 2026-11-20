import { defineConfig } from 'vite';
import { announceWhenReady } from './startup-ready.mjs';

export default defineConfig({
  esbuild: { jsx: 'automatic' },
  plugins: [{
    name: 'application-ready',
    configurePreviewServer(server) {
      const controller = new AbortController();
      server.httpServer.once('close', () => controller.abort());
      server.httpServer.once('listening', () => {
        const { port } = server.httpServer.address();
        announceWhenReady({
          appUrl: `http://127.0.0.1:${port}`,
          ollamaUrl: process.env.OLLAMA_BASE_URL,
          model: process.env.OLLAMA_MODEL,
          signal: controller.signal,
        }).catch((error) => {
          if (!controller.signal.aborted) console.error('Application readiness failed:', error.message);
        });
      });
    },
  }],
  server: {
    proxy: {
      '/api/': {
        target: process.env.BACKEND_URL ?? 'http://127.0.0.1:8000',
        ws: true,
      },
    },
  },
});
