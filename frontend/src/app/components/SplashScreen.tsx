"use client"

import { useEffect, useRef, useState, useMemo, useCallback, Suspense } from "react"
import { Canvas, useFrame, useLoader, extend } from "@react-three/fiber"
import { motion, AnimatePresence } from "framer-motion"
import * as THREE from "three"
import { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js"

// 3D model rendered as the holographic particle cloud. Defaults to a
// procedurally generated orb that ships with the project. Point
// NEXT_PUBLIC_HEAD_MODEL_URL at your own .obj to use a different model
// (only vertex positions are used; the mesh is re-centred and re-scaled).
const HEAD_MODEL_URL = process.env.NEXT_PUBLIC_HEAD_MODEL_URL || "/orb.obj"


// Preload OBJ at module level so Three.js cache is warm before Suspense triggers
useLoader.preload(OBJLoader, HEAD_MODEL_URL)

// ---------------------------------------------------------------------------
// Fresnel hologram shader — glassy translucent look
// Edges glow bright, flat-on surfaces are nearly transparent
// ---------------------------------------------------------------------------
const hologramVertexShader = `
  varying vec3 vNormal;
  varying vec3 vViewDir;
  varying vec3 vWorldPos;

  void main() {
    vec4 worldPos = modelMatrix * vec4(position, 1.0);
    vWorldPos = worldPos.xyz;
    vNormal = normalize(normalMatrix * normal);
    vViewDir = normalize(cameraPosition - worldPos.xyz);
    gl_Position = projectionMatrix * viewMatrix * worldPos;
  }
`

const hologramFragmentShader = `
  uniform float uOpacity;
  uniform float uTime;
  uniform vec3 uColor;
  uniform vec3 uEdgeColor;

  varying vec3 vNormal;
  varying vec3 vViewDir;
  varying vec3 vWorldPos;

  void main() {
    // Fresnel: bright at edges (geometry contours), transparent on flat
    float fresnel = 1.0 - abs(dot(vNormal, vViewDir));
    fresnel = pow(fresnel, 1.2);  // Lower power = more visible features

    // Curvature approximation: where normals change fast = facial features
    // Use the normal's deviation from straight-on as feature indicator
    float facing = abs(dot(vNormal, vViewDir));
    float featureGlow = smoothstep(0.2, 0.6, fresnel) * 0.4;

    // Subtle scanline effect
    float scanline = sin(vWorldPos.y * 50.0 + uTime * 1.5) * 0.5 + 0.5;
    scanline = smoothstep(0.4, 0.6, scanline) * 0.08;

    // Mix: faint base fill + bright edges + feature glow
    vec3 color = mix(uColor * 0.4, uEdgeColor, fresnel * 0.8 + featureGlow);

    // More visible overall, strong edges
    float alpha = (0.08 + fresnel * 0.7 + featureGlow + scanline) * uOpacity;

    gl_FragColor = vec4(color, alpha);
  }
`

// ---------------------------------------------------------------------------
// OBJ-based holographic head with Fresnel shader
// ---------------------------------------------------------------------------
function HolographicHead({ progress }: { progress: number }) {
  const obj = useLoader(OBJLoader, HEAD_MODEL_URL)
  const turnRef = useRef<THREE.Group>(null)  // outer: head turn (Y axis)
  const groupRef = useRef<THREE.Group>(null)  // inner: base orientation
  const meshRef = useRef<THREE.Mesh>(null)
  const wireRef = useRef<THREE.Mesh>(null)
  const pointsRef = useRef<THREE.Points>(null)

  // Track when materialization completed so breathing timer starts from that moment
  const breathStartRef = useRef<number | null>(null)

  const { geometry, sampledVerts, scatterPositions, currentPositions, driftOffsets } =
    useMemo(() => {
      let geo: THREE.BufferGeometry | null = null
      obj.traverse((child) => {
        if (child instanceof THREE.Mesh && !geo) {
          geo = child.geometry.clone()
        }
      })

      if (!geo) geo = new THREE.SphereGeometry(1, 16, 16)

      // Center
      geo.computeBoundingBox()
      const box = geo.boundingBox!
      const center = new THREE.Vector3()
      box.getCenter(center)
      geo.translate(-center.x, -center.y, -center.z)

      // Scale to ~3.5 units
      const size = new THREE.Vector3()
      box.getSize(size)
      const maxDim = Math.max(size.x, size.y, size.z)
      const scale = 3.5 / maxDim
      geo.scale(scale, scale, scale)

      // Compute normals for Fresnel shading
      geo.computeVertexNormals()

      // Sample vertices for particles
      const posAttr = geo.getAttribute("position")
      const totalVerts = posAttr.count
      const sampleCount = 6000
      const step = Math.max(1, Math.floor(totalVerts / sampleCount))
      const sampled = new Float32Array(sampleCount * 3)
      let idx = 0
      for (let i = 0; i < totalVerts && idx < sampleCount; i += step) {
        sampled[idx * 3] = posAttr.getX(i)
        sampled[idx * 3 + 1] = posAttr.getY(i)
        sampled[idx * 3 + 2] = posAttr.getZ(i)
        idx++
      }
      const finalSampled = sampled.slice(0, idx * 3)

      // Scatter across entire viewport
      const scatter = new Float32Array(finalSampled.length)
      for (let i = 0; i < idx; i++) {
        scatter[i * 3] = (Math.random() - 0.5) * 20
        scatter[i * 3 + 1] = (Math.random() - 0.5) * 14
        scatter[i * 3 + 2] = (Math.random() - 0.5) * 10
      }

      // Drift offsets for breathing cycle (same as GideonHead)
      const drift = new Float32Array(idx * 4)
      for (let i = 0; i < idx; i++) {
        const angle = Math.random() * Math.PI * 2
        const elevation = (Math.random() - 0.5) * Math.PI
        const radius = 1.0 + Math.random() * 2.0
        drift[i * 4] = Math.cos(angle) * Math.cos(elevation) * radius
        drift[i * 4 + 1] = Math.sin(elevation) * radius
        drift[i * 4 + 2] = Math.sin(angle) * Math.cos(elevation) * radius
        drift[i * 4 + 3] = Math.random() * Math.PI * 2
      }

      return {
        geometry: geo,
        sampledVerts: finalSampled,
        scatterPositions: scatter,
        currentPositions: new Float32Array(finalSampled.length),
        driftOffsets: drift,
      }
    }, [obj])

  // Hologram shader material
  const shaderMaterial = useMemo(
    () =>
      new THREE.ShaderMaterial({
        vertexShader: hologramVertexShader,
        fragmentShader: hologramFragmentShader,
        uniforms: {
          uOpacity: { value: 0 },
          uTime: { value: 0 },
          uColor: { value: new THREE.Color("#3b82f6") },
          uEdgeColor: { value: new THREE.Color("#93c5fd") },
        },
        transparent: true,
        side: THREE.DoubleSide,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      }),
    []
  )

  useFrame(({ clock }) => {
    const t = clock.getElapsedTime()
    const particleCount = sampledVerts.length / 3

    // Update shader uniforms
    shaderMaterial.uniforms.uTime.value = t
    const surfaceRaw = Math.max(0, (progress - 0.85) * 6.67)
    const surfaceOpacity = Math.min(surfaceRaw, 1)
    shaderMaterial.uniforms.uOpacity.value = surfaceOpacity

    if (progress < 1) {
      // ── Phase 1: Materialization — dots converge from scatter to surface ──
      breathStartRef.current = null
      const morphT = Math.min(progress, 1)
      for (let i = 0; i < sampledVerts.length; i++) {
        currentPositions[i] =
          scatterPositions[i] + (sampledVerts[i] - scatterPositions[i]) * morphT
      }

      if (pointsRef.current) {
        const mat = pointsRef.current.material as THREE.PointsMaterial
        const dotFade = surfaceOpacity < 0.5 ? 0.85 : Math.max(0.1, 0.85 - (surfaceOpacity - 0.5) * 1.5)
        mat.opacity = dotFade
      }
    } else {
      // ── Phase 2: Breathing cycle — dots leave head, swirl, settle back ──
      if (breathStartRef.current === null) breathStartRef.current = t
      const bt = t - breathStartRef.current
      const CYCLE = 8
      const cycleT = (bt % CYCLE) / CYCLE

      let morph: number
      if (cycleT < 0.25) {
        morph = 0
      } else if (cycleT < 0.55) {
        const p = (cycleT - 0.25) / 0.3
        morph = p * p * (3 - 2 * p)
      } else if (cycleT < 0.625) {
        morph = 1
      } else {
        const p = (cycleT - 0.625) / 0.375
        morph = 1 - p * p * (3 - 2 * p)
      }

      for (let i = 0; i < particleCount; i++) {
        const sx = sampledVerts[i * 3]
        const sy = sampledVerts[i * 3 + 1]
        const sz = sampledVerts[i * 3 + 2]
        const dx = driftOffsets[i * 4]
        const dy = driftOffsets[i * 4 + 1]
        const dz = driftOffsets[i * 4 + 2]
        const phase = driftOffsets[i * 4 + 3]

        const spiral = morph * 0.4
        const swirlAngle = t * 0.8 + phase
        const swirlX = Math.cos(swirlAngle) * spiral
        const swirlY = Math.sin(swirlAngle * 0.7) * spiral * 0.5
        const swirlZ = Math.sin(swirlAngle) * spiral

        currentPositions[i * 3] = sx + (dx + swirlX) * morph
        currentPositions[i * 3 + 1] = sy + (dy + swirlY) * morph
        currentPositions[i * 3 + 2] = sz + (dz + swirlZ) * morph
      }

      if (pointsRef.current) {
        // Dots visible when drifting, fade out when settled on surface
        const mat = pointsRef.current.material as THREE.PointsMaterial
        mat.opacity = morph * 0.85
      }
    }

    if (pointsRef.current) {
      const geom = pointsRef.current.geometry
      geom.attributes.position.array.set(currentPositions)
      geom.attributes.position.needsUpdate = true
    }

    // Head swing
    if (turnRef.current) {
      turnRef.current.rotation.y = Math.sin(t * 0.4) * (Math.PI / 3)
    }
  })

  const wireframeOpacity = Math.max(0, (progress - 0.4) * 1.2)
  const pointsOpacity = Math.min(progress * 3, 0.7)

  return (
    <group ref={turnRef}>
      <group ref={groupRef} rotation={[Math.PI / 2, Math.PI, 0]}>
        {/* Holographic Fresnel surface */}
        <mesh ref={meshRef} geometry={geometry} material={shaderMaterial} />

        {/* Wireframe removed — dots define the head shape */}

        {/* Particles */}
        <points ref={pointsRef}>
          <bufferGeometry>
            <bufferAttribute
              attach="attributes-position"
              args={[new Float32Array(sampledVerts.length), 3]}
            />
          </bufferGeometry>
          <pointsMaterial
            color="#93c5fd"
            size={0.025}
            transparent
            opacity={0.85}
            sizeAttenuation
            depthWrite={false}
            blending={THREE.AdditiveBlending}
          />
        </points>
      </group>
    </group>
  )
}

// ---------------------------------------------------------------------------
// Scene
// ---------------------------------------------------------------------------
function SplashScene({ progress }: { progress: number }) {
  return (
    <Canvas
      camera={{ position: [0, 0.3, 9], fov: 38 }}
      gl={{ alpha: true, antialias: true }}
      style={{ background: "transparent" }}
    >
      <Suspense fallback={null}>
        <HolographicHead progress={progress} />
      </Suspense>
    </Canvas>
  )
}

// ---------------------------------------------------------------------------
// Main splash screen component
// ---------------------------------------------------------------------------
export default function SplashScreen({
  onComplete,
}: {
  onComplete: () => void
}) {
  const [progress, setProgress] = useState(0)
  const [phase, setPhase] = useState<"materialize" | "display" | "zoomout">(
    "materialize"
  )
  const [visible, setVisible] = useState(true)
  const startTime = useRef(Date.now())

  const handleComplete = useCallback(() => {
    onComplete()
  }, [onComplete])

  const phaseRef = useRef(phase)
  phaseRef.current = phase

  const autoCloseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const rafIdRef = useRef<number | null>(null)

  const dismissSplash = useCallback(() => {
    if (phaseRef.current === "zoomout") return
    setPhase("zoomout")
    // Cancel any pending animation/auto-close
    if (autoCloseTimerRef.current) { clearTimeout(autoCloseTimerRef.current); autoCloseTimerRef.current = null }
    if (rafIdRef.current) { cancelAnimationFrame(rafIdRef.current); rafIdRef.current = null }
    setTimeout(() => {
      setVisible(false)
      handleComplete()
    }, 1000)
  }, [handleComplete])

  // Materialization animation, then auto-close after 3s display
  useEffect(() => {
    const animate = () => {
      if (phaseRef.current === "zoomout") return  // bail if dismissed
      const elapsed = (Date.now() - startTime.current) / 1000

      if (elapsed < 1.54) {
        setPhase("materialize")
        setProgress(elapsed / 1.54)
        rafIdRef.current = requestAnimationFrame(animate)
      } else {
        setPhase("display")
        setProgress(1)
        autoCloseTimerRef.current = setTimeout(dismissSplash, 3000)
      }
    }

    rafIdRef.current = requestAnimationFrame(animate)

    return () => {
      if (autoCloseTimerRef.current) clearTimeout(autoCloseTimerRef.current)
      if (rafIdRef.current) cancelAnimationFrame(rafIdRef.current)
    }
  }, [dismissSplash])

  if (!visible) return null

  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          className="fixed inset-0 z-[9999] flex flex-col items-center justify-center cursor-pointer"
          style={{ background: "#000000" }}
          onClick={dismissSplash}
          animate={
            phase === "zoomout"
              ? { scale: 0.3, opacity: 0 }
              : { scale: 1, opacity: 1 }
          }
          transition={
            phase === "zoomout"
              ? { duration: 1, ease: "easeInOut" }
              : { duration: 0.3 }
          }
        >
          {/* Dot grid background — full page, behind everything */}
          <div
            className="absolute inset-0 pointer-events-none"
            style={{
              backgroundImage:
                "radial-gradient(rgba(59, 130, 246, 0.15) 1px, transparent 1px)",
              backgroundSize: "24px 24px",
              zIndex: 0,
            }}
          />

          {/* Scanline effect */}
          <div className="scanline" style={{ zIndex: 1 }} />

          {/* Three.js canvas — full page */}
          <div className="absolute inset-0" style={{ zIndex: 2 }}>
            <SplashScene progress={progress} />
          </div>

          {/* GIDEON text */}
          <motion.div
            className="absolute bottom-[12%] flex flex-col items-center gap-2"
            style={{ zIndex: 3 }}
            initial={{ opacity: 0, y: 10 }}
            animate={
              progress > 0.6
                ? { opacity: 1, y: 0 }
                : { opacity: 0, y: 10 }
            }
            transition={{ duration: 0.8, ease: "easeOut" }}
          >
            <h1
              className="text-4xl font-bold tracking-[0.3em] animate-glow-pulse"
              style={{
                color: "#60a5fa",
                textShadow:
                  "0 0 20px rgba(59, 130, 246, 0.5), 0 0 40px rgba(59, 130, 246, 0.3), 0 0 80px rgba(59, 130, 246, 0.15)",
                fontFamily: "var(--font-orbitron)",
              }}
            >
              GIDEON
            </h1>
            <p
              className="text-sm tracking-widest"
              style={{ color: "rgba(59, 130, 246, 0.5)" }}
            >
              Move Faster. Do More.
            </p>
          </motion.div>

          {/* Radial glow behind face */}
          <div
            className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[500px] h-[500px] rounded-full pointer-events-none"
            style={{
              background:
                "radial-gradient(circle, rgba(59, 130, 246, 0.1) 0%, transparent 70%)",
            }}
          />
        </motion.div>
      )}
    </AnimatePresence>
  )
}
