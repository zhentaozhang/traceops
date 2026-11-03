import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 单独的 vitest 配置：不污染 vite.config.ts（构建配置不该带测试字段）
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    // 覆盖率只统计 domain/ 和 data/ —— 页面组件先不进门槛，避免为了凑数字写无意义的测试
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/domain/**", "src/data/**"],
      thresholds: {
        lines: 70,
        functions: 70,
        branches: 60,
        statements: 70,
      },
    },
  },
});
