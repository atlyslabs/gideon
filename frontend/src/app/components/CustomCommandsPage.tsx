"use client"

import { useState, useCallback, useEffect } from "react"
import { Plus, Loader2, X, Trash2, Edit2, Slash } from "lucide-react"

import useCustomCommands, { type CustomCommand, type CustomCommandInput, type CustomCommandUpdate } from "../hooks/useCustomCommands"
import useSlashCommands from "../hooks/useSlashCommands"

const SLUG_RE = /^[a-z][a-z0-9-]{0,63}$/

interface ModalState {
  mode: "create" | "edit"
  // For edit mode, the row being edited.
  command?: CustomCommand
}

function CommandModal({
  state,
  onClose,
  onSave,
}: {
  state: ModalState | null
  onClose: () => void
  onSave: (input: CustomCommandInput | CustomCommandUpdate, id?: string) => Promise<void>
}) {
  const [slug, setSlug] = useState("")
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [paramHint, setParamHint] = useState("")
  const [promptTemplate, setPromptTemplate] = useState("")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const editing = state?.mode === "edit"
  const open = state !== null

  // Hydrate when entering edit mode.
  useEffect(() => {
    if (state?.mode === "edit" && state.command) {
      setSlug(state.command.slug)
      setName(state.command.name)
      setDescription(state.command.description || "")
      setParamHint(state.command.param_hint || "")
      setPromptTemplate(state.command.prompt_template)
    } else if (state?.mode === "create") {
      setSlug("")
      setName("")
      setDescription("")
      setParamHint("")
      setPromptTemplate("")
    }
    setError(null)
  }, [state])

  if (!open) return null

  const slugLooksValid = SLUG_RE.test(slug.trim().toLowerCase())
  const canSubmit = name.trim() && promptTemplate.trim() && (editing || slugLooksValid) && !saving

  const handleSubmit = async () => {
    if (!canSubmit) return
    setSaving(true)
    setError(null)
    try {
      if (editing && state?.command) {
        const update: CustomCommandUpdate = {
          name: name.trim(),
          description: description.trim(),
          param_hint: paramHint.trim(),
          prompt_template: promptTemplate.trim(),
        }
        await onSave(update, state.command.id)
      } else {
        const create: CustomCommandInput = {
          slug: slug.trim().toLowerCase(),
          name: name.trim(),
          description: description.trim() || undefined,
          param_hint: paramHint.trim() || undefined,
          prompt_template: promptTemplate.trim(),
        }
        await onSave(create)
      }
      onClose()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Save failed.")
    } finally {
      setSaving(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center px-4"
      style={{ background: "rgba(0,0,0,0.7)" }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl rounded-xl p-6 max-h-[90vh] overflow-y-auto"
        style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-[18px] font-semibold" style={{ color: "var(--text-primary)" }}>
            {editing ? "Edit Command" : "New Slash Command"}
          </h2>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg transition-colors"
            style={{ color: "var(--text-secondary)" }}
          >
            <X size={18} />
          </button>
        </div>

        <div className="space-y-4">
          {/* Slug — fixed once created, since chat history references it */}
          <div>
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--text-secondary)" }}>
              Slug
            </label>
            <div className="flex items-center gap-2">
              <span
                className="text-[14px] font-mono shrink-0"
                style={{ color: "var(--text-tertiary)" }}
              >
                /
              </span>
              <input
                value={slug}
                onChange={(e) => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))}
                disabled={editing}
                placeholder="my-command"
                className="flex-1 px-3 py-2 rounded-lg text-[14px] font-mono disabled:opacity-50"
                style={{
                  background: "var(--surface-elevated)",
                  border: "1px solid var(--border)",
                  color: "var(--text-primary)",
                }}
              />
            </div>
            <p className="text-[11px] mt-1.5" style={{ color: "var(--text-tertiary)" }}>
              {editing
                ? "Slug can't be changed after creation — chat history references it."
                : "Lowercase letters, digits, and hyphens. Must start with a letter."}
            </p>
          </div>

          <div>
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--text-secondary)" }}>
              Name
            </label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Onboarding Recap"
              className="w-full px-3 py-2 rounded-lg text-[14px]"
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
                color: "var(--text-primary)",
              }}
            />
          </div>

          <div>
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--text-secondary)" }}>
              Description
            </label>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Shown in the slash dropdown — keep it short."
              className="w-full px-3 py-2 rounded-lg text-[14px]"
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
                color: "var(--text-primary)",
              }}
            />
          </div>

          <div>
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--text-secondary)" }}>
              Param hint <span style={{ color: "var(--text-tertiary)" }}>(optional)</span>
            </label>
            <input
              value={paramHint}
              onChange={(e) => setParamHint(e.target.value)}
              placeholder="e.g. uid=... app=..."
              className="w-full px-3 py-2 rounded-lg text-[14px] font-mono"
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
                color: "var(--text-primary)",
              }}
            />
          </div>

          <div>
            <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--text-secondary)" }}>
              Prompt template
            </label>
            <textarea
              value={promptTemplate}
              onChange={(e) => setPromptTemplate(e.target.value)}
              placeholder="Instructions Gideon will follow when this command runs. The user's typed args are appended automatically."
              rows={8}
              className="w-full px-3 py-2 rounded-lg text-[13px] font-mono resize-y"
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
                color: "var(--text-primary)",
                minHeight: 160,
              }}
            />
            <p className="text-[11px] mt-1.5" style={{ color: "var(--text-tertiary)" }}>
              Tip: tell Gideon what data to pull, what to skip, and what shape of answer you want. Skills (Email Tone, Slack Tone) still apply on top.
            </p>
          </div>

          {error && (
            <div
              className="text-[12px] p-2.5 rounded"
              style={{ background: "rgba(239,68,68,0.08)", color: "#fca5a5", border: "1px solid rgba(239,68,68,0.2)" }}
            >
              {error}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <button
              onClick={onClose}
              className="px-4 py-2 rounded-lg text-[13px] font-medium transition-colors"
              style={{ color: "var(--text-secondary)" }}
            >
              Cancel
            </button>
            <button
              onClick={handleSubmit}
              disabled={!canSubmit}
              className="flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-40"
              style={{ background: "#7c3aed", color: "white" }}
            >
              {saving ? <Loader2 size={14} className="animate-spin" /> : null}
              {editing ? "Save" : "Create"}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}


export default function CustomCommandsPage() {
  const { commands, loading, create, update, remove } = useCustomCommands()
  // Manifest is the merged list (built-ins + user). Used here only to render
  // the read-only built-ins section so users can see what's already available.
  const { commands: manifest } = useSlashCommands()
  const [modal, setModal] = useState<ModalState | null>(null)

  const handleSave = useCallback(async (input: CustomCommandInput | CustomCommandUpdate, id?: string) => {
    if (id) {
      await update(id, input as CustomCommandUpdate)
    } else {
      await create(input as CustomCommandInput)
    }
  }, [create, update])

  const handleDelete = useCallback(async (cmd: CustomCommand) => {
    const ok = confirm(`Delete /${cmd.slug}? This cannot be undone.`)
    if (!ok) return
    try {
      await remove(cmd.id)
    } catch (e) {
      alert(e instanceof Error ? e.message : "Delete failed.")
    }
  }, [remove])

  const builtins = manifest.filter((c) => c.is_builtin)
  const userCommandSlugs = new Set(commands.map((c) => c.slug))

  return (
    <div className="max-w-4xl mx-auto px-6 py-8">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-3">
          <Slash size={22} style={{ color: "var(--text-primary)" }} />
          <h1 className="text-[24px] font-semibold" style={{ color: "var(--text-primary)" }}>
            Slash Commands
          </h1>
        </div>
        <button
          onClick={() => setModal({ mode: "create" })}
          className="flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-medium transition-colors"
          style={{ background: "#7c3aed", color: "white" }}
        >
          <Plus size={14} /> New command
        </button>
      </div>
      <p className="text-[13px] mb-8" style={{ color: "var(--text-secondary)" }}>
        Type <code style={{ color: "var(--text-primary)" }}>/</code> in chat to invoke a command. Custom commands you create here are added to the dropdown for your account only.
      </p>

      {/* Built-ins */}
      <section className="mb-10">
        <h2 className="text-[12px] font-medium uppercase tracking-wider mb-3" style={{ color: "var(--text-tertiary)" }}>
          Built-in
        </h2>
        <div className="space-y-2">
          {builtins.length === 0 && (
            <p className="text-[13px]" style={{ color: "var(--text-tertiary)" }}>Loading…</p>
          )}
          {builtins.map((b) => {
            const overridden = userCommandSlugs.has(b.slug)
            return (
              <div
                key={b.slug}
                className="flex items-start gap-3 px-4 py-3 rounded-lg"
                style={{
                  background: "var(--surface-elevated)",
                  border: "1px solid var(--border)",
                  opacity: overridden ? 0.5 : 1,
                }}
              >
                <span
                  className="font-mono text-[12px] mt-[2px] px-1.5 py-0.5 rounded shrink-0"
                  style={{
                    background: "rgba(255,255,255,0.06)",
                    color: "var(--text-primary)",
                    border: "1px solid rgba(255,255,255,0.08)",
                  }}
                >
                  /{b.slug}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="text-[14px]" style={{ color: "var(--text-primary)" }}>
                    {b.name}
                    {overridden && (
                      <span className="ml-2 text-[11px]" style={{ color: "var(--text-tertiary)" }}>
                        (overridden by your custom command)
                      </span>
                    )}
                  </div>
                  <div className="text-[12px] mt-0.5" style={{ color: "var(--text-tertiary)" }}>
                    {b.description}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      </section>

      {/* User-defined */}
      <section>
        <h2 className="text-[12px] font-medium uppercase tracking-wider mb-3" style={{ color: "var(--text-tertiary)" }}>
          Your commands
        </h2>
        {loading ? (
          <p className="text-[13px]" style={{ color: "var(--text-tertiary)" }}>Loading…</p>
        ) : commands.length === 0 ? (
          <div
            className="px-5 py-8 rounded-xl text-center"
            style={{
              background: "var(--surface-elevated)",
              border: "1px dashed var(--border)",
            }}
          >
            <p className="text-[13px]" style={{ color: "var(--text-secondary)" }}>
              No custom commands yet. Click <strong>New command</strong> to create one.
            </p>
            <p className="text-[11px] mt-2" style={{ color: "var(--text-tertiary)" }}>
              Tip: re-using a built-in slug overrides it for your account.
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {commands.map((cmd) => (
              <div
                key={cmd.id}
                className="flex items-start gap-3 px-4 py-3 rounded-lg group"
                style={{
                  background: "var(--surface-elevated)",
                  border: "1px solid var(--border)",
                }}
              >
                <span
                  className="font-mono text-[12px] mt-[2px] px-1.5 py-0.5 rounded shrink-0"
                  style={{
                    background: "rgba(124,58,237,0.12)",
                    color: "#c4b5fd",
                    border: "1px solid rgba(124,58,237,0.25)",
                  }}
                >
                  /{cmd.slug}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="text-[14px]" style={{ color: "var(--text-primary)" }}>
                    {cmd.name}
                    {cmd.overrides_builtin && (
                      <span className="ml-2 text-[11px]" style={{ color: "#a78bfa" }}>
                        (overrides built-in)
                      </span>
                    )}
                  </div>
                  {cmd.description && (
                    <div className="text-[12px] mt-0.5" style={{ color: "var(--text-tertiary)" }}>
                      {cmd.description}
                    </div>
                  )}
                  {cmd.param_hint && (
                    <div className="text-[10px] mt-1 font-mono" style={{ color: "var(--text-tertiary)", opacity: 0.7 }}>
                      {cmd.param_hint}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    onClick={() => setModal({ mode: "edit", command: cmd })}
                    className="p-2 rounded-lg transition-colors"
                    style={{ color: "var(--text-secondary)" }}
                    title="Edit"
                  >
                    <Edit2 size={14} />
                  </button>
                  <button
                    onClick={() => handleDelete(cmd)}
                    className="p-2 rounded-lg transition-colors"
                    style={{ color: "#f87171" }}
                    title="Delete"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <CommandModal state={modal} onClose={() => setModal(null)} onSave={handleSave} />
    </div>
  )
}
