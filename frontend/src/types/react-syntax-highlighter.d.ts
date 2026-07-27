// Local ambient types for react-syntax-highlighter. The package ships no types
// and @types/react-syntax-highlighter isn't installed (the monorepo's yarn
// workspace install is currently broken on an unrelated package, so we can't
// `yarn add -D` it cleanly). This shim covers exactly what markdownConfig uses:
// the PrismAsyncLight highlighter + a prism style imported by its file path.
declare module "react-syntax-highlighter" {
  import type { ComponentType, ReactNode } from "react"
  interface SyntaxHighlighterProps {
    language?: string
    style?: Record<string, unknown>
    customStyle?: Record<string, unknown>
    codeTagProps?: Record<string, unknown>
    wrapLongLines?: boolean
    showLineNumbers?: boolean
    children?: ReactNode
    [key: string]: unknown
  }
  export const PrismAsyncLight: ComponentType<SyntaxHighlighterProps>
  export const Prism: ComponentType<SyntaxHighlighterProps>
  const _default: ComponentType<SyntaxHighlighterProps>
  export default _default
}

declare module "react-syntax-highlighter/dist/esm/styles/prism/*" {
  const style: Record<string, Record<string, string>>
  export default style
}
