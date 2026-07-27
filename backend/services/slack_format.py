"""
Slack mrkdwn formatting helpers.

Claude emits GitHub-flavored markdown by default (`**bold**`, `### heading`,
`[text](url)`). Slack's `chat.postMessage` renders text with its own `mrkdwn`
flavor — single asterisks for bold, underscores for italic, no headings, no
`[text](url)` syntax. Without translation, those tokens leak through as
literal characters into the Slack message.

This module is the single source of truth for the conversion. Used by:
- `tools/slack_tool.SendSlackMessage` (Claude calls `send_slack_message`)
- `routers/webhooks_router` (trigger agent reply when @Gideon is mentioned)
- `services/workflow_execution_service` (workflow completion DM)
- `services/delegation_service` (delegated todo nudges + acks)

Add new call sites here, not new copies of the regex.
"""

import re

# Triple-asterisk bold-italic must be handled BEFORE plain `**bold**`,
# otherwise the inner `**` of `***word***` is matched first and we end up
# with stray `*` floating around.
_MD_BOLD_ITALIC = re.compile(r"\*\*\*(.+?)\*\*\*", re.DOTALL)
_MD_BOLD_STAR = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_BOLD_UNDERSCORE = re.compile(r"__(.+?)__", re.DOTALL)
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
# Strikethrough: ~~word~~ → ~word~ (Slack uses single tilde).
_MD_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)


def markdown_to_slack_mrkdwn(text: str) -> str:
    """Translate common GitHub-flavored markdown into Slack mrkdwn.

    Handles:
    - `***bold-italic***` → `*_bold-italic_*` (Slack nests bold + italic)
    - `**bold**` and `__bold__` → `*bold*`
    - `~~strike~~` → `~strike~`
    - `[text](url)` → `<url|text>`
    - `# heading` (any level) → `*heading*`

    Leaves single-asterisk italics and inline `code` / fenced ``` blocks
    untouched — Slack already renders those compatibly.
    """
    if not text:
        return text
    # Order matters. Triple-asterisk first so the inner `**` isn't picked up
    # by `_MD_BOLD_STAR` and reduced to malformed output.
    text = _MD_BOLD_ITALIC.sub(r"*_\1_*", text)
    text = _MD_BOLD_STAR.sub(r"*\1*", text)
    text = _MD_BOLD_UNDERSCORE.sub(r"*\1*", text)
    text = _MD_STRIKE.sub(r"~\1~", text)
    text = _MD_LINK.sub(r"<\2|\1>", text)
    text = _MD_HEADING.sub(r"*\2*", text)
    return text
