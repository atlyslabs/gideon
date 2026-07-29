"use client"

import { motion, AnimatePresence } from "framer-motion"
import { ArrowRight } from "lucide-react"
import { FEATURE_TABS } from "./constants"
import {
  TicketLookupMock,
  DailyBriefingMock,
  SmartDraftsMock,
  SearchFilesMock,
  WorkflowsMock,
  MeetingsMock,
} from "./FeatureMocks"

interface FeatureShowcaseStepProps {
  firstName: string
  activeTab: string
  setActiveTab: (tab: string) => void
  onNext: () => void
}

export default function FeatureShowcaseStep({ firstName, activeTab, setActiveTab, onNext }: FeatureShowcaseStepProps) {
  const renderFeatureContent = () => {
    switch (activeTab) {
      case "ticket-lookup": return <TicketLookupMock />
      case "daily-briefing": return <DailyBriefingMock firstName={firstName} />
      case "smart-drafts": return <SmartDraftsMock />
      case "search-files": return <SearchFilesMock />
      case "workflows": return <WorkflowsMock />
      case "meetings": return <MeetingsMock />
      default: return <TicketLookupMock />
    }
  }

  const activeFeature = FEATURE_TABS.find((t) => t.id === activeTab)

  return (
    <div className="relative px-6 pt-16 pb-20">
      {/* Subtle radial glow */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          background: "radial-gradient(ellipse at 50% 30%, rgba(255,255,255,0.08) 0%, transparent 60%)",
        }}
      />

      <div className="relative max-w-3xl mx-auto">
        {/* Header */}
        <motion.div
          className="text-center mb-10"
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.1 }}
        >
          <motion.img
            src="/logo.png"
            alt="Gideon"
            className="w-14 h-14 rounded-full mx-auto mb-6"
            style={{ boxShadow: "0 0 40px rgba(255,255,255,0.15)" }}
            initial={{ scale: 0.8, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            transition={{ duration: 0.5 }}
          />
          <h1 className="text-3xl font-bold text-white mb-2">
            Welcome to <span style={{ fontFamily: "var(--font-orbitron)", color: "#3b82f6" }}>Gideon</span>{firstName ? `, ${firstName}` : ""}
          </h1>
          <p className="text-white/50 text-base">Your AI work assistant that connects to all your tools</p>
        </motion.div>

        {/* Tab label */}
        <motion.div
          className="text-center mb-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.3 }}
        >
          <span className="text-[10px] uppercase tracking-[0.2em] text-white/40 font-semibold">What you will unlock</span>
        </motion.div>

        {/* Tab pills */}
        <motion.div
          className="grid grid-cols-3 gap-2 mb-8 max-w-md mx-auto"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, delay: 0.35 }}
        >
          {FEATURE_TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-1.5 px-4 py-2 rounded-full text-xs font-medium transition-all duration-200 ${
                activeTab === tab.id
                  ? "bg-white/15 text-white shadow-lg shadow-white/5 border border-white/20"
                  : "bg-white/5 text-white/50 hover:bg-white/10 hover:text-white/70"
              }`}
            >
              {tab.icon}
              {tab.label}
            </button>
          ))}
        </motion.div>

        {/* Feature card */}
        <motion.div
          className="bg-white/5 backdrop-blur-xl rounded-2xl shadow-2xl shadow-black/40 border border-white/10 overflow-hidden"
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.4 }}
        >
          <AnimatePresence mode="wait">
            <motion.div
              key={activeTab}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.25, ease: "easeInOut" }}
            >
              {renderFeatureContent()}
            </motion.div>
          </AnimatePresence>
        </motion.div>

        {/* Feature description */}
        <motion.p
          className="text-center text-white/50 text-sm mt-5"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.5 }}
        >
          {activeFeature?.description}
        </motion.p>

        {/* Next button */}
        <motion.div
          className="flex justify-end mt-8"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.6 }}
        >
          <motion.button
            onClick={onNext}
            className="inline-flex items-center gap-1.5 px-6 py-2.5 rounded-full bg-white/10 text-white text-sm font-medium hover:bg-white/20 transition-all duration-150 border border-white/10"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            Next
            <ArrowRight size={14} />
          </motion.button>
        </motion.div>
      </div>
    </div>
  )
}
