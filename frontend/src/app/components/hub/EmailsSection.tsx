"use client"

import type { EmailDraft } from "../../types"
import { EmailDraftsTab } from "./EmailDrafts"

interface EmailsSectionProps {
  emailDrafts: EmailDraft[] | undefined
  mutateDrafts: (updater?: (prev?: EmailDraft[]) => EmailDraft[] | undefined, opts?: { revalidate: boolean }) => void
  avatarMap: Record<string, string>
}

export default function EmailsSection({
  emailDrafts,
  mutateDrafts,
  avatarMap,
}: EmailsSectionProps) {
  return (
    <EmailDraftsTab emailDrafts={emailDrafts} mutateDrafts={mutateDrafts} avatarMap={avatarMap} />
  )
}
