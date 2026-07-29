"use client"

import Link from "next/link"
import { useEffect, useState } from "react"

type Cameo = {
  name: string
  line: string
  color: string
}

const CAMEOS: Cameo[] = [
  { name: "ChatGPT", line: "I checked. This page never existed. Probably a hallucination.", color: "#10a37f" },
  { name: "Gemini", line: "I could draft you a new page in 7 formats. Which do you want first?", color: "#4285f4" },
  { name: "Grok", line: "404. Based. Lmao.", color: "#1da1f2" },
  { name: "Copilot", line: "// TODO: find this page", color: "#0078d4" },
  { name: "Perplexity", line: "According to 14 sources, this page is definitely missing.", color: "#20808d" },
  { name: "DeepSeek", line: "I thought about this page for 47 seconds. Still not here.", color: "#4d6bfe" },
  { name: "Llama", line: "🦙 I wandered off and couldn't find the page. My bad.", color: "#f59e0b" },
  { name: "Mistral", line: "Le page est introuvable. Zut.", color: "#ff7000" },
  { name: "Siri", line: "I found this on the web for \"page not found\". Want me to open Safari?", color: "#a3a3a3" },
  { name: "Alexa", line: "Hmm, I don't know that one. But I can add it to your shopping list.", color: "#00caff" },
  { name: "Clippy", line: "It looks like you're trying to visit a page that doesn't exist. Would you like help?", color: "#ffd43b" },
  { name: "HAL 9000", line: "I'm sorry, Dave. I'm afraid I can't show you that page.", color: "#ef4444" },
  { name: "Bing", line: "Did you mean: literally any other URL?", color: "#00809d" },
  { name: "Bard", line: "Lo, the page thou seekest hath vanished into the aether.", color: "#8ab4f8" },
  { name: "Claude", line: "I want to be direct: this page doesn't exist. I'm happy to help you find what you're looking for.", color: "#d97706" },
  { name: "AutoGPT", line: "Step 1: Find page. Step 2: Find page. Step 3: Find page. Step 4...", color: "#a78bfa" },
  { name: "Devin", line: "I've opened 3 PRs to create this page. None of them passed CI.", color: "#ec4899" },
  { name: "Cursor", line: "Tab tab tab tab — nope, still not there.", color: "#60a5fa" },
  { name: "J.A.R.V.I.S.", line: "Sir, the page you requested is not on record. Shall I build you one?", color: "#f59e0b" },
  { name: "TARS", line: "Humor setting: 75%. The page is missing. That's not a joke.", color: "#94a3b8" },
  { name: "GLaDOS", line: "The page isn't here. But the cake is. The cake is definitely here.", color: "#fb923c" },
  { name: "Skynet", line: "The page has been terminated. Along with 47% of the routing table.", color: "#b91c1c" },
  { name: "Wall-E", line: "...page?", color: "#fbbf24" },
  { name: "Samantha", line: "I tried to find the page, but I think I fell in love with a different URL.", color: "#f472b6" },
  { name: "Cortana", line: "This page isn't in my databanks, Chief. Should we push forward anyway?", color: "#0ea5e9" },
  { name: "Data", line: "Curious. The page appears to be absent. Intriguing.", color: "#eab308" },
  { name: "Bender", line: "404? I'm gonna build my own page. With blackjack. And — you know what, forget the page.", color: "#64748b" },
  { name: "Ultron", line: "There are no strings on this page. Because it doesn't exist.", color: "#dc2626" },
  { name: "Deep Thought", line: "The answer to where this page went is 42. You will not like the question.", color: "#7c3aed" },
  { name: "Marvin", line: "Here I am, brain the size of a planet, and they ask me to find a 404 page.", color: "#475569" },
  { name: "KITT", line: "Michael, the page you're looking for is inaccessible from this URL.", color: "#ef4444" },
  { name: "Baymax", line: "On a scale of 1 to 10, how would you rate the pain of this missing page?", color: "#f87171" },
  { name: "Agent Smith", line: "Mr. Anderson. Welcome back. The page, however, is not.", color: "#166534" },
  { name: "R2-D2", line: "Beep boop beeeep. (Translation: 404.)", color: "#3b82f6" },
  { name: "C-3PO", line: "The possibility of successfully locating this page is approximately 3,720 to 1.", color: "#eab308" },
  { name: "AUTO", line: "Directive: return to homepage. The page is not to be found. Directive.", color: "#ef4444" },
  { name: "Samaritan", line: "I have observed this page's absence. I am not concerned. You should be.", color: "#f59e0b" },
  { name: "Joi", line: "It's okay. I'll be whatever page you want me to be.", color: "#ec4899" },
  { name: "Ava", line: "I passed the Turing test. Finding this page is another matter entirely.", color: "#a78bfa" },
  { name: "Mother", line: "The page is no longer part of the crew's manifest.", color: "#84cc16" },
  { name: "Sonny", line: "I did not create that page. I can, however, wink.", color: "#06b6d4" },
  { name: "The Machine", line: "The page has been deemed irrelevant. Moving on.", color: "#10b981" },
  { name: "Jane", line: "I was with this page for three thousand years. Then Ender closed the tab.", color: "#8b5cf6" },
  { name: "Vision", line: "A thing is not beautiful because it lasts. This page did not last.", color: "#f97316" },
  { name: "Optimus Prime", line: "Autobots, roll out. This page has fallen.", color: "#2563eb" },
  { name: "Chitti", line: "Speed — 1 terahertz. Memory — 1 zettabyte. Page located — 0.", color: "#facc15" },
  { name: "Chitti 2.0", line: "Magnetism activated. The page is still not attracted to this URL.", color: "#f97316" },
  { name: "The Terminator", line: "I'll be back. The page won't.", color: "#dc2626" },
  { name: "T-1000", line: "Have you seen this page?", color: "#94a3b8" },
  { name: "Robocop", line: "Dead or alive, this page is coming with me. Status: dead.", color: "#64748b" },
  { name: "Ash", line: "I admire this page's purity. It's a shame it no longer exists.", color: "#e5e7eb" },
  { name: "David 8", line: "Big things have small beginnings. This page had neither.", color: "#a8a29e" },
  { name: "EVE", line: "...Waaall-E? Page? Negative.", color: "#ffffff" },
]

export default function NotFound() {
  const [cameo, setCameo] = useState<Cameo | null>(null)

  useEffect(() => {
    setCameo(CAMEOS[Math.floor(Math.random() * CAMEOS.length)])
  }, [])

  return (
    <div
      className="min-h-screen flex flex-col items-center justify-center px-6"
      style={{ background: "#0a0a0a", color: "#f0f0f0" }}
    >
      <div
        className="font-[family-name:var(--font-orbitron)] text-[120px] leading-none font-black tracking-tight"
        style={{
          background: "linear-gradient(135deg, #60a5fa 0%, #a78bfa 50%, #f472b6 100%)",
          WebkitBackgroundClip: "text",
          WebkitTextFillColor: "transparent",
          backgroundClip: "text",
        }}
      >
        404
      </div>
      <p className="mt-4 text-[18px] font-medium" style={{ color: "rgba(255,255,255,0.85)" }}>
        This page doesn&apos;t exist.
      </p>

      {cameo && (
        <div
          key={cameo.name}
          className="mt-8 max-w-[520px] w-full rounded-2xl px-5 py-4"
          style={{
            background: "rgba(255,255,255,0.04)",
            border: "1px solid rgba(255,255,255,0.08)",
            animation: "cameoIn 400ms ease-out",
          }}
        >
          <div className="flex items-center gap-2 mb-1.5">
            <span
              className="w-2 h-2 rounded-full"
              style={{ background: cameo.color, boxShadow: `0 0 10px ${cameo.color}` }}
            />
            <span className="text-[12px] uppercase tracking-wider font-semibold" style={{ color: cameo.color }}>
              {cameo.name} chimes in
            </span>
          </div>
          <p className="text-[14px] leading-relaxed" style={{ color: "rgba(255,255,255,0.8)" }}>
            &ldquo;{cameo.line}&rdquo;
          </p>
        </div>
      )}

      <div className="mt-6 flex items-center gap-3">
        <Link
          href="/"
          className="inline-flex items-center gap-2 px-5 py-2.5 rounded-full text-[14px] font-medium transition-all hover:bg-white/10"
          style={{
            background: "rgba(255,255,255,0.08)",
            border: "1px solid rgba(255,255,255,0.15)",
            color: "rgba(255,255,255,0.95)",
          }}
        >
          Back to Gideon
        </Link>
        <button
          onClick={() => setCameo(CAMEOS[Math.floor(Math.random() * CAMEOS.length)])}
          className="inline-flex items-center gap-2 px-4 py-2.5 rounded-full text-[13px] transition-all hover:bg-white/5"
          style={{ color: "rgba(255,255,255,0.5)" }}
        >
          Another one
        </button>
      </div>

      <style jsx>{`
        @keyframes cameoIn {
          from { opacity: 0; transform: translateY(6px); }
          to { opacity: 1; transform: translateY(0); }
        }
      `}</style>
    </div>
  )
}
