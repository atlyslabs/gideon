/**
 * Admin gate for optional admin-only surfaces (e.g. the usage dashboard).
 *
 * Set NEXT_PUBLIC_ADMIN_EMAILS to a comma-separated list of addresses. When it
 * is unset (the default) no user is treated as an admin and those surfaces stay
 * hidden, which is the right behaviour for a single-user self-hosted instance.
 *
 * This is a UI affordance only. The backend enforces its own authorization, so
 * hiding a control here is never the security boundary.
 */
const ADMIN_EMAILS = (process.env.NEXT_PUBLIC_ADMIN_EMAILS || "")
  .split(",")
  .map((e) => e.trim().toLowerCase())
  .filter(Boolean)

export function isAdminEmail(email?: string | null): boolean {
  if (!email || ADMIN_EMAILS.length === 0) return false
  return ADMIN_EMAILS.includes(email.toLowerCase())
}
