"use client"

import { X } from "lucide-react"
import type { MentionItem } from "../../types"
import { getIntegrationIcon } from "../../utils/integrationIcons"

interface MentionChipBarProps {
  mentions: MentionItem[]
  onRemove: (index: number) => void
}

export default function MentionChipBar({ mentions, onRemove }: MentionChipBarProps) {
  if (mentions.length === 0) return null

  return (
    <div className="flex flex-wrap gap-1.5 px-4 pt-3 pb-1">
      {mentions.map((mention, idx) => (
        <span
          key={`${mention.id}-${idx}`}
          className="inline-flex items-center gap-1.5 pl-2 pr-1 py-0.5 rounded-full text-xs"
          style={{
            background: "var(--surface-elevated)",
            border: "1px solid var(--border)",
            color: "var(--text-primary)",
          }}
        >
          <span className="shrink-0 flex items-center">
            {getIntegrationIcon(mention.icon || mention.category, 12)}
          </span>
          <span className="truncate max-w-[180px]">{mention.label}</span>
          <button
            onClick={() => onRemove(idx)}
            className="shrink-0 w-4 h-4 flex items-center justify-center rounded-full hover:bg-white/10 transition-colors"
          >
            <X size={10} />
          </button>
        </span>
      ))}
    </div>
  )
}
