/**
 * POST /api/preview/compile
 *
 * Thin proxy to an external "preview builder" service that compiles the
 * React snippets Gideon writes in chat into a renderable bundle.
 *
 * OPTIONAL. Set PREVIEW_BUILDER_URL to enable in-chat React previews;
 * leave it unset and this route returns 501 while the rest of the app
 * works normally. The builder is a separate service that accepts
 * `POST /compile` with `{ code }` and returns the compiled bundle as
 * JSON. It is not bundled with this repo — the compile pipeline (vite +
 * rollup + tailwind) is deliberately kept out of the Next.js image,
 * whose file tracing can't follow rollup's arch-suffixed native binding
 * resolution and ends up shipping the entire node_modules tree.
 *
 * Auth passthrough: the caller sends a Bearer token which is forwarded
 * unchanged for the builder to validate. No double-auth here.
 */

import { NextRequest, NextResponse } from "next/server"

const PREVIEW_BUILDER_URL = process.env.PREVIEW_BUILDER_URL || ""

// Compile is CPU-bound on the builder side — give it room. Builder
// also enforces a 15s wall-clock cap, so this just covers transport
// + cold-start overhead.
const PROXY_TIMEOUT_MS = 30_000

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

export async function POST(request: NextRequest) {
  if (!PREVIEW_BUILDER_URL) {
    return NextResponse.json(
      {
        error: {
          message:
            "In-chat previews are not configured. Set PREVIEW_BUILDER_URL to a service exposing POST /compile.",
        },
      },
      { status: 501 },
    )
  }

  const authHeader = request.headers.get("authorization")
  if (!authHeader) {
    return NextResponse.json(
      { error: { message: "Missing Authorization header" } },
      { status: 401 },
    )
  }

  // Read the body as text so we forward exactly what the FE sent
  // (avoids JSON re-serialization quirks for unicode / whitespace).
  let bodyText: string
  try {
    bodyText = await request.text()
  } catch {
    return NextResponse.json(
      { error: { message: "Failed to read request body" } },
      { status: 400 },
    )
  }

  let res: Response
  try {
    res = await fetch(`${PREVIEW_BUILDER_URL}/compile`, {
      method: "POST",
      headers: {
        Authorization: authHeader,
        "Content-Type": "application/json",
      },
      body: bodyText,
      signal: AbortSignal.timeout(PROXY_TIMEOUT_MS),
    })
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e)
    return NextResponse.json(
      { error: { message: `preview-builder unreachable: ${msg}` } },
      { status: 502 },
    )
  }

  // Pass the builder's response through, including status code so 401
  // / 413 / 500 reach the FE with the right semantics.
  const responseText = await res.text()
  return new NextResponse(responseText, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  })
}
