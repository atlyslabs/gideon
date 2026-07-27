"use client"

import { useEffect, useState, useCallback } from "react"
import { motion, AnimatePresence } from "framer-motion"

/**
 * CSS-only fallback splash screen.
 * Used if Three.js fails to load or on low-power devices.
 * Dot grid background + SVG wireframe face outline + CSS animations.
 */
export default function SplashFallback({
  onComplete,
}: {
  onComplete: () => void
}) {
  const [visible, setVisible] = useState(true)
  const [phase, setPhase] = useState<"enter" | "display" | "exit">("enter")

  const handleComplete = useCallback(() => {
    onComplete()
  }, [onComplete])

  useEffect(() => {
    const t1 = setTimeout(() => setPhase("display"), 500)
    const t2 = setTimeout(() => setPhase("exit"), 2500)
    const t3 = setTimeout(() => {
      setVisible(false)
      handleComplete()
    }, 3500)

    return () => {
      clearTimeout(t1)
      clearTimeout(t2)
      clearTimeout(t3)
    }
  }, [handleComplete])

  if (!visible) return null

  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          className="fixed inset-0 z-[9999] flex flex-col items-center justify-center overflow-hidden"
          style={{ background: "#000000" }}
          animate={
            phase === "exit"
              ? { scale: 0.3, opacity: 0 }
              : { scale: 1, opacity: 1 }
          }
          transition={{ duration: 1, ease: "easeInOut" }}
        >
          {/* Dot grid background */}
          <div
            className="absolute inset-0"
            style={{
              backgroundImage:
                "radial-gradient(rgba(59, 130, 246, 0.15) 1px, transparent 1px)",
              backgroundSize: "24px 24px",
            }}
          />

          {/* Scanline */}
          <div className="scanline" />

          {/* SVG wireframe face */}
          <motion.svg
            viewBox="0 0 200 260"
            className="w-48 h-60"
            initial={{ opacity: 0, scale: 0.8 }}
            animate={
              phase !== "enter"
                ? { opacity: 1, scale: 1 }
                : { opacity: 0, scale: 0.8 }
            }
            transition={{ duration: 0.8, ease: "easeOut" }}
          >
            {/* Head outline */}
            <ellipse
              cx="100"
              cy="110"
              rx="65"
              ry="85"
              fill="none"
              stroke="#3b82f6"
              strokeWidth="0.8"
              opacity="0.6"
            />
            {/* Inner face contour */}
            <ellipse
              cx="100"
              cy="115"
              rx="50"
              ry="70"
              fill="none"
              stroke="#3b82f6"
              strokeWidth="0.5"
              opacity="0.3"
            />
            {/* Left eye */}
            <ellipse
              cx="78"
              cy="100"
              rx="12"
              ry="6"
              fill="none"
              stroke="#60a5fa"
              strokeWidth="0.8"
              opacity="0.7"
            />
            {/* Right eye */}
            <ellipse
              cx="122"
              cy="100"
              rx="12"
              ry="6"
              fill="none"
              stroke="#60a5fa"
              strokeWidth="0.8"
              opacity="0.7"
            />
            {/* Nose */}
            <path
              d="M100 95 L96 120 L104 120 Z"
              fill="none"
              stroke="#3b82f6"
              strokeWidth="0.6"
              opacity="0.5"
            />
            {/* Mouth */}
            <path
              d="M85 138 Q100 145 115 138"
              fill="none"
              stroke="#3b82f6"
              strokeWidth="0.6"
              opacity="0.5"
            />
            {/* Jaw line */}
            <path
              d="M40 90 Q35 150 100 190 Q165 150 160 90"
              fill="none"
              stroke="#3b82f6"
              strokeWidth="0.5"
              opacity="0.3"
            />
            {/* Cross wireframe lines */}
            {[60, 80, 100, 120, 140].map((y) => (
              <line
                key={`h-${y}`}
                x1="45"
                y1={y}
                x2="155"
                y2={y}
                stroke="#3b82f6"
                strokeWidth="0.2"
                opacity="0.15"
              />
            ))}
            {[60, 80, 100, 120, 140].map((x) => (
              <line
                key={`v-${x}`}
                x1={x}
                y1="30"
                x2={x}
                y2="195"
                stroke="#3b82f6"
                strokeWidth="0.2"
                opacity="0.15"
              />
            ))}
          </motion.svg>

          {/* GIDEON text */}
          <motion.div
            className="mt-8 flex flex-col items-center gap-2"
            initial={{ opacity: 0, y: 10 }}
            animate={
              phase !== "enter"
                ? { opacity: 1, y: 0 }
                : { opacity: 0, y: 10 }
            }
            transition={{ duration: 0.8, delay: 0.3 }}
          >
            <h1
              className="text-4xl font-bold tracking-[0.3em] animate-glow-pulse"
              style={{
                color: "#60a5fa",
                textShadow:
                  "0 0 20px rgba(59, 130, 246, 0.5), 0 0 40px rgba(59, 130, 246, 0.3)",
                fontFamily: "monospace",
              }}
            >
              GIDEON
            </h1>
            <p
              className="text-sm tracking-widest"
              style={{ color: "rgba(59, 130, 246, 0.5)" }}
            >
              Move Faster. Do More.
            </p>
          </motion.div>

          {/* Radial glow */}
          <div
            className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[400px] h-[400px] rounded-full pointer-events-none"
            style={{
              background:
                "radial-gradient(circle, rgba(59, 130, 246, 0.1) 0%, transparent 70%)",
            }}
          />
        </motion.div>
      )}
    </AnimatePresence>
  )
}
