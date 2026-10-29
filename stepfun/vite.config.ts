import { defineConfig } from 'vite';

export default defineConfig({
  publicDir: false,
  server: { host: '127.0.0.1', port: 4177, watch: { ignored: ['**/public/**', '**/artifacts/**'] } },
  worker: { format: 'es' },
  build: { target: 'es2022', chunkSizeWarningLimit: 1800 },
  optimizeDeps: { exclude: ['@huggingface/transformers'] },
});
