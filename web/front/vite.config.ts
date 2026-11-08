// Build de l'appli (lot 4b) : sortie statique dans web/static/app, servie par FastAPI (web/app.py).
import path from "node:path";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  build: {
    outDir: path.resolve(__dirname, "../static/app"),
    emptyOutDir: true,
    // Polices et images en fichiers (CSP font-src 'self', pas de data:) ; noms à empreinte, build reproductible.
    assetsInlineLimit: 0,
    sourcemap: false,
    modulePreload: { polyfill: false },
    chunkSizeWarningLimit: 900,
  },
  server: {
    // Développement : `npm run dev` avec le backend sur 127.0.0.1:8095 (HOMEOS_WEB_ORIGINE=http://localhost:5173).
    proxy: {
      "/api": { target: "http://127.0.0.1:8095", changeOrigin: false },
      "/auth": { target: "http://127.0.0.1:8095", changeOrigin: false },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
  },
});
