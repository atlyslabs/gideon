import type { Metadata } from "next"

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080"

export async function generateMetadata({ params }: { params: Promise<{ id: string }> }): Promise<Metadata> {
  const { id } = await params

  try {
    const res = await fetch(`${API_BASE}/api/gideon/v1/chat/shared/${id}/meta`, {
      next: { revalidate: 60 },
    })
    if (res.ok) {
      const data = await res.json()
      const title = data.title || "Shared conversation"
      const description = data.owner_name
        ? `Shared by ${data.owner_name} on Gideon`
        : "Shared conversation on Gideon"

      return {
        title: `${title} - Gideon`,
        description,
        openGraph: {
          title: `${title} - Gideon`,
          description,
          images: [{ url: "/og-image.jpg", width: 1200, height: 630 }],
          type: "article",
        },
        twitter: {
          card: "summary_large_image",
          title: `${title} - Gideon`,
          description,
          images: ["/og-image.jpg"],
        },
      }
    }
  } catch {
    // Fallback on fetch failure
  }

  return {
    title: "Gideon - AI Work Assistant",
    description: "Shared conversation on Gideon",
  }
}

export default function ChatLayout({ children }: { children: React.ReactNode }) {
  return <>{children}</>
}
