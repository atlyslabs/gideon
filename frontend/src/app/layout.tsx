import type { Metadata } from "next"
import { Orbitron, Geist, Geist_Mono } from "next/font/google"
import { Toaster } from "react-hot-toast"
import GoogleOAuthProvider from "./components/GoogleOAuthProvider"
import MaintenanceOverlay from "./components/MaintenanceOverlay"
import "./globals.css"

const orbitron = Orbitron({
  subsets: ["latin"],
  weight: ["700", "900"],
  variable: "--font-orbitron",
})

const geist = Geist({
  subsets: ["latin"],
  variable: "--font-geist",
})

const geistMono = Geist_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
})

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_APP_URL || "http://localhost:3000"),
  title: "Gideon - AI Work Assistant",
  description: "Move faster. Do more. Your AI work assistant that connects to Gmail, Slack, Notion, Linear, and more.",
  robots: {
    index: false,
    follow: false,
    nocache: true,
    googleBot: {
      index: false,
      follow: false,
      noimageindex: true,
    },
  },
  icons: {
    icon: "/icon.png",
  },
  openGraph: {
    title: "Gideon - AI Work Assistant",
    description: "Move faster. Do more. Your AI work assistant that connects to Gmail, Slack, Notion, Linear, and more.",
    images: [{ url: "/og-image.jpg", width: 1200, height: 630 }],
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: "Gideon - AI Work Assistant",
    description: "Move faster. Do more. Your AI work assistant that connects to Gmail, Slack, Notion, Linear, and more.",
    images: ["/og-image.jpg"],
  },
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="en">
      <head>
        <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no" />
        <link rel="preload" href={process.env.NEXT_PUBLIC_HEAD_MODEL_URL || "/orb.obj"} as="fetch" crossOrigin="anonymous" />
      </head>
      <body className={`${geist.variable} ${geistMono.variable} ${orbitron.variable}`}>
        <GoogleOAuthProvider>
          {children}
        </GoogleOAuthProvider>
        <MaintenanceOverlay />
        <Toaster
          position="top-right"
          toastOptions={{
            duration: 4000,
            style: {
              background: "#1c1c1c",
              color: "#f0f0f0",
              border: "1px solid #222222",
            },
            success: {
              iconTheme: {
                primary: "#10b981",
                secondary: "#1c1c1c",
              },
            },
            error: {
              iconTheme: {
                primary: "#ef4444",
                secondary: "#1c1c1c",
              },
            },
          }}
        />
      </body>
    </html>
  )
}
