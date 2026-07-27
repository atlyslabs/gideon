"use client"

/*
 * Streaming-reveal animation variants for assistant messages.
 *
 * TypewriterMarkdown (ContentBlocks.tsx) advances a `snapLen` cursor through
 * `fullText` at ~300cps while streaming. This module takes that partial text
 * and renders it with one of 10 reveal animations — character ripple, word
 * blur, chunk rise, block fade, gradient sweep, etc.
 *
 * Switching variants:
 *   NEXT_PUBLIC_STREAM_VARIANT=character-ripple   (default)
 *   NEXT_PUBLIC_STREAM_VARIANT=word-blur
 *   NEXT_PUBLIC_STREAM_VARIANT=chunk-rise
 *   NEXT_PUBLIC_STREAM_VARIANT=gradient-sweep
 *   NEXT_PUBLIC_STREAM_VARIANT=shimmer-trail
 *   NEXT_PUBLIC_STREAM_VARIANT=block-fade
 *   NEXT_PUBLIC_STREAM_VARIANT=pulsing-caret
 *   NEXT_PUBLIC_STREAM_VARIANT=depth-of-field
 *   NEXT_PUBLIC_STREAM_VARIANT=thinking-dots
 *   NEXT_PUBLIC_STREAM_VARIANT=typewriter        (plain char-by-char, no ornament)
 *
 * Only the WHILE-STREAMING render is variant-specific — once streaming ends
 * we always fall back to the full ReactMarkdown render in TypewriterMarkdown
 * so the final output is faithful (links, tables, syntax highlighting, etc.).
 */

import React, { useLayoutEffect, useMemo, useRef, useState, Fragment } from "react"
import type { ReactNode } from "react"

// ---------------------------------------------------------------------------
// Inline tokenizer — splits a line into text / code / bold spans so we can
// re-split text runs by word or character without breaking inline code / bold.
// ---------------------------------------------------------------------------

type InlineToken = { kind: "text" | "code" | "bold"; text: string }

function tokenizeInline(text: string): InlineToken[] {
  const tokens: InlineToken[] = []
  let i = 0
  while (i < text.length) {
    if (text[i] === "`") {
      const end = text.indexOf("`", i + 1)
      if (end === -1) { tokens.push({ kind: "text", text: text.slice(i) }); break }
      tokens.push({ kind: "code", text: text.slice(i + 1, end) })
      i = end + 1
    } else if (text[i] === "*" && text[i + 1] === "*") {
      const end = text.indexOf("**", i + 2)
      if (end === -1) { tokens.push({ kind: "text", text: text.slice(i) }); break }
      tokens.push({ kind: "bold", text: text.slice(i + 2, end) })
      i = end + 2
    } else {
      let j = i
      while (j < text.length && text[j] !== "`" && !(text[j] === "*" && text[j + 1] === "*")) j++
      tokens.push({ kind: "text", text: text.slice(i, j) })
      i = j
    }
  }
  return tokens.filter(t => t.text !== "")
}

function renderTokensPlain(tokens: InlineToken[], kp: string): ReactNode[] {
  return tokens.map((t, i) => {
    if (t.kind === "code") return <code key={`${kp}-${i}`}>{t.text}</code>
    if (t.kind === "bold") return <strong key={`${kp}-${i}`}>{t.text}</strong>
    return <Fragment key={`${kp}-${i}`}>{t.text}</Fragment>
  })
}

type WrapFn = (s: string, key: string) => ReactNode

function renderTokensByWord(tokens: InlineToken[], kp: string, wrap: WrapFn): ReactNode[] {
  const out: ReactNode[] = []
  let k = 0
  tokens.forEach((t, ti) => {
    if (t.kind === "code") { out.push(<code key={`${kp}-${k++}`}>{t.text}</code>); return }
    if (t.kind === "bold") {
      const parts = t.text.split(/(\s+)/)
      const children = parts.map((p, pi) => {
        if (p === "") return null
        if (/^\s+$/.test(p)) return <Fragment key={pi}>{p}</Fragment>
        return wrap(p, `b-${ti}-${pi}`)
      })
      out.push(<strong key={`${kp}-${k++}`}>{children}</strong>)
      return
    }
    const parts = t.text.split(/(\s+)/)
    parts.forEach((p) => {
      if (p === "") return
      if (/^\s+$/.test(p)) { out.push(<Fragment key={`${kp}-${k++}`}>{p}</Fragment>) }
      else { out.push(wrap(p, `${kp}-${k++}`)) }
    })
  })
  return out
}

function renderTokensByChunk(tokens: InlineToken[], kp: string, wrap: WrapFn, chunkSize = 4): ReactNode[] {
  const out: ReactNode[] = []
  let k = 0
  const emitTextRun = (str: string) => {
    const parts = str.split(/(\s+)/)
    parts.forEach((p) => {
      if (p === "") return
      if (/^\s+$/.test(p)) { out.push(<Fragment key={`${kp}-${k++}`}>{p}</Fragment>); return }
      for (let i = 0; i < p.length; i += chunkSize) {
        out.push(wrap(p.slice(i, i + chunkSize), `${kp}-${k++}`))
      }
    })
  }
  tokens.forEach((t, ti) => {
    if (t.kind === "code") { out.push(<code key={`${kp}-${k++}`}>{t.text}</code>); return }
    if (t.kind === "bold") {
      const inner: ReactNode[] = []
      const parts = t.text.split(/(\s+)/)
      let ik = 0
      parts.forEach((p) => {
        if (p === "") return
        if (/^\s+$/.test(p)) { inner.push(<Fragment key={`b-${ti}-${ik++}`}>{p}</Fragment>); return }
        for (let i = 0; i < p.length; i += chunkSize) {
          inner.push(wrap(p.slice(i, i + chunkSize), `b-${ti}-${ik++}`))
        }
      })
      out.push(<strong key={`${kp}-${k++}`}>{inner}</strong>)
      return
    }
    emitTextRun(t.text)
  })
  return out
}

function renderTokensByChar(tokens: InlineToken[], kp: string, wrap: WrapFn): ReactNode[] {
  const out: ReactNode[] = []
  let k = 0
  const emitRun = (str: string) => {
    for (const ch of str) {
      if (ch === " " || ch === "\n") out.push(<Fragment key={`${kp}-${k++}`}>{ch}</Fragment>)
      else out.push(wrap(ch, `${kp}-${k++}`))
    }
  }
  tokens.forEach((t, ti) => {
    if (t.kind === "code") { out.push(<code key={`${kp}-${k++}`}>{t.text}</code>); return }
    if (t.kind === "bold") {
      const inner: ReactNode[] = []
      let ik = 0
      for (const ch of t.text) {
        if (ch === " " || ch === "\n") inner.push(<Fragment key={`b-${ti}-${ik++}`}>{ch}</Fragment>)
        else inner.push(wrap(ch, `b-${ti}-${ik++}`))
      }
      out.push(<strong key={`${kp}-${k++}`}>{inner}</strong>)
      return
    }
    emitRun(t.text)
  })
  return out
}

// ---------------------------------------------------------------------------
// Block parser — lightweight markdown subset used only DURING streaming.
// (On stream end, TypewriterMarkdown snaps to the real ReactMarkdown render.)
// ---------------------------------------------------------------------------

type Block =
  | { type: "h1" | "h2" | "h3" | "p"; text: string }
  | { type: "ul" | "ol"; items: string[] }
  | { type: "pre"; lang: string; code: string }

function parseMarkdown(src: string): Block[] {
  const lines = src.split("\n")
  const blocks: Block[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (line.startsWith("### ")) { blocks.push({ type: "h3", text: line.slice(4) }); i++ }
    else if (line.startsWith("## ")) { blocks.push({ type: "h2", text: line.slice(3) }); i++ }
    else if (line.startsWith("# ")) { blocks.push({ type: "h1", text: line.slice(2) }); i++ }
    else if (line.startsWith("```")) {
      const lang = line.slice(3).trim()
      const code: string[] = []
      i++
      while (i < lines.length && !lines[i].startsWith("```")) { code.push(lines[i]); i++ }
      i++
      blocks.push({ type: "pre", lang, code: code.join("\n") })
    } else if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*]\s+/, "")); i++ }
      blocks.push({ type: "ul", items })
    } else if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*\d+\.\s+/, "")); i++ }
      blocks.push({ type: "ol", items })
    } else if (line.trim() === "") { i++ }
    else {
      const para = [line]; i++
      while (
        i < lines.length &&
        lines[i].trim() !== "" &&
        !lines[i].startsWith("#") &&
        !/^\s*[-*]\s+/.test(lines[i]) &&
        !/^\s*\d+\.\s+/.test(lines[i]) &&
        !lines[i].startsWith("```")
      ) { para.push(lines[i]); i++ }
      blocks.push({ type: "p", text: para.join(" ") })
    }
  }
  return blocks
}

type Mode = "plain" | "word" | "chunk" | "char"

function MarkdownRender({ text, mode = "plain", wrap }: { text: string; mode?: Mode; wrap?: WrapFn }) {
  const blocks = useMemo(() => parseMarkdown(text), [text])
  const renderLine = (s: string, kp: string): ReactNode[] => {
    const toks = tokenizeInline(s)
    if (mode === "word" && wrap) return renderTokensByWord(toks, kp, wrap)
    if (mode === "chunk" && wrap) return renderTokensByChunk(toks, kp, wrap)
    if (mode === "char" && wrap) return renderTokensByChar(toks, kp, wrap)
    return renderTokensPlain(toks, kp)
  }
  // No hardcoded color/font-size here — the caller's wrapper sets those so
  // this renderer can be reused for both main chat (primary text) and thinking
  // blocks (tertiary text, smaller font).
  return (
    <div className="prose prose-invert prose-sm max-w-none">
      {blocks.map((b, bi) => {
        if (b.type === "h1") return <h1 key={bi}>{renderLine(b.text, `h-${bi}`)}</h1>
        if (b.type === "h2") return <h2 key={bi}>{renderLine(b.text, `h-${bi}`)}</h2>
        if (b.type === "h3") return <h3 key={bi}>{renderLine(b.text, `h-${bi}`)}</h3>
        if (b.type === "p") return <p key={bi}>{renderLine(b.text, `p-${bi}`)}</p>
        if (b.type === "ul") return <ul key={bi}>{b.items.map((it, ii) => <li key={ii}>{renderLine(it, `l-${bi}-${ii}`)}</li>)}</ul>
        if (b.type === "ol") return <ol key={bi}>{b.items.map((it, ii) => <li key={ii}>{renderLine(it, `l-${bi}-${ii}`)}</li>)}</ol>
        if (b.type === "pre") return <pre key={bi}><code>{b.code}</code></pre>
        return null
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Variant renderers — each receives the already-snapped partial text.
// ---------------------------------------------------------------------------

type VariantProps = { shown: string; fullText: string }

function Typewriter({ shown }: VariantProps) {
  return (
    <>
      <MarkdownRender text={shown} />
      <span className="gd-stream-caret" />
    </>
  )
}

function PulsingCaret({ shown }: VariantProps) {
  return (
    <>
      <MarkdownRender text={shown} />
      <span className="gd-stream-caret-pulse" />
    </>
  )
}

function WordBlur({ shown }: VariantProps) {
  const wrap: WrapFn = (w, k) => <span className="gd-v-word-blur" key={k}>{w}</span>
  return <MarkdownRender text={shown} mode="word" wrap={wrap} />
}

function ChunkRise({ shown }: VariantProps) {
  const wrap: WrapFn = (c, k) => <span className="gd-v-chunk-rise" key={k}>{c}</span>
  return <MarkdownRender text={shown} mode="chunk" wrap={wrap} />
}

function CharacterRipple({ shown }: VariantProps) {
  const wrap: WrapFn = (c, k) => <span className="gd-v-char-ripple" key={k}>{c}</span>
  return <MarkdownRender text={shown} mode="char" wrap={wrap} />
}

function DepthOfField({ shown }: VariantProps) {
  const wrap: WrapFn = (w, k) => <span className="gd-v-dof" key={k}>{w}</span>
  return <MarkdownRender text={shown} mode="word" wrap={wrap} />
}

function ThinkingDots({ shown }: VariantProps) {
  const wrap: WrapFn = (w, k) => <span className="gd-v-dissolve" key={k}>{w}</span>
  return (
    <>
      <MarkdownRender text={shown} mode="word" wrap={wrap} />
      <span className="gd-v-dots" aria-hidden>
        <span /><span /><span />
      </span>
    </>
  )
}

function GradientSweep({ shown }: VariantProps) {
  // Last block shimmers; everything before it renders plainly.
  const blocks = useMemo(() => parseMarkdown(shown), [shown])
  return (
    <div className="prose prose-invert prose-sm max-w-none">
      {blocks.map((b, bi) => {
        const isLast = bi === blocks.length - 1
        const cls = isLast ? "gd-v-sweep" : ""
        if (b.type === "h1") return <h1 key={bi} className={cls}>{renderTokensPlain(tokenizeInline(b.text), `h-${bi}`)}</h1>
        if (b.type === "h2") return <h2 key={bi} className={cls}>{renderTokensPlain(tokenizeInline(b.text), `h-${bi}`)}</h2>
        if (b.type === "h3") return <h3 key={bi} className={cls}>{renderTokensPlain(tokenizeInline(b.text), `h-${bi}`)}</h3>
        if (b.type === "p") return <p key={bi} className={cls}>{renderTokensPlain(tokenizeInline(b.text), `p-${bi}`)}</p>
        if (b.type === "ul") return (
          <ul key={bi}>
            {b.items.map((it, ii) => {
              const lastItem = isLast && ii === b.items.length - 1
              return <li key={ii} className={lastItem ? "gd-v-sweep" : ""}>{renderTokensPlain(tokenizeInline(it), `l-${bi}-${ii}`)}</li>
            })}
          </ul>
        )
        if (b.type === "ol") return (
          <ol key={bi}>
            {b.items.map((it, ii) => {
              const lastItem = isLast && ii === b.items.length - 1
              return <li key={ii} className={lastItem ? "gd-v-sweep" : ""}>{renderTokensPlain(tokenizeInline(it), `l-${bi}-${ii}`)}</li>
            })}
          </ol>
        )
        if (b.type === "pre") return <pre key={bi}><code>{b.code}</code></pre>
        return null
      })}
    </div>
  )
}

function ShimmerTrail({ shown }: VariantProps) {
  const sentinelRef = useRef<HTMLSpanElement | null>(null)
  const areaRef = useRef<HTMLDivElement | null>(null)
  const [pos, setPos] = useState({ x: -999, y: -999 })

  useLayoutEffect(() => {
    if (!sentinelRef.current || !areaRef.current) return
    const r = sentinelRef.current.getBoundingClientRect()
    const pr = areaRef.current.getBoundingClientRect()
    setPos({ x: r.left - pr.left, y: r.top - pr.top + r.height / 2 })
  }, [shown])

  return (
    <div ref={areaRef} style={{ position: "relative" }}>
      <MarkdownRender text={shown} />
      <span ref={sentinelRef} style={{ display: "inline-block", width: 1, height: "1em", verticalAlign: "text-bottom" }} />
      {pos.x > -100 && <div className="gd-v-shimmer" style={{ left: pos.x, top: pos.y }} />}
    </div>
  )
}

function BlockFade({ shown, fullText }: VariantProps) {
  // Show only fully-arrived blocks (all except the one currently streaming in).
  const fullBlocks = useMemo(() => parseMarkdown(fullText), [fullText])
  const shownBlocks = useMemo(() => parseMarkdown(shown), [shown])
  const streamingUp = shown !== fullText
  const visible = streamingUp ? fullBlocks.slice(0, Math.max(0, shownBlocks.length - 1)) : fullBlocks

  return (
    <div className="prose prose-invert prose-sm max-w-none">
      {visible.map((b, bi) => (
        <div className="gd-v-block-fade" key={bi} style={{ animationDelay: `${Math.min(bi, 3) * 40}ms` }}>
          {b.type === "h1" && <h1>{renderTokensPlain(tokenizeInline(b.text), `h-${bi}`)}</h1>}
          {b.type === "h2" && <h2>{renderTokensPlain(tokenizeInline(b.text), `h-${bi}`)}</h2>}
          {b.type === "h3" && <h3>{renderTokensPlain(tokenizeInline(b.text), `h-${bi}`)}</h3>}
          {b.type === "p" && <p>{renderTokensPlain(tokenizeInline(b.text), `p-${bi}`)}</p>}
          {b.type === "ul" && <ul>{b.items.map((it, ii) => <li key={ii}>{renderTokensPlain(tokenizeInline(it), `l-${bi}-${ii}`)}</li>)}</ul>}
          {b.type === "ol" && <ol>{b.items.map((it, ii) => <li key={ii}>{renderTokensPlain(tokenizeInline(it), `l-${bi}-${ii}`)}</li>)}</ol>}
          {b.type === "pre" && <pre><code>{b.code}</code></pre>}
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Registry + env-var selector
// ---------------------------------------------------------------------------

export type VariantName =
  | "character-ripple"
  | "typewriter"
  | "pulsing-caret"
  | "word-blur"
  | "chunk-rise"
  | "depth-of-field"
  | "thinking-dots"
  | "gradient-sweep"
  | "shimmer-trail"
  | "block-fade"

export const VARIANTS: Record<VariantName, { label: string; render: (p: VariantProps) => ReactNode }> = {
  "character-ripple": { label: "Character ripple", render: (p) => <CharacterRipple {...p} /> },
  "typewriter":       { label: "Character typewriter", render: (p) => <Typewriter {...p} /> },
  "pulsing-caret":    { label: "Pulsing caret", render: (p) => <PulsingCaret {...p} /> },
  "word-blur":        { label: "Word blur-in", render: (p) => <WordBlur {...p} /> },
  "chunk-rise":       { label: "Chunk rise", render: (p) => <ChunkRise {...p} /> },
  "depth-of-field":   { label: "Depth of field", render: (p) => <DepthOfField {...p} /> },
  "thinking-dots":    { label: "Thinking dots", render: (p) => <ThinkingDots {...p} /> },
  "gradient-sweep":   { label: "Gradient sweep", render: (p) => <GradientSweep {...p} /> },
  "shimmer-trail":    { label: "Shimmer trail", render: (p) => <ShimmerTrail {...p} /> },
  "block-fade":       { label: "Block fade", render: (p) => <BlockFade {...p} /> },
}

const DEFAULT_VARIANT: VariantName = "character-ripple"

export function getStreamVariant(): VariantName {
  const raw = (process.env.NEXT_PUBLIC_STREAM_VARIANT || "").trim()
  // Own-property check only — `in` would accept prototype keys like
  // "toString" / "constructor" and crash when we try to render them.
  if (raw && Object.prototype.hasOwnProperty.call(VARIANTS, raw)) {
    return raw as VariantName
  }
  return DEFAULT_VARIANT
}

export function renderStreamVariant(props: VariantProps): ReactNode {
  const name = getStreamVariant()
  return VARIANTS[name].render(props)
}

/** Force a specific variant regardless of env var — used for thinking blocks,
 * which always render with `thinking-dots` so the trailing bouncing dots
 * signal "still thinking" while words dissolve in. */
export function renderNamedVariant(name: VariantName, props: VariantProps): ReactNode {
  return VARIANTS[name].render(props)
}
