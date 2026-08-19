import { isValidElement, memo, useMemo, useState } from "react";
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remend from "remend";

function extractText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return extractText(node.props.children);
  return "";
}

function CodeBlock({ children }: ComponentPropsWithoutRef<"pre"> & { node?: unknown }) {
  const [copied, setCopied] = useState(false);
  const code = extractText(children).replace(/\n$/, "");

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="markdown-code-block">
      <div className="markdown-code-toolbar">
        <span>代码</span>
        <button type="button" onClick={() => void copy()} aria-label="复制代码">
          <span className="material-symbols-rounded">{copied ? "check" : "content_copy"}</span>
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre>{children}</pre>
    </div>
  );
}

const MARKDOWN_COMPONENTS: Components = {
  a: ({ node: _node, ...props }) => (
    <a {...props} target="_blank" rel="noopener noreferrer nofollow" />
  ),
  table: ({ node: _node, ...props }) => (
    <div className="markdown-table-wrap">
      <table {...props} />
    </div>
  ),
  pre: CodeBlock,
};
const REMARK_PLUGINS = [remarkGfm];

function MarkdownRenderer({ content, streaming }: { content: string; streaming: boolean }) {
  const renderedContent = useMemo(
    () => streaming
      ? remend(content, { linkMode: "text-only", inlineKatex: false, katex: false })
      : content,
    [content, streaming]
  );

  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        components={MARKDOWN_COMPONENTS}
        skipHtml
      >
        {renderedContent}
      </ReactMarkdown>
    </div>
  );
}

export default memo(MarkdownRenderer);
