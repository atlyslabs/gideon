"use client"

import { useId } from "react"

/**
 * Animated sparkle that morphs between a 4-point star and a circle.
 * Shown at the bottom of a streaming response to indicate work in progress.
 */
export default function StreamingSparkle({ size = 24 }: { size?: number }) {
  const gradId = `sparkle-gradient-${useId().replace(/:/g, "")}`
  return (
    <div className="py-2">
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        className="streaming-sparkle"
      >
        <path
          d="M12 2L14.5 9.5L22 12L14.5 14.5L12 22L9.5 14.5L2 12L9.5 9.5L12 2Z"
          fill={`url(#${gradId})`}
        />
        <defs>
          <radialGradient id={gradId} cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="rgba(255,255,255,1)" />
            <stop offset="100%" stopColor="rgba(255,255,255,0.75)" />
          </radialGradient>
        </defs>
      </svg>
    </div>
  )
}
