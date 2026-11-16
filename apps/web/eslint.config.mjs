import { defineConfig, globalIgnores } from "eslint/config";
import next from "eslint-config-next/core-web-vitals";
import typescript from "eslint-config-next/typescript";

export default defineConfig([
  ...next,
  ...typescript,
  globalIgnores([".next/**", ".next-private*/**", "playwright-report/**", "test-results*/**", "next-env.d.ts"]),
]);
