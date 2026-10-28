import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const webPort = Number(process.env.LABRELAY_WEB_PORT ?? 5173);
const apiOrigin = process.env.LABRELAY_API_ORIGIN ?? "http://127.0.0.1:8010";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: webPort,
    proxy: {
      "/api": {
        target: apiOrigin,
        changeOrigin: true,
      },
    },
  },
});
