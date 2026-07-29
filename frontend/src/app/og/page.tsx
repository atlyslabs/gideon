"use client"

/**
 * OG Image page - Renders the splash screen (with 3D head) at 1200x630.
 * Visit /og, screenshot, save as /public/og-image.png
 */

import dynamic from "next/dynamic"

const SplashScreen = dynamic(() => import("../components/SplashScreen"), { ssr: false })

export default function OGImagePage() {
  return (
    <div style={{ width: 1200, height: 630, position: "relative", overflow: "hidden" }}>
      <SplashScreen onComplete={() => {}} />
    </div>
  )
}
