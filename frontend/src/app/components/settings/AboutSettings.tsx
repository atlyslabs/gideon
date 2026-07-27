"use client"

import { useRouter } from "next/navigation"
import dynamic from "next/dynamic"
import type { LucideIcon } from "lucide-react"
import { Info, Sparkles, Zap, Server, Brain, ExternalLink, Wrench, Rocket } from "lucide-react"

const GideonHead = dynamic(() => import("../GideonHead"), { ssr: false })

const STATS = [
  { label: "Tools", value: "143" },
  { label: "Providers", value: "17" },
  { label: "AI features", value: "14" },
  { label: "Schedulers", value: "5" },
]

const MODELS = [
  { name: "Claude Opus 4.7", role: "Advisor — strategic guidance on tough calls", accent: "#a78bfa" },
  { name: "Claude Sonnet 4.6", role: "Chat, agents, workflows, triggers, drafts, prep", accent: "#60a5fa" },
  { name: "Claude Haiku 4.5", role: "Titles, suggestions, and lightweight classification", accent: "#34d399" },
]

const CHANGELOG = [
  {
    version: "v0.1.0",
    items: [
      "Initial open-source release.",
      "Provider-neutral LLM layer: run Gideon on Anthropic, OpenAI, or any OpenAI-compatible endpoint.",
      "Chat with streaming, extended thinking, tool use, and in-chat React previews.",
      "Integrations for Slack, Gmail, Google Calendar, Drive, Docs, Sheets, Notion, Linear, GitHub, and more.",
      "Scheduled workflows, skills, memory, and a daily hub.",
      "Trigger Gideon from Slack, email, or WhatsApp.",
    ],
  },
]

const LINKS = [
  { label: "Source (GitHub)", href: "https://github.com/atlyslabs/gideon" },
  { label: "Report an issue", href: "https://github.com/atlyslabs/gideon/issues" },
]

const STACK = [
  { label: "Frontend", value: "Next.js 15, React 19, Tailwind, Three.js" },
  { label: "Backend", value: "FastAPI, Python 3.13, SQLAlchemy async" },
  { label: "Data", value: "PostgreSQL, Redis, Neo4j (Graphiti)" },
  { label: "LLM", value: "Claude (Opus / Sonnet / Haiku) via Anthropic API" },
  { label: "Deploy", value: "Kubernetes, Istio, Google Artifact Registry" },
]

const BUILD_SHA = process.env.NEXT_PUBLIC_GIT_SHA?.slice(0, 7) || "dev"

export default function AboutSettings() {
  const router = useRouter()

  return (
    <div className="max-w-[720px] mx-auto py-8 px-8">
      {/* Section header */}
      <div className="flex items-center gap-3 mb-6">
        <div
          className="w-10 h-10 rounded-xl flex items-center justify-center"
          style={{ background: "var(--surface-elevated)" }}
        >
          <Info size={20} style={{ color: "var(--text-secondary)" }} />
        </div>
        <div>
          <h1 className="text-[16px] font-semibold" style={{ color: "var(--text-primary)" }}>
            About Gideon
          </h1>
          <p className="text-[13px] mt-0.5" style={{ color: "var(--text-secondary)" }}>
            What's under the hood, and how we got here.
          </p>
        </div>
      </div>

      {/* Hero with floating head */}
      <div
        className="rounded-xl relative overflow-hidden mb-4"
        style={{ border: "1px solid var(--border)", background: "var(--surface-elevated)" }}
      >
        <div
          className="absolute -top-20 -right-20 w-80 h-80 rounded-full pointer-events-none"
          style={{ background: "radial-gradient(circle, rgba(96,165,250,0.18), transparent 60%)" }}
        />
        <div className="relative flex items-start gap-4 p-6 pr-2">
          <div className="flex-1 min-w-0 py-2">
            <div className="flex items-center gap-2 mb-2">
              <Sparkles size={14} style={{ color: "#a78bfa" }} />
              <span
                className="text-[11px] uppercase tracking-wider font-semibold"
                style={{ color: "#a78bfa" }}
              >
                AI Work Assistant
              </span>
            </div>
            <h2 className="text-[22px] font-semibold mb-2" style={{ color: "var(--text-primary)" }}>
              Move faster. Do more.
            </h2>
            <p className="text-[13px] leading-relaxed" style={{ color: "var(--text-secondary)" }}>
              Gideon connects to your tools, learns how you work, and takes the first 80% off your plate
              — so you can focus on the 20% where judgment matters.
            </p>
          </div>
          <div className="shrink-0 -mt-4 -mr-4 opacity-90 pointer-events-none">
            <GideonHead size={200} />
          </div>
        </div>
      </div>

      {/* Stats */}
      <div
        className="grid grid-cols-4 gap-px mb-4 rounded-xl overflow-hidden"
        style={{ border: "1px solid var(--border)", background: "var(--border)" }}
      >
        {STATS.map((s) => (
          <div
            key={s.label}
            className="px-3 py-4 text-center"
            style={{ background: "var(--surface)" }}
          >
            <div className="text-[22px] font-semibold" style={{ color: "var(--text-primary)" }}>
              {s.value}
            </div>
            <div
              className="text-[11px] uppercase tracking-wider mt-1"
              style={{ color: "var(--text-tertiary)" }}
            >
              {s.label}
            </div>
          </div>
        ))}
      </div>

                  {/* AI Models */}
      <SectionCard icon={Brain} title="AI Models">
        {MODELS.map((m) => (
          <div
            key={m.name}
            className="flex items-center gap-3 px-6 py-3"
            style={{ borderTop: "1px solid var(--border)" }}
          >
            <div
              className="w-2 h-2 rounded-full shrink-0"
              style={{ background: m.accent, boxShadow: `0 0 10px ${m.accent}` }}
            />
            <div className="flex-1 min-w-0">
              <div className="text-[13px] font-medium" style={{ color: "var(--text-primary)" }}>
                {m.name}
              </div>
              <div className="text-[12px]" style={{ color: "var(--text-tertiary)" }}>
                {m.role}
              </div>
            </div>
          </div>
        ))}
      </SectionCard>

      {/* Stack */}
      <SectionCard icon={Server} title="Stack">
        <div className="px-6 py-4 space-y-1.5 text-[12px] leading-relaxed">
          {STACK.map((s) => (
            <div key={s.label}>
              <span style={{ color: "var(--text-tertiary)" }}>{s.label}: </span>
              <span style={{ color: "var(--text-secondary)" }}>{s.value}</span>
            </div>
          ))}
        </div>
      </SectionCard>

      {/* Changelog */}
      <SectionCard icon={Rocket} title="What's new">
        {CHANGELOG.map((release, rIdx) => (
          <div
            key={release.version}
            className="px-6 py-4"
            style={{ borderTop: rIdx === 0 ? "1px solid var(--border)" : "1px solid var(--border)" }}
          >
            <div
              className="text-[11px] uppercase tracking-wider font-semibold mb-2"
              style={{ color: "var(--text-tertiary)" }}
            >
              {release.version}
            </div>
            <ul className="space-y-1">
              {release.items.map((item) => (
                <li
                  key={item}
                  className="text-[12px] leading-relaxed pl-4 relative"
                  style={{ color: "var(--text-secondary)" }}
                >
                  <span
                    className="absolute left-0 top-[7px] w-1 h-1 rounded-full"
                    style={{ background: "var(--text-tertiary)" }}
                  />
                  {item}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </SectionCard>

      {/* Links */}
      <SectionCard icon={Zap} title="Resources">
        {LINKS.map((l) => (
          <a
            key={l.href}
            href={l.href}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center justify-between px-6 py-3 text-[13px] transition-colors hover:bg-white/[0.02]"
            style={{ borderTop: "1px solid var(--border)", color: "var(--text-primary)" }}
          >
            <span>{l.label}</span>
            <ExternalLink size={13} style={{ color: "var(--text-tertiary)" }} />
          </a>
        ))}
      </SectionCard>

      {/* Credits + easter egg */}
      <div className="text-center py-6">
        <p className="text-[12px]" style={{ color: "var(--text-secondary)" }}>
          Created by{" "}
          <a
            href="https://github.com/abhiban9"
            target="_blank"
            rel="noopener noreferrer"
            className="underline decoration-dotted underline-offset-2 transition-colors hover:text-white"
            style={{ color: "var(--text-primary)" }}
          >
            Abhishek Banerjee
          </a>
        </p>
        <p className="text-[12px] mt-1" style={{ color: "var(--text-secondary)" }}>
          Open source under Apache-2.0 ·{" "}
          <a
            href="https://github.com/atlyslabs/gideon"
            target="_blank"
            rel="noopener noreferrer"
            className="underline decoration-dotted underline-offset-2 transition-colors hover:text-white"
            style={{ color: "var(--text-primary)" }}
          >
            GitHub
          </a>
        </p>
        <p className="text-[11px] mt-1" style={{ color: "var(--text-tertiary)" }}>
          Build <span style={{ fontFamily: "var(--font-mono)" }}>{BUILD_SHA}</span>
        </p>
        <button
          onClick={() => router.push("/cameos")}
          className="mt-4 inline-flex items-center gap-1.5 text-[11px] opacity-40 hover:opacity-90 transition-opacity"
          style={{ color: "var(--text-tertiary)" }}
        >
          <Wrench size={11} />
          psst — try the 404
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// SectionCard — shared card wrapper with icon + title header
// ---------------------------------------------------------------------------

function SectionCard({
  icon: Icon,
  title,
  children,
}: {
  icon: LucideIcon
  title: string
  children: React.ReactNode
}) {
  return (
    <div className="rounded-xl overflow-hidden mb-4" style={{ border: "1px solid var(--border)" }}>
      <div className="px-6 pt-5 pb-3">
        <div className="flex items-center gap-2">
          <Icon size={14} style={{ color: "var(--text-secondary)" }} />
          <p
            className="text-[13px] font-semibold uppercase tracking-wide"
            style={{ color: "var(--text-secondary)" }}
          >
            {title}
          </p>
        </div>
      </div>
      {children}
    </div>
  )
}
