import path from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = {
    ...loadEnv(mode, process.cwd(), ""),
    ...process.env,
  };
  const resolvedBackendPort = env.BACKEND_PORT?.trim() || "8000";
  const resolvedFrontendPort = Number(env.FRONTEND_PORT?.trim() || "5173");
  const backendBaseUrl =
    env.BACKEND_URL?.trim() || `http://localhost:${resolvedBackendPort}`;

  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
        "@shared": path.resolve(__dirname, "./shared"),
      },
    },
    server: {
      port: resolvedFrontendPort,
      proxy: {
        // 后端路由注册在 /api 前缀下（如 /api/v1/agent-runner/*），必须原样透传。
        "/api": {
          target: backendBaseUrl,
          changeOrigin: true,
        },
      },
    },
    preview: {
      port: 4173,
      proxy: {
        // 生产构建预览时同样把 /api 透传到后端，便于 E2E 与手动验证。
        "/api": {
          target: backendBaseUrl,
          changeOrigin: true,
        },
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.includes("node_modules/@refinedev") || id.includes("node_modules/@tanstack/react-query")) {
              return "refine";
            }
            if (
              id.includes("node_modules/react") ||
              id.includes("node_modules/react-dom") ||
              id.includes("node_modules/react-router")
            ) {
              return "react-vendor";
            }
            if (
              id.includes("node_modules/@radix-ui") ||
              id.includes("node_modules/lucide-react") ||
              id.includes("node_modules/class-variance-authority") ||
              id.includes("node_modules/clsx") ||
              id.includes("node_modules/tailwind-merge")
            ) {
              return "ui-vendor";
            }
          },
        },
      },
    },
  };
});
