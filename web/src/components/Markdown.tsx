// Markdown renderer for assistant response text.
//
// react-markdown + remark-gfm gives us GitHub-flavored markdown (tables,
// strikethrough, task lists, autolinks) on top of CommonMark. We then
// override the renderer for every element type so each one inherits our
// dark-theme typography instead of the browser defaults.
//
// Streaming-safe: react-markdown parses the input on every render. If the
// final ``` of a fenced code block hasn't arrived yet, react-markdown still
// renders the partial block as inline text — better than the alternative
// (Prism-style highlighters often choke on unterminated tokens).

import ReactMarkdown, { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// Each component override applies our dark-theme classes. Tailwind needs to
// see every class at build time, so we spell them out rather than building
// strings dynamically.
const components: Components = {
  // Headings — clear hierarchy without shouting. h1 / h2 get a hairline
  // underline so they stand out from prose.
  h1: ({ children }) => (
    <h1 className="mt-4 mb-2 border-b border-border/60 pb-1 text-[18px] font-semibold tracking-tight text-text first:mt-0">
      {children}
    </h1>
  ),
  h2: ({ children }) => (
    <h2 className="mt-4 mb-2 border-b border-border/40 pb-0.5 text-[16px] font-semibold tracking-tight text-text first:mt-0">
      {children}
    </h2>
  ),
  h3: ({ children }) => (
    <h3 className="mt-3 mb-1.5 text-[15px] font-semibold text-text first:mt-0">
      {children}
    </h3>
  ),
  h4: ({ children }) => (
    <h4 className="mt-3 mb-1 text-[14px] font-semibold text-text first:mt-0">
      {children}
    </h4>
  ),
  // Paragraphs — relaxed leading for readability.
  p: ({ children }) => (
    <p className="my-2 leading-relaxed first:mt-0 last:mb-0">{children}</p>
  ),
  // Emphasis.
  strong: ({ children }) => (
    <strong className="font-semibold text-text">{children}</strong>
  ),
  em: ({ children }) => <em className="italic text-text/90">{children}</em>,
  del: ({ children }) => (
    <del className="text-subtle line-through">{children}</del>
  ),
  // Links — accent color, open externally.
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent underline decoration-accent/40 underline-offset-2 hover:decoration-accent"
    >
      {children}
    </a>
  ),
  // Lists.
  ul: ({ children }) => (
    <ul className="my-2 ml-5 list-disc space-y-1 marker:text-subtle">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="my-2 ml-5 list-decimal space-y-1 marker:text-subtle">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  // Inline + block code.
  code: ({ className, children, ...props }: any) => {
    const isBlock = /language-/.test(className || "") || String(children).includes("\n");
    if (isBlock) {
      // Block code — handled by <pre> below. We just style the inner span.
      return (
        <code
          className={`block whitespace-pre overflow-x-auto font-mono text-tx-sm leading-snug ${className ?? ""}`}
          {...props}
        >
          {children}
        </code>
      );
    }
    return (
      <code
        className="rounded border border-border/60 bg-elevated/60 px-1 py-px font-mono text-[12.5px] text-accent"
        {...props}
      >
        {children}
      </code>
    );
  },
  pre: ({ children }) => (
    <pre className="my-3 overflow-x-auto rounded-lg border border-border bg-bg/80 p-3 font-mono text-tx-sm leading-snug text-text">
      {children}
    </pre>
  ),
  // Blockquote — left rail, dimmer text.
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-accent/40 bg-elevated/30 py-1 pl-3 italic text-muted">
      {children}
    </blockquote>
  ),
  // Horizontal rule.
  hr: () => <hr className="my-4 border-t border-border/60" />,
  // Tables (GFM) — bordered, dark-theme, scrollable horizontally on narrow screens.
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto">
      <table className="w-full border-collapse text-tx-sm">
        {children}
      </table>
    </div>
  ),
  thead: ({ children }) => (
    <thead className="bg-elevated/60">{children}</thead>
  ),
  tbody: ({ children }) => (
    <tbody className="divide-y divide-border/40">{children}</tbody>
  ),
  tr: ({ children }) => <tr className="border-b border-border/40">{children}</tr>,
  th: ({ children, style }) => (
    <th
      className="border border-border/60 px-2.5 py-1.5 text-left text-[11px] font-semibold uppercase tracking-[0.08em] text-subtle"
      style={style as React.CSSProperties}
    >
      {children}
    </th>
  ),
  td: ({ children, style }) => (
    <td
      className="border border-border/40 px-2.5 py-1.5 align-top text-text"
      style={style as React.CSSProperties}
    >
      {children}
    </td>
  ),
};

export default function Markdown({ text }: { text: string }) {
  return (
    <div className="font-sans text-[14px] leading-relaxed text-text">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
