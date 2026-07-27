"use client"

/*
 * GraphCanvas — interactive 3D memory graph.
 *
 * Pure 2D canvas with a manual perspective projection (no three.js).
 * Runs at 60fps with ~500 nodes. Contract defined by MemoryGraphPage.
 *
 * Two interaction modes on the same canvas:
 *   HERO  (focusedId === null)  — gentle 3D orbit, camera drift, depth fog
 *   FOCUS (focusedId set)        — snap to 2D radial layout around the node,
 *                                   non-neighbors fade, side panel slides in
 *
 * Center node = highest-degree entity. Recency driven by edges.created_at.
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent as ReactWheelEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react"
import type { MemoryGraph, GraphNode, GraphEdge, NodeKind } from "./MemoryGraphPage"

// Kind → presentation. Colors are rendered with `.gc-legend-dot` class too so
// they stay in sync between canvas rings and the legend.
const TYPE_GLYPHS: Record<NodeKind, string> = {
  person: "●",
  project: "◆",
  company: "■",
  preference: "▲",
  topic: "★",
  other: "○",
}

const TYPE_RING: Record<NodeKind, string> = {
  person: "rgba(255,255,255,0.35)",
  project: "rgba(96,165,250,0.55)",
  company: "rgba(255,255,255,0.22)",
  preference: "rgba(251,191,36,0.45)",
  topic: "rgba(167,139,250,0.5)",
  other: "rgba(255,255,255,0.15)",
}

const TYPE_LEGEND_COLOR: Record<NodeKind, string> = {
  person: "rgba(255,255,255,0.8)",
  project: "rgba(96,165,250,0.9)",
  company: "rgba(200,200,200,0.7)",
  preference: "rgba(251,191,36,0.9)",
  topic: "rgba(167,139,250,0.9)",
  other: "rgba(180,180,180,0.5)",
}

const TYPE_LABEL: Record<NodeKind, string> = {
  person: "People",
  project: "Projects",
  company: "Companies",
  preference: "Preferences",
  topic: "Topics",
  other: "Other",
}

// ---------------------------------------------------------------------------
// Layouts
// ---------------------------------------------------------------------------

type Vec3 = { x: number; y: number; z: number }
type Positions = Record<string, Vec3>

// All layout / simulation tunables live here so tuning is a single-file read.
// Don't inline these values in the math below — prefer a new named constant.
const LAYOUT = {
  // HERO — initial sphere placement
  HERO_INNER_RADIUS: 240,        // minimum orbit distance before jitter
  HERO_RADIUS_JITTER: 80,        // random per-node spread layered on top
  // HERO — force simulation
  SIM_ITERATIONS: 60,            // relaxation passes; more = stabler, slower
  SIM_MIN_DIST_SQ: 100,          // softening on repulsion denominator (prevents blow-up when nodes coincide)
  SIM_REPULSION: 1800,           // inverse-square repulsion strength
  SIM_SPRING_K: 0.02,            // attraction spring stiffness along edges
  SIM_SPRING_REST: 180,          // ideal edge length the spring relaxes to
  SIM_FORCE_CLAMP: 20,           // per-step force cap to stop explosions
  SIM_STEP_DAMPING: 0.5,         // velocity damping each iteration
  // FOCUS — radial layout around focused node
  FOCUS_NEIGHBOR_RADIUS: 220,    // neighbors sit on this circle
  FOCUS_SCATTER_INNER: 500,      // non-neighbors start at this distance
  FOCUS_SCATTER_JITTER: 300,     // + random jitter for scattered nodes
} as const

function fnv1a(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return (h >>> 0) / 4294967295
}

function computeHeroLayout(nodes: GraphNode[], edges: GraphEdge[]): Positions {
  const positions: Positions = {}
  for (const n of nodes) {
    const a = fnv1a(n.id + "a") * Math.PI * 2
    const b = Math.acos(2 * fnv1a(n.id + "b") - 1)
    const r = LAYOUT.HERO_INNER_RADIUS + fnv1a(n.id + "r") * LAYOUT.HERO_RADIUS_JITTER
    positions[n.id] = {
      x: r * Math.sin(b) * Math.cos(a),
      y: r * Math.sin(b) * Math.sin(a),
      z: r * Math.cos(b),
    }
  }
  const adj = new Map<string, string[]>()
  for (const n of nodes) adj.set(n.id, [])
  for (const e of edges) {
    adj.get(e.source)?.push(e.target)
    adj.get(e.target)?.push(e.source)
  }
  for (let it = 0; it < LAYOUT.SIM_ITERATIONS; it++) {
    const fx: Record<string, number> = {}
    const fy: Record<string, number> = {}
    const fz: Record<string, number> = {}
    for (const n of nodes) { fx[n.id] = 0; fy[n.id] = 0; fz[n.id] = 0 }
    // Pairwise inverse-square repulsion — pushes unrelated nodes apart
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j]
        const pa = positions[a.id], pb = positions[b.id]
        const dx = pa.x - pb.x, dy = pa.y - pb.y, dz = pa.z - pb.z
        const d2 = Math.max(LAYOUT.SIM_MIN_DIST_SQ, dx * dx + dy * dy + dz * dz)
        const f = LAYOUT.SIM_REPULSION / d2
        fx[a.id] += dx * f; fy[a.id] += dy * f; fz[a.id] += dz * f
        fx[b.id] -= dx * f; fy[b.id] -= dy * f; fz[b.id] -= dz * f
      }
    }
    // Hooke spring along edges toward the ideal rest length
    for (const e of edges) {
      const pa = positions[e.source], pb = positions[e.target]
      if (!pa || !pb) continue
      const dx = pb.x - pa.x, dy = pb.y - pa.y, dz = pb.z - pa.z
      const d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1
      const f = LAYOUT.SIM_SPRING_K * (d - LAYOUT.SIM_SPRING_REST)
      fx[e.source] += (dx / d) * f; fy[e.source] += (dy / d) * f; fz[e.source] += (dz / d) * f
      fx[e.target] -= (dx / d) * f; fy[e.target] -= (dy / d) * f; fz[e.target] -= (dz / d) * f
    }
    // Integrate with a clamped force + velocity damping
    const C = LAYOUT.SIM_FORCE_CLAMP
    for (const n of nodes) {
      positions[n.id].x += Math.max(-C, Math.min(C, fx[n.id])) * LAYOUT.SIM_STEP_DAMPING
      positions[n.id].y += Math.max(-C, Math.min(C, fy[n.id])) * LAYOUT.SIM_STEP_DAMPING
      positions[n.id].z += Math.max(-C, Math.min(C, fz[n.id])) * LAYOUT.SIM_STEP_DAMPING
    }
  }
  return positions
}

function computeFocusLayout(nodes: GraphNode[], edges: GraphEdge[], focusedId: string): Positions {
  const pos: Positions = {}
  pos[focusedId] = { x: 0, y: 0, z: 0 }
  const neighbors: string[] = []
  for (const e of edges) {
    if (e.source === focusedId) neighbors.push(e.target)
    else if (e.target === focusedId) neighbors.push(e.source)
  }
  const unique = Array.from(new Set(neighbors))
  unique.forEach((id, i) => {
    const a = (i / Math.max(1, unique.length)) * Math.PI * 2 - Math.PI / 2
    pos[id] = { x: Math.cos(a) * LAYOUT.FOCUS_NEIGHBOR_RADIUS, y: Math.sin(a) * LAYOUT.FOCUS_NEIGHBOR_RADIUS, z: 0 }
  })
  for (const n of nodes) {
    if (pos[n.id]) continue
    const a = fnv1a(n.id) * Math.PI * 2
    const r = LAYOUT.FOCUS_SCATTER_INNER + fnv1a(n.id + "r") * LAYOUT.FOCUS_SCATTER_JITTER
    pos[n.id] = { x: Math.cos(a) * r, y: Math.sin(a) * r, z: 0 }
  }
  return pos
}

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------

const IconSearch = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
  </svg>
)
const IconTrash = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="3 6 5 6 21 6" />
    <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
    <path d="M10 11v6" /><path d="M14 11v6" />
    <path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2" />
  </svg>
)
const IconArrowLeft = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="19" y1="12" x2="5" y2="12" /><polyline points="12 19 5 12 12 5" />
  </svg>
)

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

type Camera = {
  yaw: number; pitch: number
  yawTarget: number; pitchTarget: number
  zoom: number; zoomTarget: number
  tx: number; ty: number
  txTarget: number; tyTarget: number
  mode: "hero" | "focus"
  t: number
}

type Projected = { x: number; y: number; z: number; scale: number; fog: number }

const FOV = 700
// Minimum distance behind the focal plane before we clamp. Without this,
// z >= FOV makes `FOV - z` zero or negative, which either explodes `depth`
// to Infinity or flips its sign — projecting the node to a mirror position
// on screen. Clamping means a node at or past the near plane degrades to a
// large-but-finite scale at its (still-correct) screen position.
const NEAR_PLANE_EPS = 10

// Node sizing — shared between render and hit-test so the two stay in sync.
// CENTER_MUL must match the visual size of the "centermost" entity.
const NODE_BASE = 4
const NODE_DEGREE_MUL = 3            // how steeply radius grows with log(degree+1)
const NODE_MIN_RADIUS = 0.5          // render lower bound (never invisible)
const CENTER_SIZE_MUL = 1.9          // center node is larger than others
const HIT_SLOP_PX = 6                // extra pixels on the hit-test radius for forgiveness

function nodeVisualRadius(n: GraphNode, scale: number, isCenter: boolean): number {
  const deg = Math.max(1, n.degree ?? 1)
  const baseR = NODE_BASE + Math.log(deg + 1) * NODE_DEGREE_MUL
  const sizeMul = isCenter ? CENTER_SIZE_MUL : 1
  return Math.max(NODE_MIN_RADIUS, baseR * sizeMul * Math.max(0, scale))
}

// Shared projection used by BOTH the render loop and hit-testing so they
// can't drift. Yaw-then-pitch rotate, perspective divide, center in viewport.
function projectPoint(p: Vec3, cam: Camera, w: number, h: number): Projected {
  const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw)
  const cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch)
  const x = p.x * cy - p.z * sy
  const zR = p.x * sy + p.z * cy
  const y = p.y * cp - zR * sp
  const z = p.y * sp + zR * cp
  const denom = Math.max(NEAR_PLANE_EPS, FOV - z)
  const depth = FOV / denom
  const sx = x * depth * cam.zoom + w / 2 + cam.tx
  const sy2 = y * depth * cam.zoom + h / 2 + cam.ty
  return { x: sx, y: sy2, z, scale: depth, fog: Math.max(0.25, Math.min(1, 1 - (z + 300) / 900)) }
}

type HoverHit = { node: GraphNode; z: number; x: number; y: number }

export default function GraphCanvas({
  graph,
  focusedId,
  onFocus,
  onDeleteFact,
  onDeleteNode,
}: {
  graph: MemoryGraph
  focusedId: string | null
  onFocus: (id: string | null) => void
  onDeleteFact: (edgeId: string) => void
  onDeleteNode: (nodeId: string) => void
}) {
  const rootRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const [size, setSize] = useState({ w: 0, h: 0 })
  const [query, setQuery] = useState("")
  const [hoverNode, setHoverNode] = useState<GraphNode | null>(null)
  const [mousePos, setMousePos] = useState({ x: 0, y: 0 })
  const [dragging, setDragging] = useState(false)
  const [confirmForget, setConfirmForget] = useState(false)
  const [hiddenKinds, setHiddenKinds] = useState<Set<NodeKind>>(new Set())

  // Reset confirm state when focus changes
  useEffect(() => { setConfirmForget(false) }, [focusedId])

  const camRef = useRef<Camera>({
    yaw: 0.0, pitch: -0.25,
    yawTarget: 0.0, pitchTarget: -0.25,
    zoom: 1.0, zoomTarget: 1.0,
    tx: 0, ty: 0,
    txTarget: 0, tyTarget: 0,
    mode: focusedId ? "focus" : "hero",
    t: 0,
  })

  const heroLayout = useMemo(() => {
    const raw = computeHeroLayout(graph.nodes, graph.edges)
    let centerId = graph.nodes[0]?.id
    let best = -1
    for (const n of graph.nodes) {
      if ((n.degree ?? 0) > best) { best = n.degree ?? 0; centerId = n.id }
    }
    const c = centerId ? raw[centerId] : null
    if (c) {
      for (const n of graph.nodes) {
        raw[n.id].x -= c.x; raw[n.id].y -= c.y; raw[n.id].z -= c.z
      }
    }
    return raw
  }, [graph])

  const focusLayout = useMemo(
    () => (focusedId ? computeFocusLayout(graph.nodes, graph.edges, focusedId) : null),
    [graph, focusedId],
  )

  const meta = useMemo(() => {
    if (!graph.nodes.length) return { centerId: null as string | null, hop: {} as Record<string, number>, rec: {} as Record<string, number> }
    let centerId = graph.nodes[0].id
    let best = -1
    for (const n of graph.nodes) {
      if ((n.degree ?? 0) > best) { best = n.degree ?? 0; centerId = n.id }
    }
    const adj = new Map<string, string[]>()
    for (const n of graph.nodes) adj.set(n.id, [])
    for (const e of graph.edges) {
      adj.get(e.source)?.push(e.target)
      adj.get(e.target)?.push(e.source)
    }
    const hop: Record<string, number> = {}
    const q: string[] = [centerId]
    hop[centerId] = 0
    while (q.length) {
      const cur = q.shift()
      if (cur == null) break
      for (const nb of adj.get(cur) || []) {
        if (hop[nb] == null) { hop[nb] = hop[cur] + 1; q.push(nb) }
      }
    }
    for (const n of graph.nodes) if (hop[n.id] == null) hop[n.id] = 5
    let minT = Infinity, maxT = -Infinity
    const nodeT: Record<string, number> = {}
    for (const e of graph.edges) {
      const t = new Date(e.created_at).getTime()
      if (!isFinite(t)) continue
      if (t < minT) minT = t
      if (t > maxT) maxT = t
      if ((nodeT[e.source] ?? -Infinity) < t) nodeT[e.source] = t
      if ((nodeT[e.target] ?? -Infinity) < t) nodeT[e.target] = t
    }
    const span = Math.max(1, maxT - minT)
    const rec: Record<string, number> = {}
    for (const n of graph.nodes) {
      const t = nodeT[n.id]
      rec[n.id] = t == null ? 0 : (t - minT) / span
    }
    return { centerId, hop, rec }
  }, [graph])

  const livePosRef = useRef<Positions>({})
  useEffect(() => {
    if (Object.keys(livePosRef.current).length === 0) {
      for (const n of graph.nodes) {
        const p = heroLayout[n.id]
        if (p) livePosRef.current[n.id] = { ...p }
      }
    }
  }, [graph, heroLayout])

  useEffect(() => {
    const cam = camRef.current
    if (focusedId) {
      cam.mode = "focus"
      cam.zoomTarget = 1.4
      cam.yawTarget = 0
      cam.pitchTarget = 0
      cam.txTarget = -180
      cam.tyTarget = 0
    } else {
      cam.mode = "hero"
      cam.zoomTarget = 1.0
      cam.pitchTarget = -0.25
      cam.txTarget = 0
      cam.tyTarget = 0
    }
  }, [focusedId])

  useLayoutEffect(() => {
    const root = rootRef.current
    if (!root) return
    const measure = () => {
      const r = root.getBoundingClientRect()
      if (r.width > 0 && r.height > 0) setSize({ w: r.width, h: r.height })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(root)
    return () => ro.disconnect()
  }, [])

  const syncCanvas = useCallback(() => {
    const c = canvasRef.current
    const root = rootRef.current
    if (!c || !root) return { w: 0, h: 0 }
    const r = root.getBoundingClientRect()
    const dpr = window.devicePixelRatio || 1
    const w = r.width, h = r.height
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr)
      c.height = Math.round(h * dpr)
      const ctx = c.getContext("2d")
      ctx?.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    return { w, h }
  }, [])

  // Keep current hoverNode in a ref so the rAF draw can read it without reclosing
  const hoverNodeRef = useRef<GraphNode | null>(null)
  useEffect(() => { hoverNodeRef.current = hoverNode }, [hoverNode])

  // Same trick for hiddenKinds — legend toggles should update the render
  // without tearing down / rebuilding the rAF loop.
  const hiddenKindsRef = useRef<Set<NodeKind>>(new Set())
  useEffect(() => { hiddenKindsRef.current = hiddenKinds }, [hiddenKinds])

  useEffect(() => {
    let raf = 0
    const neighborSet = focusedId
      ? new Set(
          graph.edges
            .filter((e) => e.source === focusedId || e.target === focusedId)
            .flatMap((e) => [e.source, e.target]),
        )
      : null

    const draw = () => {
      const { w, h } = syncCanvas()
      const c = canvasRef.current
      if (!c || !w || !h) return
      const ctx = c.getContext("2d")
      if (!ctx) return
      ctx.clearRect(0, 0, w, h)

      const cam = camRef.current
      const live = livePosRef.current
      const filterLower = query.trim().toLowerCase()

      const proj: Record<string, Projected> = {}
      for (const n of graph.nodes) {
        const p = live[n.id]
        if (!p) continue
        proj[n.id] = projectPoint(p, cam, w, h)
      }

      // Edges
      for (const e of graph.edges) {
        const pa = proj[e.source], pb = proj[e.target]
        if (!pa || !pb) continue
        const isFocusEdge = !!focusedId && (e.source === focusedId || e.target === focusedId)
        const sourceVisible = !neighborSet || neighborSet.has(e.source)
        const targetVisible = !neighborSet || neighborSet.has(e.target)
        let alpha: number
        if (focusedId) {
          alpha = isFocusEdge ? 0.8 : (sourceVisible && targetVisible ? 0.15 : 0.03)
        } else {
          alpha = 0.18 * ((pa.fog + pb.fog) / 2)
        }
        ctx.strokeStyle = isFocusEdge
          ? `rgba(96, 165, 250, ${alpha})`
          : `rgba(255, 255, 255, ${alpha})`
        ctx.lineWidth = isFocusEdge ? 1.2 : 0.6
        ctx.beginPath()
        ctx.moveTo(pa.x, pa.y)
        ctx.lineTo(pb.x, pb.y)
        ctx.stroke()
      }

      // Nodes (back-to-front)
      const sorted = graph.nodes.slice().sort((a, b) => (proj[a.id]?.z ?? 0) - (proj[b.id]?.z ?? 0))
      const hover = hoverNodeRef.current
      const hiddenKindsNow = hiddenKindsRef.current
      for (const n of sorted) {
        const p = proj[n.id]
        if (!p) continue
        const deg = Math.max(1, n.degree)
        const isCenter = n.id === meta.centerId
        const r = nodeVisualRadius(n, p.scale, isCenter)

        let visible = true
        if (filterLower) visible = n.name.toLowerCase().includes(filterLower)

        let alpha: number
        if (focusedId && neighborSet) {
          if (n.id === focusedId) alpha = 1
          else if (neighborSet.has(n.id)) alpha = 1
          else alpha = 0.1
        } else {
          alpha = p.fog * (visible ? 1 : 0.2)
        }
        if (!visible && !focusedId) alpha *= 0.35
        // Legend toggle: hide this kind by fading heavily. Still rendered so
        // the layout doesn't jump when toggled and it reads as "filtered
        // out" rather than "gone".
        if (hiddenKindsNow.has(n.kind)) alpha *= 0.12

        const isFocus = n.id === focusedId
        const isHover = hover?.id === n.id
        const hop = Math.min(4, meta.hop[n.id] ?? 4)
        const blueness = 1 - hop / 4
        const rec = meta.rec[n.id] ?? 0

        const glowRadiusMul = isCenter ? 5.5 : (1.6 + blueness * 1.5 + rec * 0.9)
        const glowAlpha =
          isCenter ? 0.55 * alpha
                   : Math.min(0.12, (0.016 + 0.044 * blueness + 0.056 * rec)) * alpha

        if (glowAlpha > 0.02) {
          const grad = ctx.createRadialGradient(p.x, p.y, r * 0.4, p.x, p.y, r * glowRadiusMul)
          const mix = Math.min(1, blueness * 0.7 + rec * 0.5)
          const gr = Math.round(59 + (255 - 59) * (1 - mix))
          const gg = Math.round(130 + (255 - 130) * (1 - mix))
          const gb = Math.round(246 + (255 - 246) * (1 - mix))
          grad.addColorStop(0, `rgba(${gr}, ${gg}, ${gb}, ${glowAlpha})`)
          grad.addColorStop(1, `rgba(${gr}, ${gg}, ${gb}, 0)`)
          ctx.fillStyle = grad
          ctx.beginPath()
          ctx.arc(p.x, p.y, r * glowRadiusMul, 0, Math.PI * 2)
          ctx.fill()
        }

        let baseR: number, baseG: number, baseB: number
        if (isCenter) { baseR = 160; baseG = 200; baseB = 255 }
        else if (isFocus) { baseR = 96; baseG = 165; baseB = 250 }
        else if (isHover) { baseR = 255; baseG = 255; baseB = 255 }
        else {
          const b = blueness
          const blueTone = { r: 80, g: 150, b: 250 }
          const whiteTone = { r: 225, g: 230, b: 240 }
          baseR = Math.round(blueTone.r * b + whiteTone.r * (1 - b))
          baseG = Math.round(blueTone.g * b + whiteTone.g * (1 - b))
          baseB = Math.round(blueTone.b * b + whiteTone.b * (1 - b))
          const lift = rec * 0.45
          baseR = Math.min(255, Math.round(baseR * (1 - lift) + 255 * lift))
          baseG = Math.min(255, Math.round(baseG * (1 - lift) + 255 * lift))
          baseB = Math.min(255, Math.round(baseB * (1 - lift) + 255 * lift))
          const fogMul = 0.55 + p.fog * 0.45
          baseR = Math.round(baseR * fogMul)
          baseG = Math.round(baseG * fogMul)
          baseB = Math.round(baseB * fogMul)
        }
        const hx = p.x - r * 0.35
        const hy = p.y - r * 0.4
        const sphereGrad = ctx.createRadialGradient(hx, hy, r * 0.05, p.x, p.y, r * 1.05)
        const hiR = Math.min(255, baseR + (isCenter ? 90 : 60))
        const hiG = Math.min(255, baseG + (isCenter ? 55 : 60))
        const hiB = Math.min(255, baseB + (isCenter ? 0 : 60))
        const rimMul = isCenter ? 0.75 : 0.35
        const rimBlueBoost = isCenter ? 1.0 : 0.45
        const loR = Math.max(0, Math.round(baseR * rimMul))
        const loG = Math.max(0, Math.round(baseG * rimMul))
        const loB = Math.max(0, Math.round(baseB * rimBlueBoost * 1.1))
        sphereGrad.addColorStop(0, `rgba(${hiR}, ${hiG}, ${hiB}, ${alpha})`)
        sphereGrad.addColorStop(0.45, `rgba(${baseR}, ${baseG}, ${baseB}, ${alpha})`)
        sphereGrad.addColorStop(1, `rgba(${loR}, ${loG}, ${loB}, ${alpha})`)
        ctx.fillStyle = sphereGrad
        ctx.beginPath()
        ctx.arc(p.x, p.y, r, 0, Math.PI * 2)
        ctx.fill()

        if (r > 3) {
          const specGrad = ctx.createRadialGradient(hx, hy, 0, hx, hy, r * (isCenter ? 0.85 : 0.6))
          const specA = isCenter ? 0.9 : 0.55
          specGrad.addColorStop(0, `rgba(255,255,255,${specA * alpha})`)
          specGrad.addColorStop(0.5, `rgba(255,255,255,${(isCenter ? 0.3 : 0.12) * alpha})`)
          specGrad.addColorStop(1, "rgba(255,255,255,0)")
          ctx.fillStyle = specGrad
          ctx.beginPath()
          ctx.arc(hx, hy, r * (isCenter ? 0.85 : 0.6), 0, Math.PI * 2)
          ctx.fill()
        }

        if (!isCenter) {
          ctx.strokeStyle = TYPE_RING[n.kind] ?? TYPE_RING.other
          ctx.lineWidth = 1
          ctx.beginPath()
          ctx.arc(p.x, p.y, r + 2, 0, Math.PI * 2)
          ctx.globalAlpha = alpha
          ctx.stroke()
          ctx.globalAlpha = 1
        }

        const showLabel =
          isCenter || isFocus || isHover ||
          (focusedId && neighborSet && neighborSet.has(n.id)) ||
          (!focusedId && cam.zoom > 1.15 && p.fog > 0.5 && deg >= 5)
        if (showLabel && alpha > 0.3) {
          const weight = isCenter ? 600 : (isFocus ? 600 : 500)
          const fz = isCenter ? 15 : (isFocus ? 13 : 12)
          ctx.font = `${weight} ${fz}px "Geist", system-ui, sans-serif`
          ctx.fillStyle = isCenter
            ? `rgba(200, 220, 255, ${0.95 * alpha})`
            : (isFocus ? "rgba(255,255,255,1)" : `rgba(255,255,255,${0.85 * alpha})`)
          ctx.textAlign = "center"
          ctx.textBaseline = "top"
          ctx.fillText(n.name, p.x, p.y + r + 6)
        }
      }
    }

    const step = () => {
      const cam = camRef.current
      cam.t += 0.004
      const ease = (cur: number, tgt: number, k = 0.08) => cur + (tgt - cur) * k
      cam.yaw = ease(cam.yaw, cam.yawTarget + (cam.mode === "hero" ? Math.sin(cam.t) * 0.35 : 0), 0.06)
      cam.pitch = ease(cam.pitch, cam.pitchTarget + (cam.mode === "hero" ? Math.cos(cam.t * 0.7) * 0.08 : 0), 0.06)
      cam.zoom = ease(cam.zoom, cam.zoomTarget, 0.1)
      cam.tx = ease(cam.tx, cam.txTarget, 0.1)
      cam.ty = ease(cam.ty, cam.tyTarget, 0.1)

      const target = cam.mode === "focus" ? focusLayout : heroLayout
      const live = livePosRef.current
      for (const n of graph.nodes) {
        const t = target?.[n.id]
        if (!t) continue
        if (!live[n.id]) live[n.id] = { x: t.x, y: t.y, z: t.z ?? 0 }
        live[n.id].x = ease(live[n.id].x, t.x, 0.08)
        live[n.id].y = ease(live[n.id].y, t.y, 0.08)
        live[n.id].z = ease(live[n.id].z, t.z ?? 0, 0.08)
      }

      draw()
      raf = requestAnimationFrame(step)
    }

    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [graph, size, heroLayout, focusLayout, focusedId, query, meta, syncCanvas])

  const hitTest = useCallback((mx: number, my: number): HoverHit | null => {
    const cam = camRef.current
    const live = livePosRef.current
    const w = size.w, h = size.h
    if (!w || !h) return null
    let best: HoverHit | null = null
    const hiddenKindsNow = hiddenKindsRef.current
    for (const n of graph.nodes) {
      // Don't pick nodes whose kind is toggled off in the legend — they're
      // visually dimmed and shouldn't capture clicks.
      if (hiddenKindsNow.has(n.kind)) continue
      const p = live[n.id]
      if (!p) continue
      const { x: sx, y: sy2, z, scale } = projectPoint(p, cam, w, h)
      const isCenter = n.id === meta.centerId
      const r = nodeVisualRadius(n, scale, isCenter) + HIT_SLOP_PX
      const dd = (sx - mx) ** 2 + (sy2 - my) ** 2
      if (dd < r * r) {
        // Nodes are drawn back-to-front (ascending z), so the visually
        // frontmost overlapping node has the LARGEST z. Pick that one.
        if (!best || z > best.z) best = { node: n, z, x: sx, y: sy2 }
      }
    }
    return best
  }, [graph, size, meta])

  const pointerRef = useRef({ down: false, mx: 0, my: 0, moved: false })
  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    pointerRef.current = { down: true, mx: e.clientX, my: e.clientY, moved: false }
    setDragging(true)
  }
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const root = rootRef.current
    if (!root) return
    const r = root.getBoundingClientRect()
    const mx = e.clientX - r.left, my = e.clientY - r.top
    setMousePos({ x: mx, y: my })
    if (pointerRef.current.down) {
      const dx = e.clientX - pointerRef.current.mx
      const dy = e.clientY - pointerRef.current.my
      pointerRef.current.mx = e.clientX
      pointerRef.current.my = e.clientY
      if (Math.abs(dx) + Math.abs(dy) > 2) pointerRef.current.moved = true
      const cam = camRef.current
      if (cam.mode === "hero" && !e.shiftKey) {
        cam.yawTarget += dx * 0.005
        cam.pitchTarget = Math.max(-1.2, Math.min(1.2, cam.pitchTarget - dy * 0.005))
      } else {
        cam.txTarget += dx
        cam.tyTarget += dy
      }
      setHoverNode(null)
    } else {
      const hit = hitTest(mx, my)
      setHoverNode(hit ? hit.node : null)
    }
  }
  const onPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const wasMoved = pointerRef.current.moved
    pointerRef.current.down = false
    setDragging(false)
    if (!wasMoved) {
      const root = rootRef.current
      if (!root) return
      const r = root.getBoundingClientRect()
      const mx = e.clientX - r.left, my = e.clientY - r.top
      const hit = hitTest(mx, my)
      if (hit) onFocus(hit.node.id)
      else if (focusedId) onFocus(null)
    }
  }
  const onWheel = (e: ReactWheelEvent<HTMLDivElement>) => {
    const cam = camRef.current
    const delta = -e.deltaY * 0.0015
    cam.zoomTarget = Math.max(0.4, Math.min(3.5, cam.zoomTarget * (1 + delta)))
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && focusedId) onFocus(null)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [focusedId, onFocus])

  const focusedNode = focusedId ? graph.nodes.find((n) => n.id === focusedId) : null
  const focusedEdges = useMemo(() => {
    if (!focusedId) return []
    return graph.edges.filter((e) => e.source === focusedId || e.target === focusedId)
  }, [graph, focusedId])
  const nodeById = useMemo(() => Object.fromEntries(graph.nodes.map((n) => [n.id, n])), [graph])

  const onSearchKey = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      const q = query.trim().toLowerCase()
      if (!q) return
      const match = graph.nodes.find((n) => n.name.toLowerCase().includes(q))
      if (match) onFocus(match.id)
    }
  }

  const focusedType: NodeKind = focusedNode ? (focusedNode.kind ?? "other") : "other"

  if (!graph || !graph.nodes || graph.nodes.length === 0) return null

  return (
    <div
      ref={rootRef}
      className={`gc-root${dragging ? " dragging" : ""}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={() => { setHoverNode(null); setDragging(false); pointerRef.current.down = false }}
      onWheel={onWheel}
    >
      <canvas ref={canvasRef} className="gc-canvas" />

      <div className="gc-search" onPointerDown={(e) => e.stopPropagation()}>
        <IconSearch />
        <input
          placeholder="Search memory"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onSearchKey}
        />
        <kbd>{"↵"}</kbd>
      </div>

      <div
        className="gc-legend"
        onPointerDown={(e) => e.stopPropagation()}
        onPointerUp={(e) => e.stopPropagation()}
      >
        {(["person", "project", "preference", "company", "topic"] as NodeKind[]).map((k) => {
          const isHidden = hiddenKinds.has(k)
          return (
            <button
              key={k}
              type="button"
              className={`gc-legend-item${isHidden ? " hidden" : ""}`}
              onClick={() => {
                setHiddenKinds((prev) => {
                  const next = new Set(prev)
                  if (next.has(k)) next.delete(k)
                  else next.add(k)
                  return next
                })
              }}
              title={isHidden ? `Show ${TYPE_LABEL[k]}` : `Hide ${TYPE_LABEL[k]}`}
            >
              <span className="gc-legend-dot" style={{ background: TYPE_LEGEND_COLOR[k] }} />
              <span className="gc-legend-label">{TYPE_LABEL[k]}</span>
            </button>
          )
        })}
      </div>

      {hoverNode && !focusedId && (
        <div className="gc-tooltip" style={{ left: mousePos.x, top: mousePos.y }}>
          <div style={{ fontWeight: 500 }}>{hoverNode.name}</div>
          {hoverNode.summary && (
            <div style={{ color: "var(--text-secondary)", marginTop: 2, fontSize: 11 }}>
              {hoverNode.summary}
            </div>
          )}
        </div>
      )}

      {focusedId && (
        <button className="gc-back" onPointerDown={(e) => e.stopPropagation()} onClick={() => onFocus(null)}>
          <IconArrowLeft />
          Back to overview
          <span style={{ color: "var(--text-tertiary)", fontFamily: "var(--font-mono)", fontSize: 10, marginLeft: 4 }}>esc</span>
        </button>
      )}

      <aside
        className={`gc-panel${focusedNode ? " open" : ""}`}
        onPointerDown={(e) => e.stopPropagation()}
        onWheel={(e) => e.stopPropagation()}
      >
        {focusedNode && (
          <>
            <div className="gc-panel-head">
              <div className="gc-panel-type">
                <span className="chip" />
                {focusedType} {"·"} {TYPE_GLYPHS[focusedType]}
              </div>
              <h2 className="gc-panel-name">{focusedNode.name}</h2>
              {focusedNode.summary && (
                <p className="gc-panel-summary">{focusedNode.summary}</p>
              )}
            </div>
            <div className="gc-panel-section-label">
              Facts {"·"} {focusedEdges.length}
            </div>
            <div className="gc-facts">
              {focusedEdges.map((e) => {
                const partnerId = e.source === focusedId ? e.target : e.source
                const partner = nodeById[partnerId]
                const date = new Date(e.created_at)
                const dateStr = date.toLocaleDateString(undefined, { month: "short", day: "numeric" })
                return (
                  <div
                    key={e.id}
                    className="gc-fact"
                    onClick={() => partner && onFocus(partner.id)}
                  >
                    <div className="fact-body">
                      <span className="fact-partner">{partner ? partner.name : "—"}</span>
                      <span className="fact-text">{e.fact}</span>
                      <span className="fact-date">{dateStr}</span>
                    </div>
                    <button
                      className="gc-trash"
                      onClick={(ev) => { ev.stopPropagation(); onDeleteFact(e.id) }}
                      aria-label="Delete fact"
                    >
                      <IconTrash />
                    </button>
                  </div>
                )
              })}
              {focusedEdges.length === 0 && (
                <div style={{ padding: 14, color: "var(--text-tertiary)", fontSize: 12 }}>
                  No facts yet.
                </div>
              )}
            </div>
            <div className="gc-panel-forget">
              {confirmForget ? (
                <div className="gc-forget-confirm">
                  <span>Forget {focusedNode.name}?</span>
                  <button
                    className="gc-forget-yes"
                    onClick={() => onDeleteNode(focusedNode.id)}
                  >
                    Yes, forget
                  </button>
                  <button
                    className="gc-forget-cancel"
                    onClick={() => setConfirmForget(false)}
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  className="gc-forget-btn"
                  onClick={() => setConfirmForget(true)}
                >
                  Forget this entirely
                </button>
              )}
            </div>
          </>
        )}
      </aside>
    </div>
  )
}
