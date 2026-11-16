import type { NextConfig } from "next";

const config: NextConfig = {
  distDir: process.env.NEXT_PUBLIC_F1RI_PRIVATE === "true" ? process.env.F1RI_PRIVATE_DIST ?? ".next-private" : ".next",
  poweredByHeader: false,
};
export default config;
