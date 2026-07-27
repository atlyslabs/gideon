"use client"

import type { ComponentType } from "react"
import {
  Stack,
  Card,
  Heading,
  Text,
  Pill,
  AirportTime,
  DurationBar,
  AirlineBadge,
  PriceTag,
  Button,
} from "./components"

export type Node = {
  component: string
  props?: Record<string, unknown>
  children?: Node[]
}

const REGISTRY: Record<string, ComponentType<any>> = {
  Stack,
  Card,
  Heading,
  Text,
  Pill,
  AirportTime,
  DurationBar,
  AirlineBadge,
  PriceTag,
  Button,
}

type RenderProps = {
  node: Node
  onAction: (id: string) => void
  path?: string
}

export function Renderer({ node, onAction, path = "0" }: RenderProps) {
  const Comp = REGISTRY[node.component]
  if (!Comp) {
    return (
      <div
        style={{
          padding: 8,
          fontSize: 12,
          color: "#f87171",
          border: "1px dashed #f87171",
          borderRadius: 6,
        }}
      >
        Unknown component: {node.component}
      </div>
    )
  }

  const props: Record<string, unknown> = { ...(node.props ?? {}) }

  // Translate model-emitted actionId into a real handler.
  if (node.component === "Button" && typeof props.actionId === "string") {
    const id = props.actionId
    props.onClick = () => onAction(id)
    delete props.actionId
  }

  const children = node.children?.map((child, idx) => (
    <Renderer key={`${path}.${idx}`} node={child} onAction={onAction} path={`${path}.${idx}`} />
  ))

  return <Comp {...props}>{children}</Comp>
}
