// 流式文本：打字机效果
import { useEffect, useRef, useState } from "react";

export default function StreamingText({
  content,
  streaming,
}: {
  content: string;
  streaming: boolean;
}) {
  const [shown, setShown] = useState(content.length);
  const prevLenRef = useRef(content.length);

  // 非流式：直接显示全文
  useEffect(() => {
    if (!streaming) {
      setShown(content.length);
    }
  }, [streaming, content]);

  // 流式：content 增量追加时，平滑增加已显示字数（保留已显示部分）
  useEffect(() => {
    if (!streaming) return;
    const prevLen = prevLenRef.current;
    prevLenRef.current = content.length;
    if (content.length > prevLen) {
      setShown(content.length);
    }
  }, [streaming, content]);

  // 打字机节奏：仅当 streaming 且 shown 落后于 content 时逐步前进
  useEffect(() => {
    if (!streaming) return;
    const timer = setInterval(() => {
      setShown((s) => {
        if (s >= content.length) {
          clearInterval(timer);
          return s;
        }
        return Math.min(s + 2, content.length);
      });
    }, 20);
    return () => clearInterval(timer);
  }, [streaming, content.length]);

  return <span>{content.slice(0, shown)}</span>;
}
