/** Convert contentEditable HTML back to markdown (handles ReactMarkdown output) */
export function htmlToMarkdown(el: HTMLElement): string {
  const lines: string[] = []

  function processInline(node: Node): string {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || ""
    if (!(node instanceof HTMLElement)) return ""
    const tag = node.tagName.toLowerCase()
    const inner = Array.from(node.childNodes).map(processInline).join("")
    if (tag === "strong" || tag === "b") return `**${inner}**`
    if (tag === "em" || tag === "i") return `*${inner}*`
    if (tag === "code") return `\`${inner}\``
    if (tag === "a") {
      const href = node.getAttribute("href") || ""
      // Block javascript: and data: URLs
      if (href.match(/^(javascript|data|vbscript):/i)) return inner
      return `[${inner}](${href})`
    }
    if (tag === "br") return "\n"
    return inner
  }

  function processBlock(node: Node) {
    if (node.nodeType === Node.TEXT_NODE) {
      const t = node.textContent?.trim()
      if (t) lines.push(t)
      return
    }
    if (!(node instanceof HTMLElement)) return
    const tag = node.tagName.toLowerCase()
    const inline = Array.from(node.childNodes).map(processInline).join("")

    if (tag === "h1") lines.push(`# ${inline}`, "")
    else if (tag === "h2") lines.push(`## ${inline}`, "")
    else if (tag === "h3") lines.push(`### ${inline}`, "")
    else if (tag === "h4") lines.push(`#### ${inline}`, "")
    else if (tag === "p") lines.push(inline, "")
    else if (tag === "hr") lines.push("---", "")
    else if (tag === "blockquote") lines.push(`> ${inline}`, "")
    else if (tag === "ul") {
      for (const li of node.querySelectorAll(":scope > li")) {
        lines.push(`- ${Array.from(li.childNodes).map(processInline).join("")}`)
      }
      lines.push("")
    } else if (tag === "ol") {
      let i = 1
      for (const li of node.querySelectorAll(":scope > li")) {
        lines.push(`${i++}. ${Array.from(li.childNodes).map(processInline).join("")}`)
      }
      lines.push("")
    } else if (tag === "pre") {
      const code = node.querySelector("code")
      lines.push("```", code?.textContent || node.textContent || "", "```", "")
    } else {
      if (inline.trim()) lines.push(inline, "")
    }
  }

  for (const child of el.childNodes) {
    processBlock(child)
  }

  return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim()
}
