"use client"

import { useRef, useMemo, Suspense } from "react"
import { Canvas, useFrame, useLoader } from "@react-three/fiber"
import * as THREE from "three"
import { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js"

const vertexShader = `
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

const fragmentShader = `
  uniform float uTime;
  varying vec3 vNormal;
  varying vec3 vViewDir;
  varying vec3 vWorldPos;
  void main() {
    float fresnel = 1.0 - abs(dot(vNormal, vViewDir));
    fresnel = pow(fresnel, 1.2);
    float featureGlow = smoothstep(0.2, 0.6, fresnel) * 0.4;
    float scanline = sin(vWorldPos.y * 50.0 + uTime * 1.5) * 0.5 + 0.5;
    scanline = smoothstep(0.4, 0.6, scanline) * 0.06;
    vec3 baseColor = vec3(0.231, 0.510, 0.965);
    vec3 edgeColor = vec3(0.576, 0.773, 0.992);
    vec3 color = mix(baseColor * 0.4, edgeColor, fresnel * 0.8 + featureGlow);
    float alpha = (0.08 + fresnel * 0.7 + featureGlow + scanline) * 0.9;
    gl_FragColor = vec4(color, alpha);
  }
`

// ---------------------------------------------------------------------------
// Breathing particles — dots leave the head, drift in swirl patterns, settle back
//
// Cycle (8s loop):
//   0-2s: settled on head surface (idle shimmer)
//   2-5s: burst outward in spiral/swirl paths
//   5-8s: converge back onto head surface
// ---------------------------------------------------------------------------

// 3D model rendered as the holographic particle cloud. Defaults to a
// procedurally generated orb that ships with the project. Point
// NEXT_PUBLIC_HEAD_MODEL_URL at your own .obj to use a different model
// (only vertex positions are used; the mesh is re-centred and re-scaled).
const HEAD_MODEL_URL = process.env.NEXT_PUBLIC_HEAD_MODEL_URL || "/orb.obj"

const PARTICLE_COUNT = 3000
const CYCLE_DURATION = 8

function Head() {
  const obj = useLoader(OBJLoader, HEAD_MODEL_URL)
  const turnRef = useRef<THREE.Group>(null)
  const pointsRef = useRef<THREE.Points>(null)

  const { geometry, shaderMaterial, surfacePositions, driftOffsets, currentPositions } =
    useMemo(() => {
      let geo: THREE.BufferGeometry | null = null
      obj.traverse((child) => {
        if (child instanceof THREE.Mesh && !geo) geo = child.geometry.clone()
      })
      if (!geo) geo = new THREE.SphereGeometry(1, 16, 16)

      // Center and scale
      geo.computeBoundingBox()
      const center = new THREE.Vector3()
      geo.boundingBox!.getCenter(center)
      geo.translate(-center.x, -center.y, -center.z)
      const size = new THREE.Vector3()
      geo.boundingBox!.getSize(size)
      const scale = 3.5 / Math.max(size.x, size.y, size.z)
      geo.scale(scale, scale, scale)
      geo.computeVertexNormals()

      // Sample surface positions
      const posAttr = geo.getAttribute("position")
      const totalVerts = posAttr.count
      const step = Math.max(1, Math.floor(totalVerts / PARTICLE_COUNT))
      const surface = new Float32Array(PARTICLE_COUNT * 3)
      const drift = new Float32Array(PARTICLE_COUNT * 4) // x,y,z offset + phase
      let idx = 0
      for (let i = 0; i < totalVerts && idx < PARTICLE_COUNT; i += step) {
        surface[idx * 3] = posAttr.getX(i)
        surface[idx * 3 + 1] = posAttr.getY(i)
        surface[idx * 3 + 2] = posAttr.getZ(i)
        // Each particle gets a unique drift direction + spiral phase
        const angle = Math.random() * Math.PI * 2
        const elevation = (Math.random() - 0.5) * Math.PI
        const radius = 0.8 + Math.random() * 1.2 // keep drift tight within canvas
        drift[idx * 4] = Math.cos(angle) * Math.cos(elevation) * radius
        drift[idx * 4 + 1] = Math.sin(elevation) * radius
        drift[idx * 4 + 2] = Math.sin(angle) * Math.cos(elevation) * radius
        drift[idx * 4 + 3] = Math.random() * Math.PI * 2 // phase offset for spiral
        idx++
      }

      const mat = new THREE.ShaderMaterial({
        vertexShader,
        fragmentShader,
        uniforms: { uTime: { value: 0 } },
        transparent: true,
        side: THREE.DoubleSide,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      })

      return {
        geometry: geo,
        shaderMaterial: mat,
        surfacePositions: surface.slice(0, idx * 3),
        driftOffsets: drift.slice(0, idx * 4),
        currentPositions: new Float32Array(idx * 3),
      }
    }, [obj])

  const particleCount = surfacePositions.length / 3

  useFrame(({ clock }) => {
    const t = clock.getElapsedTime()
    shaderMaterial.uniforms.uTime.value = t

    // Head rotation
    if (turnRef.current) {
      turnRef.current.rotation.y = Math.sin(t * 0.3) * (Math.PI / 4)
    }

    // Particle breathing cycle
    const cycleT = (t % CYCLE_DURATION) / CYCLE_DURATION // 0→1
    // Morph factor: 0 = on surface, 1 = fully drifted
    // 0.00-0.25: settled (morph=0)
    // 0.25-0.55: burst out (morph 0→1, eased)
    // 0.55-0.625: hold out (morph=1)
    // 0.625-1.0: settle back (morph 1→0, eased)
    let morph: number
    if (cycleT < 0.25) {
      morph = 0
    } else if (cycleT < 0.55) {
      const p = (cycleT - 0.25) / 0.3
      morph = p * p * (3 - 2 * p) // smoothstep
    } else if (cycleT < 0.625) {
      morph = 1
    } else {
      const p = (cycleT - 0.625) / 0.375
      morph = 1 - p * p * (3 - 2 * p) // smoothstep back
    }

    for (let i = 0; i < particleCount; i++) {
      const sx = surfacePositions[i * 3]
      const sy = surfacePositions[i * 3 + 1]
      const sz = surfacePositions[i * 3 + 2]
      const dx = driftOffsets[i * 4]
      const dy = driftOffsets[i * 4 + 1]
      const dz = driftOffsets[i * 4 + 2]
      const phase = driftOffsets[i * 4 + 3]

      // Add spiral motion when drifting
      const spiral = morph * 0.3
      const swirlAngle = t * 0.8 + phase
      const swirlX = Math.cos(swirlAngle) * spiral
      const swirlY = Math.sin(swirlAngle * 0.7) * spiral * 0.5
      const swirlZ = Math.sin(swirlAngle) * spiral

      let px = sx + (dx + swirlX) * morph
      let py = sy + (dy + swirlY) * morph
      let pz = sz + (dz + swirlZ) * morph

      // Clamp to sphere so particles don't hit canvas edge
      const dist = Math.sqrt(px * px + py * py + pz * pz)
      const maxR = 3.2
      if (dist > maxR) {
        const s = maxR / dist
        px *= s; py *= s; pz *= s
      }

      currentPositions[i * 3] = px
      currentPositions[i * 3 + 1] = py
      currentPositions[i * 3 + 2] = pz
    }

    if (pointsRef.current) {
      const geom = pointsRef.current.geometry
      geom.attributes.position.array.set(currentPositions)
      geom.attributes.position.needsUpdate = true
      // Dots visible when drifting, fade out when settled on surface
      const mat = pointsRef.current.material as THREE.PointsMaterial
      mat.opacity = morph * 0.85
    }
  })

  return (
    <group ref={turnRef}>
      <group rotation={[Math.PI / 2, Math.PI, 0]}>
        {/* Holographic surface */}
        <mesh geometry={geometry} material={shaderMaterial} />

        {/* Breathing particles */}
        <points ref={pointsRef}>
          <bufferGeometry>
            <bufferAttribute
              attach="attributes-position"
              args={[new Float32Array(surfacePositions.length), 3]}
            />
          </bufferGeometry>
          <pointsMaterial
            color="#93c5fd"
            size={0.03}
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

export default function GideonHead({ size = 120 }: { size?: number }) {
  return (
    <div style={{ width: size, height: size }}>
      <Canvas
        camera={{ position: [0, 0.3, 9], fov: 38 }}
        gl={{ alpha: true, antialias: true }}
        style={{ background: "transparent" }}
      >
        <Suspense fallback={null}>
          <Head />
        </Suspense>
      </Canvas>
    </div>
  )
}
