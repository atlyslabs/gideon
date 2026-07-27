"use client"

import { Search } from "lucide-react"
import { getIntegrationIcon } from "../../utils/integrationIcons"

const GoogleMeetIcon = () => <>{getIntegrationIcon("google_meet", 20)}</>

export function TicketLookupMock() {
  return (
    <div className="p-5 space-y-3 text-[13px]">
      {/* Ticket header */}
      <div className="flex items-center justify-between">
        <div className="text-white/90 font-semibold text-sm">#4821 — Checkout fails on card payment</div>
        <div className="flex gap-1.5">
          <span className="px-2 py-0.5 rounded bg-blue-500/15 text-blue-400 text-[10px] font-medium">OPEN</span>
          <span className="px-2 py-0.5 rounded bg-amber-500/15 text-amber-400 text-[10px] font-medium">HIGH</span>
        </div>
      </div>

      {/* Customer details */}
      <div className="bg-white/[0.03] rounded-lg p-3 space-y-2">
        <div className="text-[10px] uppercase tracking-wider text-white/30 font-semibold">Customer</div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-1.5">
          <div><span className="text-white/30">Name — </span><span className="text-white/60">Jordan Lee</span></div>
          <div><span className="text-white/30">Email — </span><span className="text-white/60">jordan@example.com</span></div>
        </div>
      </div>

      {/* Ticket details */}
      <div className="bg-white/[0.03] rounded-lg p-3 space-y-2">
        <div className="text-[10px] uppercase tracking-wider text-white/30 font-semibold">Details</div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-1.5">
          <div><span className="text-white/30">Status — </span><span className="text-green-400">Open</span></div>
          <div><span className="text-white/30">Priority — </span><span className="text-blue-400">High</span></div>
          <div><span className="text-white/30">Assignee — </span><span className="text-white/60">Unassigned</span></div>
          <div><span className="text-white/30">Group — </span><span className="text-blue-400">Billing</span></div>
          <div><span className="text-white/30">Created — </span><span className="text-white/60">Mar 26, 2026</span></div>
          <div><span className="text-white/30">Updated — </span><span className="text-white/60">Mar 29, 2026</span></div>
          <div><span className="text-white/30">Replies — </span><span className="text-white/60">4</span></div>
          <div><span className="text-white/30">Plan — </span><span className="text-white/60">Pro</span></div>
        </div>
      </div>

      {/* Linked tickets */}
      <div className="bg-white/[0.03] rounded-lg p-3">
        <div className="text-[10px] uppercase tracking-wider text-white/30 font-semibold mb-2">Linked</div>
        <div className="flex items-center gap-2">
          <span className="text-white/25 text-xs font-mono">#4790</span>
          <span className="text-xs text-white/50 truncate">Card declined at checkout (same customer)</span>
          <span className="ml-auto text-[11px] text-white/25 shrink-0">Apr 27</span>
        </div>
      </div>
    </div>
  )
}

export function DailyBriefingMock({ firstName }: { firstName: string }) {
  return (
    <div className="p-6 space-y-5">
      <div className="text-center space-y-1.5">
        <div className="text-2xl text-white/40">Good morning</div>
        <div className="text-lg font-semibold text-white/90">Enjoy your day, {firstName || "there"}.</div>
        <div className="text-sm text-white/50">You have <span className="font-semibold text-white/80">3 Meetings</span> and <span className="font-semibold text-white/80">23 Emails</span></div>
      </div>
      <div className="bg-white/5 rounded-xl p-4 space-y-3">
        <div className="text-xs font-semibold text-white/40 uppercase tracking-wider">Morning Briefing</div>
        <div className="text-sm text-white/60 leading-relaxed">
          Your 10 AM standup has 2 new agenda items from Alex. The quarterly reliability report is due today &mdash;
          Sam shared the draft in #data. 4 tickets were escalated overnight, mostly billing corrections.
        </div>
        <div className="flex gap-2 mt-2">
          <div className="px-2.5 py-1 rounded-md bg-blue-500/15 text-blue-400 text-xs font-medium">View Calendar</div>
          <div className="px-2.5 py-1 rounded-md bg-purple-500/15 text-purple-400 text-xs font-medium">Open Inbox</div>
        </div>
      </div>
    </div>
  )
}

export function SmartDraftsMock() {
  return (
    <div className="p-6 space-y-4">
      <div className="space-y-3">
        <div className="flex items-start gap-3">
          <div className="w-7 h-7 rounded-full bg-white/10 flex items-center justify-center text-xs font-semibold text-white/60 shrink-0 mt-0.5">AI</div>
          <div>
            <div className="text-xs text-white/40 mb-0.5">Priya &middot; 2:14 PM</div>
            <div className="text-sm text-white/70 bg-white/5 rounded-lg rounded-tl-none px-3 py-2">
              Hey, can we get the invoice reports by today? Finance needs them to close the month.
            </div>
          </div>
        </div>
        <div className="flex items-start gap-3 justify-end">
          <div>
            <div className="text-xs text-white/40 mb-0.5 text-right">You &middot; Draft</div>
            <div className="text-sm text-white/80 bg-indigo-500/15 rounded-lg rounded-tr-none px-3 py-2 border border-indigo-500/20">
              Hi Priya, I will need to sync the records first, then share the report with the finance team. Can you confirm the date range you need?
            </div>
            <div className="flex items-center gap-1.5 mt-1.5 justify-end">
              <div className="flex items-center gap-1 px-2 py-0.5 rounded-md bg-blue-500 text-white text-[10px] font-semibold">
                <img src="/logo.png" alt="" className="w-3 h-3 rounded-full" />
                Gideon
              </div>
              <span className="text-[10px] text-white/40">AI-drafted reply</span>
            </div>
          </div>
          <div className="w-7 h-7 rounded-full bg-indigo-500/20 flex items-center justify-center text-xs font-semibold text-indigo-400 shrink-0 mt-0.5">You</div>
        </div>
      </div>
    </div>
  )
}

export function SearchFilesMock() {
  return (
    <div className="p-6 space-y-4">
      <div className="bg-white/5 rounded-xl px-4 py-3 flex items-center gap-3">
        <Search size={16} className="text-white/30" />
        <span className="text-sm text-white/40">What are we shipping next month?</span>
      </div>
      <div className="flex gap-2">
        {[
          { label: "Google Drive", count: 5, color: "text-green-400 bg-green-500/15" },
          { label: "Sheets", count: 8, color: "text-emerald-400 bg-emerald-500/15" },
          { label: "Docs", count: 10, color: "text-blue-400 bg-blue-500/15" },
        ].map((tab) => (
          <div key={tab.label} className={`px-3 py-1 rounded-full text-xs font-medium ${tab.color}`}>
            {tab.label} <span className="opacity-70">{tab.count}</span>
          </div>
        ))}
      </div>
      <div className="space-y-2">
        {[
          { name: "Q2 Shipping Roadmap", type: "Google Sheets", date: "Mar 25", icon: "S" },
          { name: "April Launch Plan - Final", type: "Google Docs", date: "Mar 22", icon: "D" },
          { name: "Product OKRs Q2 2026", type: "Google Docs", date: "Mar 20", icon: "D" },
          { name: "Release Notes Template", type: "Google Drive", date: "Mar 18", icon: "F" },
        ].map((file) => (
          <div key={file.name} className="flex items-center gap-3 py-2 px-3 rounded-lg hover:bg-white/5 transition-colors">
            <div className={`w-8 h-8 rounded-lg flex items-center justify-center text-xs font-bold ${
              file.icon === "S" ? "bg-emerald-500/15 text-emerald-400" :
              file.icon === "D" ? "bg-blue-500/15 text-blue-400" :
              "bg-yellow-500/15 text-yellow-400"
            }`}>{file.icon}</div>
            <div className="flex-1 min-w-0">
              <div className="text-sm text-white/80 font-medium truncate">{file.name}</div>
              <div className="text-xs text-white/40">{file.type}</div>
            </div>
            <div className="text-xs text-white/30">{file.date}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

export function WorkflowsMock() {
  return (
    <div className="p-6 space-y-4">
      <div className="flex items-start gap-3">
        <div className="w-7 h-7 rounded-full bg-indigo-500/20 flex items-center justify-center text-xs font-semibold text-indigo-400 shrink-0">You</div>
        <div className="bg-white/5 rounded-lg rounded-tl-none px-3 py-2 text-sm text-white/70">
          Create a workflow: every time a new Bug Report comes in, send it to Slack #bugs and create a Linear issue.
        </div>
      </div>
      <div className="flex items-start gap-3">
        <div className="w-7 h-7 rounded-full bg-gradient-to-br from-indigo-400 to-blue-500 flex items-center justify-center shrink-0">
          <img src="/logo.png" alt="" className="w-4 h-4 rounded-full" />
        </div>
        <div className="space-y-3 flex-1">
          <div className="text-sm text-white/70">Here is the workflow I have set up:</div>
          <div className="bg-white/5 rounded-xl border border-white/10 overflow-hidden">
            {[
              { step: "Trigger", text: "New Bug Report filed", color: "bg-red-500" },
              { step: "Action 1", text: "Send notification to Slack #bugs", color: "bg-purple-500" },
              { step: "Action 2", text: "Create Linear issue in Bug Triage", color: "bg-indigo-500" },
            ].map((item, i) => (
              <div key={item.step} className={`flex items-center gap-3 px-4 py-3 ${i > 0 ? "border-t border-white/5" : ""}`}>
                <div className={`w-2 h-2 rounded-full ${item.color}`} />
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-white/30">{item.step}</div>
                  <div className="text-sm text-white/70">{item.text}</div>
                </div>
              </div>
            ))}
          </div>
          <div className="flex gap-2">
            <div className="px-3 py-1.5 rounded-lg bg-blue-500 text-white text-xs font-medium">Activate</div>
            <div className="px-3 py-1.5 rounded-lg bg-white/10 text-white/60 text-xs font-medium">Edit</div>
          </div>
        </div>
      </div>
    </div>
  )
}

export function MeetingsMock() {
  return (
    <div className="p-6 space-y-4">
      <div className="bg-gradient-to-r from-teal-500/10 to-cyan-500/10 rounded-xl p-4 border border-white/5">
        <div className="flex items-center justify-between mb-3">
          <div>
            <div className="text-sm font-semibold text-white/90">Code Review</div>
            <div className="text-xs text-white/40">1:00 PM &ndash; 2:30 PM</div>
          </div>
          <div className="flex items-center gap-2">
            <GoogleMeetIcon />
            <span className="text-xs text-teal-400 font-medium">Google Meet</span>
          </div>
        </div>
        <div className="flex -space-x-2">
          {["A", "P", "S", "R"].map((initial, i) => (
            <div key={i} className={`w-7 h-7 rounded-full border-2 border-black flex items-center justify-center text-[10px] font-semibold ${
              ["bg-blue-500/20 text-blue-400", "bg-purple-500/20 text-purple-400", "bg-amber-500/20 text-amber-400", "bg-green-500/20 text-green-400"][i]
            }`}>{initial}</div>
          ))}
          <div className="w-7 h-7 rounded-full border-2 border-black bg-white/10 flex items-center justify-center text-[10px] text-white/50">+2</div>
        </div>
      </div>
      <div className="space-y-3">
        <div>
          <div className="text-xs font-semibold text-white/40 uppercase tracking-wider mb-1">Meeting Summary</div>
          <div className="text-sm text-white/60 leading-relaxed">Reviewed PR #482 for the new CSV export feature. Agreed to add CSV injection protection before merge. Follow-up on API rate limiting discussion next week.</div>
        </div>
        <div>
          <div className="text-xs font-semibold text-white/40 uppercase tracking-wider mb-1">Prep Notes</div>
          <ul className="text-sm text-white/60 space-y-1">
            <li className="flex items-start gap-1.5"><span className="text-indigo-400 mt-0.5">-</span> Review latest commits on feature/csv-export</li>
            <li className="flex items-start gap-1.5"><span className="text-indigo-400 mt-0.5">-</span> Check Sentry for any new issues in staging</li>
          </ul>
        </div>
      </div>
    </div>
  )
}
