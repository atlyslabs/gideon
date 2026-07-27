"use client"

import { motion } from "framer-motion"
import { ArrowRight, Sparkles } from "lucide-react"
import ReactMarkdown from "react-markdown"
import { getIntegrationIcon } from "../../utils/integrationIcons"
import ProfileLoadingScreen from "./ProfileLoadingScreen"

interface ProfileReviewStepProps {
  profileLoading: boolean
  onboardingStage: string | null
  backgroundInfo: string
  learnedSkills: { id: string; name: string; description: string; memory_content: string | null }[]
  profileRef: React.RefObject<HTMLDivElement | null>
  skillRefs: React.MutableRefObject<Record<string, HTMLDivElement | null>>
  onNext: () => void
}

export default function ProfileReviewStep({
  profileLoading,
  onboardingStage,
  backgroundInfo,
  learnedSkills,
  profileRef,
  skillRefs,
  onNext,
}: ProfileReviewStepProps) {
  return (
    <div className="relative px-6 pt-12 pb-20 flex-1 flex items-start justify-center">
      <div className="max-w-5xl mx-auto w-full">
        <motion.div
          className="rounded-2xl p-8 md:p-12"
          style={{
            background: "linear-gradient(135deg, rgba(59,130,246,0.12) 0%, rgba(59,130,246,0.04) 100%)",
            border: "1px solid rgba(59,130,246,0.2)",
            backdropFilter: "blur(24px)",
            boxShadow: "0 0 60px rgba(59,130,246,0.08), inset 0 1px 0 rgba(255,255,255,0.05)",
          }}
          initial={{ opacity: 0, y: 30 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
        >
          {profileLoading ? (
            <>
              <ProfileLoadingScreen stage={onboardingStage} />
              <div className="mt-6 flex justify-end">
                <button
                  disabled
                  className="inline-flex items-center gap-1.5 px-6 py-2.5 rounded-full text-sm font-medium bg-white/5 text-white/30 border border-white/5 cursor-not-allowed"
                >
                  Next
                  <ArrowRight size={14} />
                </button>
              </div>
            </>
          ) : (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.5 }}
            >
              <div className="text-center mb-8">
                <h2 className="text-2xl font-bold text-white mb-2">
                  Here&apos;s what we learned
                </h2>
                <p className="text-white/40 text-sm">
                  Review and edit — this helps Gideon personalize everything for you.
                </p>
              </div>

              {/* Background info */}
              {backgroundInfo && (
                <div className="mb-8">
                  <label className="block text-xs font-semibold uppercase tracking-wider text-white/50 mb-2">
                    Your Profile
                  </label>
                  <div
                    ref={profileRef}
                    contentEditable
                    suppressContentEditableWarning
                    className="px-4 py-3 rounded-xl text-sm leading-relaxed prose prose-invert prose-sm max-w-none cursor-text transition-colors focus:outline-none focus:ring-1 focus:ring-blue-500/30"
                    style={{
                      background: "rgba(255,255,255,0.05)",
                      border: "1px solid rgba(255,255,255,0.08)",
                      maxHeight: 300,
                      overflowY: "auto",
                    }}
                  >
                    <ReactMarkdown>{backgroundInfo}</ReactMarkdown>
                  </div>
                </div>
              )}

              {/* Skills learned */}
              {learnedSkills.length > 0 && (
                <div className="mb-8">
                  <label className="block text-xs font-semibold uppercase tracking-wider text-white/50 mb-3">
                    Communication Style
                  </label>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {learnedSkills.map((skill) => (
                      <div
                        key={skill.id}
                        className="rounded-xl flex flex-col"
                        style={{
                          background: "rgba(255,255,255,0.05)",
                          border: "1px solid rgba(255,255,255,0.08)",
                        }}
                      >
                        <div className="flex items-center gap-2 px-4 pt-3 pb-2">
                          {skill.name.toLowerCase().includes("slack") ? (
                            <span className="shrink-0">{getIntegrationIcon("slack", 16)}</span>
                          ) : skill.name.toLowerCase().includes("email") ? (
                            <span className="shrink-0">{getIntegrationIcon("gmail", 16)}</span>
                          ) : (
                            <Sparkles size={14} className="text-blue-400" />
                          )}
                          <span className="text-sm font-medium text-white">{skill.name}</span>
                        </div>
                        <div
                          ref={(el) => { skillRefs.current[skill.id] = el }}
                          contentEditable
                          suppressContentEditableWarning
                          className="flex-1 px-4 py-2 pb-3 text-xs leading-relaxed overflow-y-auto prose prose-invert prose-sm max-w-none rounded-b-xl cursor-text focus:outline-none focus:ring-1 focus:ring-blue-500/30"
                          style={{ maxHeight: 280, color: "rgba(255,255,255,0.5)" }}
                        >
                          <ReactMarkdown>{skill.memory_content || "*Still learning...*"}</ReactMarkdown>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* No data fallback */}
              {!backgroundInfo && learnedSkills.length === 0 && (
                <div className="text-center py-8">
                  <p className="text-white/40 text-sm">
                    We couldn&apos;t gather enough data yet — don&apos;t worry, Gideon will learn as you use it.
                  </p>
                </div>
              )}

              <div className="mt-6 flex justify-end">
                <motion.button
                  onClick={onNext}
                  className="inline-flex items-center gap-1.5 px-6 py-2.5 rounded-full text-sm font-medium bg-white/10 text-white hover:bg-white/20 border border-white/10 transition-all duration-150"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  Next
                  <ArrowRight size={14} />
                </motion.button>
              </div>
            </motion.div>
          )}
        </motion.div>
      </div>
    </div>
  )
}
