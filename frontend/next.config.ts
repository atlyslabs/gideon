import type { NextConfig } from "next"

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  compress: true,
  output: "standalone",
  // Vite ecosystem packages must NOT be webpack-bundled. They ship with
  // native binaries (fsevents.node), dynamic requires (rollup's
  // native.js), and `import.meta` access that webpack can't handle. The
  // /api/preview/compile route loads them via dynamic import at runtime
  // — telling Next.js they're external preserves Node's `require`
  // resolver for these packages and skips the bundling pass entirely.
  // Includes transitive native deps that surfaced as build errors:
  // fsevents (macOS file watcher), rollup, lightningcss / esbuild
  // (bundled inside @tailwindcss/vite + vite itself).
  serverExternalPackages: [
    "vite",
    "rollup",
    "fsevents",
    "esbuild",
    "lightningcss",
    "@vitejs/plugin-react",
    "@tailwindcss/vite",
  ],
  // The preview-compile route loads vite + @tailwindcss/vite at
  // runtime. Both packages access files via dynamic paths Next's
  // @vercel/nft tracer can't see:
  //   - vite/dist/client/{client,env}.mjs — referenced via
  //     `resolve(VITE_PACKAGE_DIR, "dist/client/client.mjs")` in
  //     vite/dist/node/constants.js. Without it Vite throws
  //     `cannot test case insensitive FS, CLIENT_ENTRY does not
  //     point to an existing file` during init.
  //   - @tailwindcss/node/dist/esm-cache.loader.mjs — registered via
  //     `module.register(require.resolve("@tailwindcss/node/esm-cache-loader"))`
  //     in @tailwindcss/node/dist/index.mjs. The wider `dist/**`
  //     glob covers any sibling files the same pattern reaches for.
  // Glob paths are resolved relative to the gideon-ui project dir.
  outputFileTracingIncludes: {
    "/api/preview/compile": [
      "./node_modules/vite/dist/client/**",
      // yarn 1's resolver nests a second @tailwindcss/node under
      // @tailwindcss/vite/node_modules/, and that's the one loaded
      // at runtime (Node resolves from the calling package). The
      // `**` matches both the top-level and any nested copies.
      "./node_modules/**/@tailwindcss/node/dist/**",
      // tailwindcss itself (the CSS engine) — resolved by vite at
      // runtime when the staged preview.css contains `@import
      // "tailwindcss"`. Only the nested copy under @tailwindcss/vite
      // gets traced by nft; the workspace-level one is missing.
      "./node_modules/**/tailwindcss/**",
      // @tailwindcss/oxide is Tailwind v4's Rust class scanner.
      // Loaded dynamically with the same require-by-platform-name
      // pattern as rollup; nft can't follow.
      "./node_modules/**/@tailwindcss/oxide/**",
      "./node_modules/**/@tailwindcss/oxide-linux-*-musl/**",
    ],
  },
  async rewrites() {
    return [
      // All app pages rewrite to root (SPA routing handled client-side in Layout.tsx)
      { source: "/skills", destination: "/" },
      { source: "/skills/:id", destination: "/" },
      { source: "/workflows", destination: "/" },
      { source: "/workflows/:id", destination: "/" },
      // Workflow run detail pages (Slack DM links land here on a fresh nav).
      { source: "/workflows/:id/runs/:runId", destination: "/" },
      { source: "/settings", destination: "/" },
      { source: "/integrations", destination: "/" },
      { source: "/memory", destination: "/" },
      { source: "/files", destination: "/" },
      { source: "/usage", destination: "/" },
      { source: "/todo", destination: "/" },
      { source: "/chat", destination: "/" },
      { source: "/commands", destination: "/" },
      { source: "/surveys", destination: "/" },
      { source: "/surveys/:slug", destination: "/" },
      { source: "/explore", destination: "/" },
    ]
  },
}

export default nextConfig
