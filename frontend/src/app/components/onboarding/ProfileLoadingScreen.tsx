"use client"

import { useState, useEffect } from "react"
import { motion } from "framer-motion"
import dynamic from "next/dynamic"
import { STAGE_MAP } from "./constants"

const GideonHead = dynamic(() => import("../GideonHead"), { ssr: false })

export default function ProfileLoadingScreen({ stage }: { stage: string | null }) {
  const info = STAGE_MAP[stage || ""] || { progress: 5, label: "Starting up" }
  const [displayProgress, setDisplayProgress] = useState(5)

  useEffect(() => {
    const target = info.progress
    if (target <= displayProgress) return
    const step = Math.max(0.5, (target - displayProgress) / 20)
    const interval = setInterval(() => {
      setDisplayProgress((prev) => {
        if (prev >= target) { clearInterval(interval); return target }
        return Math.min(target, prev + step)
      })
    }, 100)
    return () => clearInterval(interval)
  }, [info.progress])

  return (
    <div className="flex flex-col items-center py-12 gap-6">
      <GideonHead size={120} />
      <div className="text-center">
        <h2 className="text-xl font-bold text-white mb-2">Learning about you...</h2>
        <p className="text-white/40 text-sm max-w-md">This usually takes about a minute. Sit tight.</p>
      </div>

      <style>{`
        @keyframes elec-flicker {
          0%, 100% { opacity: 1; }
          5% { opacity: 0.7; }
          10% { opacity: 1; }
          42% { opacity: 0.95; }
          44% { opacity: 0.6; }
          46% { opacity: 1; }
          78% { opacity: 0.9; }
          80% { opacity: 0.5; }
          82% { opacity: 1; }
        }
        @keyframes elec-arc1 {
          0%, 100% { clip-path: polygon(0% 50%, 12% 20%, 24% 70%, 36% 10%, 48% 60%, 60% 25%, 72% 80%, 84% 15%, 100% 50%, 100% 100%, 0% 100%); }
          25% { clip-path: polygon(0% 50%, 10% 75%, 22% 15%, 34% 65%, 46% 30%, 58% 85%, 70% 20%, 82% 70%, 100% 50%, 100% 100%, 0% 100%); }
          50% { clip-path: polygon(0% 50%, 14% 30%, 26% 80%, 38% 20%, 50% 70%, 62% 15%, 74% 65%, 86% 25%, 100% 50%, 100% 100%, 0% 100%); }
          75% { clip-path: polygon(0% 50%, 11% 60%, 23% 25%, 35% 75%, 47% 15%, 59% 70%, 71% 30%, 83% 80%, 100% 50%, 100% 100%, 0% 100%); }
        }
        @keyframes elec-arc2 {
          0%, 100% { clip-path: polygon(0% 50%, 15% 80%, 25% 20%, 40% 75%, 55% 15%, 65% 70%, 80% 25%, 90% 65%, 100% 50%, 100% 0%, 0% 0%); }
          33% { clip-path: polygon(0% 50%, 12% 25%, 28% 80%, 42% 20%, 52% 65%, 68% 30%, 78% 75%, 88% 20%, 100% 50%, 100% 0%, 0% 0%); }
          66% { clip-path: polygon(0% 50%, 18% 70%, 30% 15%, 38% 65%, 50% 30%, 62% 80%, 75% 20%, 85% 70%, 100% 50%, 100% 0%, 0% 0%); }
        }
        @keyframes spark-pop {
          0% { transform: scale(0); opacity: 1; }
          50% { transform: scale(1.5); opacity: 0.8; }
          100% { transform: scale(0); opacity: 0; }
        }
      `}</style>

      {/* Electric bar */}
      <div className="w-full max-w-sm">
        <div className="relative h-3.5 rounded-full overflow-hidden" style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(59,130,246,0.15)" }}>
          <div className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${displayProgress}%`, background: "linear-gradient(90deg, #0c1929, #132744, #1a3a5c)", animation: "elec-flicker 2s infinite", transition: "width 0.3s linear" }} />
          <div className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${displayProgress}%`, background: "linear-gradient(90deg, #2563eb, #3b82f6, #60a5fa, #3b82f6, #2563eb)", opacity: 0.9, animation: "elec-arc1 0.15s infinite, elec-flicker 2s infinite", transition: "width 0.3s linear" }} />
          <div className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${displayProgress}%`, background: "linear-gradient(90deg, #60a5fa, #93c5fd, #dbeafe, #93c5fd, #60a5fa)", opacity: 0.6, animation: "elec-arc2 0.2s infinite, elec-flicker 1.5s infinite", transition: "width 0.3s linear" }} />
          <div className="absolute left-0 rounded-full" style={{ width: `${displayProgress}%`, top: "25%", height: "50%", background: "linear-gradient(90deg, rgba(219,234,254,0.2), rgba(255,255,255,0.9), rgba(219,234,254,0.2))", filter: "blur(0.5px)", animation: "elec-flicker 0.8s infinite", transition: "width 0.3s linear" }} />
          <div className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${displayProgress}%`, boxShadow: "0 0 8px #3b82f6, 0 0 20px rgba(59,130,246,0.5), inset 0 0 8px rgba(96,165,250,0.3)", animation: "elec-flicker 1.2s infinite", transition: "width 0.3s linear" }} />
          {displayProgress > 3 && [0, 1, 2].map((i) => (
            <div key={i} className="absolute" style={{ left: `${displayProgress}%`, top: `${15 + i * 30}%`, width: 4, height: 4, borderRadius: "50%", background: "#dbeafe", boxShadow: "0 0 6px #60a5fa, 0 0 12px #3b82f6", animation: `spark-pop ${0.3 + i * 0.15}s infinite ${i * 0.1}s` }} />
          ))}
        </div>
        <p className="text-[11px] text-white/30 text-right mt-1.5">{Math.round(displayProgress)}%</p>
      </div>

      {/* Stage label */}
      <motion.p
        key={info.label}
        className="text-sm text-white/50"
        initial={{ opacity: 0, y: 5 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3 }}
      >
        {info.label}{info.progress < 100 ? "..." : ""}
      </motion.p>
    </div>
  )
}
