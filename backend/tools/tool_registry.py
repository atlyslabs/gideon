"""
Tool Registry — maps tool names to tool instances.
Returns only tools whose integration is connected for a given user.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from db.models import Integration
from tools.base import BaseTool
from tools.gmail_tool import (
    ListEmails, ReadEmail, ReadEmailAttachment, SearchEmails, DraftEmail, UpdateDraft, SendDraft,
    ListDrafts, ReplyToEmail, DeleteDraft,
    ArchiveEmail, TrashEmail, MarkAsRead, ForwardEmail, CreateGmailLabel, AddLabelToEmail,
)
from tools.calendar_tool import (
    ListTodayEvents, ListEvents, GetEvent, CreateEvent, FindFreeTime,
    UpdateEvent, DeleteEvent, RsvpEvent,
    AddAttendeesToEvent, FindGroupFreeTime,
)
from tools.google_drive_tool import (
    ListDriveFiles, SearchDriveFiles, GetDriveFile,
    MoveDriveFile, RenameDriveFile, TrashDriveFile, ShareDriveFile,
)
from tools.google_sheets_tool import (
    ReadSpreadsheet, GetSpreadsheetInfo, UpdateSpreadsheet, CreateSpreadsheet,
    AppendRowsToSpreadsheet, ClearSpreadsheetRange, FindReplaceSpreadsheet,
    SortSpreadsheetRange, CopySpreadsheetSheet, CreateSpreadsheetChart,
)
from tools.google_docs_tool import (
    ReadDocument, CreateDocument, UpdateDocument,
    FindReplaceInDocument, InsertImageIntoDocument,
    InsertTableIntoDocument, InsertPageBreakIntoDocument,
)
from tools.pdf_tool import MarkdownToPdf, HtmlToPdf
from tools.slack_tool import (
    ListChannels, ListChannelMembers, ReadChannel, ReadSlackDM, ReadSlackThread, ReadSlackThreads, SearchSlackMessages, SendSlackMessage,
    ScheduleSlackMessage, CancelScheduledSlackMessage,
    UpdateSlackMessage, DeleteSlackMessage, AddSlackReaction, RemoveSlackReaction,
    PinSlackMessage, UnpinSlackMessage, UploadSlackFile,
)
from tools.slack_canvas_tool import ListSlackCanvases, ReadSlackCanvas, EditSlackCanvas, CreateSlackCanvas
from tools.notion_tool import (
    SearchNotionPages, ReadNotionPage, ReadNotionDatabase, CreateNotionPage,
    CreateNotionDatabaseRow,
    UpdateNotionPage, DuplicateNotionPage, MoveNotionPages,
    CreateNotionDatabase, UpdateNotionDataSource,
    CreateNotionView, UpdateNotionView,
    CreateNotionComment, GetNotionComments,
    GetNotionUsers, GetNotionTeams,
)
from tools.linear_tool import (
    ListLinearIssues, SearchLinearIssues, GetLinearIssue,
    CreateLinearIssue, UpdateLinearIssue, ListLinearTeams,
    CreateLinearComment, ListLinearProjects, GetLinearProject,
    CreateLinearProject, UpdateLinearProject,
    ListLinearLabels, ListLinearWorkflowStates, ListLinearCycles,
    CreateLinearRelation, ArchiveLinearIssue, UnarchiveLinearIssue,
    AttachLinearLink,
)
from tools.github_tool import (
    ListGitHubRepos, ListGitHubIssues, GetGitHubPullRequests,
    GetGitHubPR, SearchGitHubCode, ListGitHubNotifications,
    GetGitHubIssue, GetGitHubFile, ListGitHubBranches, GetGitHubPRDiff,
    ListGitHubIssueComments, ListGitHubPRReviewComments,
    CreateGitHubIssue, UpdateGitHubIssue, AddCommentToGitHubIssue,
    CreateGitHubPR, MergeGitHubPR, RequestPRReview, ReviewGitHubPR,
    AddLabelsToGitHubIssue,
)
from tools.internal_tools import (
    ListTodos, CreateTodo, CompleteTodo, DeleteTodo, DelegateTodo,
    SaveMemory, RecallMemories, ListMemories, DeleteMemory,
    SearchActivity,
)
from tools.upload_tool import ReadUploadedFile
from tools.preview_tool import GeneratePreview
from tools.grafana_tool import (
    ListGrafanaDashboards,
    GetGrafanaDashboard,
    QueryGrafanaPanel,
    ExportGrafanaPanelToSheet,
)
from tools.analytics_tool import ListAnalyticsTables, GetTableSchema, QueryAnalytics
from tools.granola_tool import (
    QueryGranolaMeetings, ListGranolaMeetings, GetGranolaMeetings,
    GetGranolaMeetingTranscript,
)
from tools.ramp_tool import (
    ListRampTransactions, ListRampBills, ListRampReimbursements,
    ListRampUsers, GetRampBalance, ListRampDepartments,
)
from tools.posthog_tool import (
    RunPostHogQuery, SearchPostHogInsights, GetPostHogFeatureFlags,
    CreatePostHogFeatureFlag, GetPostHogErrors, ListPostHogExperiments,
)
# Meta Ads is currently disabled (coming-soon). When re-enabling, also
# uncomment the *META_ADS_TOOL_INSTANCES line in ALL_TOOLS below.
# from tools.meta_ads_tool import META_ADS_TOOL_INSTANCES
from tools.figma_tool import (
    GetFigmaDesignContext, GetFigmaVariables, SearchFigmaDesignSystem,
    GetFigmaScreenshot, GetFigmaMetadata, ListFigmaTeamProjects,
    ListFigmaProjectFiles,
)
# Full Recruitee ATS surface — imported as a module to avoid 37 alias lines and
# name collisions (e.g. ListTags / CreateTask / GetJob) with other providers.
from tools import recruitee_tool as _recruitee
from tools.semrush_tool import (
    SemrushDomainOverview,
    SemrushOrganicKeywords,
    SemrushKeywordOverview,
    SemrushRelatedKeywords,
    SemrushCompetitors,
)
from tools.freshdesk_tool import (
    SearchFreshdeskTickets, GetFreshdeskTicket, ListFreshdeskTickets,
)
from tools.freshchat_tool import (
    SearchFreshchatConversations, GetFreshchatConversation, GetFreshchatUser,
)
from tools.search_console_tool import (
    ListSearchConsoleSites, QuerySearchAnalytics, ListSitemaps, InspectUrl,
)
from tools.workflow_tool import (
    CreateWorkflow, ListWorkflows, ActivateWorkflow, RunWorkflow,
    UpdateWorkflow, DeactivateWorkflow,
)
from tools.custom_command_tool import CreateCustomCommand, UpdateCustomCommand
from tools.exa_tool import (
    WebSearch,
    FindSimilarPages,
    GetPageContent,
    ExaAnswer,
    ExaNewsSearch,
    ExaResearchSearch,
    ExaCompanySearch,
    ExaLinkedInSearch,
    ExaGitHubSearch,
)
from tools.survey_tool import (
    GetOrgBreakdown, DraftSurvey, UpdateSurveyDraft,
    ActivateSurvey, CloseSurvey, AnalyzeSurveyResponses,
)
logger = structlog.get_logger()

# All available tools grouped by provider
ALL_TOOLS: list[BaseTool] = [
    # Google — Gmail
    ListEmails(),
    ReadEmail(),
    ReadEmailAttachment(),
    SearchEmails(),
    DraftEmail(),
    UpdateDraft(),
    SendDraft(),
    ListDrafts(),
    ReplyToEmail(),
    DeleteDraft(),
    ArchiveEmail(),
    TrashEmail(),
    MarkAsRead(),
    ForwardEmail(),
    CreateGmailLabel(),
    AddLabelToEmail(),
    ListTodayEvents(),
    ListEvents(),
    GetEvent(),
    CreateEvent(),
    FindFreeTime(),
    UpdateEvent(),
    DeleteEvent(),
    RsvpEvent(),
    AddAttendeesToEvent(),
    FindGroupFreeTime(),
    # Google — Drive
    ListDriveFiles(),
    SearchDriveFiles(),
    GetDriveFile(),
    MoveDriveFile(),
    RenameDriveFile(),
    TrashDriveFile(),
    ShareDriveFile(),
    # Google — Sheets
    ReadSpreadsheet(),
    GetSpreadsheetInfo(),
    UpdateSpreadsheet(),
    CreateSpreadsheet(),
    AppendRowsToSpreadsheet(),
    ClearSpreadsheetRange(),
    FindReplaceSpreadsheet(),
    SortSpreadsheetRange(),
    CopySpreadsheetSheet(),
    CreateSpreadsheetChart(),
    # Google — Docs
    ReadDocument(),
    CreateDocument(),
    UpdateDocument(),
    FindReplaceInDocument(),
    InsertImageIntoDocument(),
    InsertTableIntoDocument(),
    InsertPageBreakIntoDocument(),
    # PDF generation (markdown / HTML → PDF, saved to Gideon/PDFs/ in Drive)
    MarkdownToPdf(),
    HtmlToPdf(),
    # Slack
    ListChannels(),
    ListChannelMembers(),
    ReadChannel(),
    ReadSlackDM(),
    ReadSlackThread(),
    ReadSlackThreads(),
    SearchSlackMessages(),
    SendSlackMessage(),
    ScheduleSlackMessage(),
    CancelScheduledSlackMessage(),
    UpdateSlackMessage(),
    DeleteSlackMessage(),
    AddSlackReaction(),
    RemoveSlackReaction(),
    PinSlackMessage(),
    UnpinSlackMessage(),
    UploadSlackFile(),
    # Slack — Canvases
    ListSlackCanvases(),
    ReadSlackCanvas(),
    EditSlackCanvas(),
    CreateSlackCanvas(),
    # Notion
    SearchNotionPages(),
    ReadNotionPage(),
    ReadNotionDatabase(),
    CreateNotionPage(),
    CreateNotionDatabaseRow(),
    UpdateNotionPage(),
    DuplicateNotionPage(),
    MoveNotionPages(),
    CreateNotionDatabase(),
    UpdateNotionDataSource(),
    CreateNotionView(),
    UpdateNotionView(),
    CreateNotionComment(),
    GetNotionComments(),
    GetNotionUsers(),
    GetNotionTeams(),
    # Linear
    ListLinearIssues(),
    SearchLinearIssues(),
    GetLinearIssue(),
    CreateLinearIssue(),
    UpdateLinearIssue(),
    ListLinearTeams(),
    CreateLinearComment(),
    ListLinearProjects(),
    GetLinearProject(),
    CreateLinearProject(),
    UpdateLinearProject(),
    ListLinearLabels(),
    ListLinearWorkflowStates(),
    ListLinearCycles(),
    CreateLinearRelation(),
    ArchiveLinearIssue(),
    UnarchiveLinearIssue(),
    AttachLinearLink(),
    # GitHub
    ListGitHubRepos(),
    ListGitHubIssues(),
    GetGitHubPullRequests(),
    GetGitHubPR(),
    SearchGitHubCode(),
    ListGitHubNotifications(),
    GetGitHubIssue(),
    GetGitHubFile(),
    ListGitHubBranches(),
    GetGitHubPRDiff(),
    ListGitHubIssueComments(),
    ListGitHubPRReviewComments(),
    CreateGitHubIssue(),
    UpdateGitHubIssue(),
    AddCommentToGitHubIssue(),
    CreateGitHubPR(),
    MergeGitHubPR(),
    RequestPRReview(),
    ReviewGitHubPR(),
    AddLabelsToGitHubIssue(),
    # Internal (always available, no integration needed)
    ListTodos(),
    CreateTodo(),
    CompleteTodo(),
    DeleteTodo(),
    DelegateTodo(),
    # Memory (always available, no integration needed)
    SaveMemory(),
    RecallMemories(),
    ListMemories(),
    DeleteMemory(),
    # Activity search (always available, no integration needed)
    SearchActivity(),
    # File upload (always available)
    ReadUploadedFile(),
    # Live UI preview (always available — emits a payload the FE compiles).
    GeneratePreview(),
    # Grafana (available if GRAFANA_URL is configured)
    ListGrafanaDashboards(),
    GetGrafanaDashboard(),
    QueryGrafanaPanel(),
    ExportGrafanaPanelToSheet(),
    # ClickHouse analytics (always available)
    ListAnalyticsTables(),
    GetTableSchema(),
    QueryAnalytics(),
    # Granola — meeting notes via MCP
    QueryGranolaMeetings(),
    ListGranolaMeetings(),
    GetGranolaMeetings(),
    GetGranolaMeetingTranscript(),
    # Ramp — expense management via REST API
    ListRampTransactions(),
    ListRampBills(),
    ListRampReimbursements(),
    ListRampUsers(),
    GetRampBalance(),
    ListRampDepartments(),
    # PostHog — analytics via MCP
    RunPostHogQuery(),
    SearchPostHogInsights(),
    GetPostHogFeatureFlags(),
    CreatePostHogFeatureFlag(),
    GetPostHogErrors(),
    ListPostHogExperiments(),
    # Meta Ads — disabled (coming-soon). Graph API direct integration is fully
    # built (see tools/meta_ads_tool.py + services/oauth/meta_ads_oauth.py) but
    # not exposed yet. To re-enable: uncomment the line below, flip
    # comingSoon=false in providerConfig, and add "meta_ads" back to
    # OAUTH_PROVIDERS in providerConfig.ts.
    # *META_ADS_TOOL_INSTANCES,
    # Figma — design via MCP
    GetFigmaDesignContext(),
    GetFigmaVariables(),
    SearchFigmaDesignSystem(),
    GetFigmaScreenshot(),
    GetFigmaMetadata(),
    ListFigmaTeamProjects(),
    ListFigmaProjectFiles(),
    # Workflows (always available)
    CreateWorkflow(),
    ListWorkflows(),
    ActivateWorkflow(),
    RunWorkflow(),
    UpdateWorkflow(),
    DeactivateWorkflow(),
    # Custom slash commands (on-demand /<slug> prompts — always available)
    CreateCustomCommand(),
    UpdateCustomCommand(),
    # Recruitee — ATS (per-user API key). Full recruiting-workflow surface.
    # Reads:
    _recruitee.SearchCandidates(),
    _recruitee.GetCandidate(),
    _recruitee.ListCandidateNotes(),
    _recruitee.ListCandidateMessages(),
    _recruitee.ListCandidateEvaluations(),
    _recruitee.ListJobs(),
    _recruitee.GetJob(),
    _recruitee.ListTalentPools(),
    _recruitee.ListPipelineTemplates(),
    _recruitee.ListPlacements(),
    _recruitee.ListDepartments(),
    _recruitee.ListAdmins(),
    _recruitee.ListTags(),
    _recruitee.ListSources(),
    _recruitee.ListDisqualifyReasons(),
    _recruitee.ListLocations(),
    _recruitee.ListRequisitions(),
    _recruitee.ListTasks(),
    _recruitee.ListInterviewEvents(),
    # Writes:
    _recruitee.CreateCandidate(),
    _recruitee.UpdateCandidate(),
    _recruitee.DeleteCandidate(),
    _recruitee.MoveCandidateStage(),
    _recruitee.DisqualifyCandidate(),
    _recruitee.DeletePlacement(),
    _recruitee.AddCandidateNote(),
    _recruitee.AddCandidateTags(),
    _recruitee.RemoveCandidateTag(),
    _recruitee.CreateTask(),
    _recruitee.CompleteTask(),
    _recruitee.CreateOffer(),
    _recruitee.UpdateOffer(),
    # Semrush — SEO + marketing intel via shared org API key
    SemrushDomainOverview(),
    SemrushOrganicKeywords(),
    SemrushKeywordOverview(),
    SemrushRelatedKeywords(),
    SemrushCompetitors(),
    # Freshdesk — support tickets (shared API key, always available)
    SearchFreshdeskTickets(),
    GetFreshdeskTicket(),
    ListFreshdeskTickets(),
    # Freshchat — customer conversations (shared API key, always available)
    SearchFreshchatConversations(),
    GetFreshchatConversation(),
    GetFreshchatUser(),
    # Web Search (Exa — always available)
    WebSearch(),
    FindSimilarPages(),
    GetPageContent(),
    ExaAnswer(),
    ExaNewsSearch(),
    ExaResearchSearch(),
    ExaCompanySearch(),
    ExaLinkedInSearch(),
    ExaGitHubSearch(),
    # Google — Search Console
    ListSearchConsoleSites(),
    QuerySearchAnalytics(),
    ListSitemaps(),
    InspectUrl(),
    # Survey builder (admin-only — gated in tool_executor.py against SURVEY_ADMINS)
    GetOrgBreakdown(),
    DraftSurvey(),
    UpdateSurveyDraft(),
    ActivateSurvey(),
    CloseSurvey(),
    AnalyzeSurveyResponses(),
]

# Index by name
TOOL_MAP: dict[str, BaseTool] = {t.name: t for t in ALL_TOOLS}


async def get_tools_for_user(
    user_id: UUID, session: AsyncSession
) -> list[BaseTool]:
    """Return all tools — tool_executor handles disconnected integrations
    with reconnect buttons so Claude always attempts the call. Admin-only
    tools (survey builder) are stripped for non-admins so Claude never sees
    schemas it cannot use."""
    from db.models import User
    from routers.surveys_router import is_survey_admin
    from sqlalchemy import select

    has_admin_tools = any(getattr(t, "admin_only", False) for t in ALL_TOOLS)
    if not has_admin_tools:
        return list(ALL_TOOLS)

    email_row = await session.execute(select(User.email).where(User.id == user_id))
    email = email_row.scalar_one_or_none()
    if is_survey_admin(email):
        return list(ALL_TOOLS)
    return [t for t in ALL_TOOLS if not getattr(t, "admin_only", False)]


def get_tool_by_name(name: str) -> BaseTool | None:
    return TOOL_MAP.get(name)
