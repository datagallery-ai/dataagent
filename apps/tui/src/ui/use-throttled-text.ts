import { useEffect, useRef, useState } from "react";

export function useThrottledText(text: string, intervalMs = 50): string {
  const [rendered, setRendered] = useState(text);
  const latestRef = useRef(text);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flushingRef = useRef(false);

  latestRef.current = text;

  useEffect(() => {
    if (text === rendered) {
      return undefined;
    }

    if (timerRef.current !== null) {
      return undefined;
    }

    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      flushingRef.current = true;
      setRendered(latestRef.current);
      flushingRef.current = false;
    }, intervalMs);

    return () => {
      if (timerRef.current !== null && !flushingRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [text, rendered, intervalMs]);

  return rendered;
}
