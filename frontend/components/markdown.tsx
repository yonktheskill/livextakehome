import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
export function SafeMarkdown({ content }: { content: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        disallowedElements={["img", "input"]}
        components={{
          a: ({ href, children }) => {
            const safe = href && /^(https?:\/\/|mailto:)/i.test(href);
            return safe ? (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            );
          },
          table: ({ children }) => (
            <div className="table-scroll">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
