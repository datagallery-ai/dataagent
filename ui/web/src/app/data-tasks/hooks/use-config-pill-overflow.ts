"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { splitConfigPillsByWidth } from "../config-pill-overflow";
import {
  PER_RUN_MENTION_KINDS,
  type PerRunMentionKind,
} from "../data-task-state";

type UseConfigPillOverflowResult = {
  pillsContainerRef: (node: HTMLDivElement | null) => void;
  setPillRef: (kind: PerRunMentionKind, node: HTMLDivElement | null) => void;
  visibleKinds: PerRunMentionKind[];
  overflowKinds: PerRunMentionKind[];
};

export function useConfigPillOverflow(): UseConfigPillOverflowResult {
  const [pillsContainer, setPillsContainer] = useState<HTMLDivElement | null>(
    null,
  );
  const [pillsContainerWidth, setPillsContainerWidth] = useState(0);
  const pillElementsRef = useRef(new Map<PerRunMentionKind, HTMLDivElement>());
  const [measuredWidths, setMeasuredWidths] = useState<
    Partial<Record<PerRunMentionKind, number>>
  >({});

  const setPillRef = (kind: PerRunMentionKind, node: HTMLDivElement | null) => {
    if (node) {
      pillElementsRef.current.set(kind, node);
    } else {
      pillElementsRef.current.delete(kind);
    }
  };

  useLayoutEffect(() => {
    if (!pillsContainer) return;

    const measure = () => {
      setPillsContainerWidth(pillsContainer.clientWidth);

      const nextWidths: Partial<Record<PerRunMentionKind, number>> = {};
      for (const kind of PER_RUN_MENTION_KINDS) {
        const element = pillElementsRef.current.get(kind);
        if (element) {
          nextWidths[kind] = element.offsetWidth;
        }
      }
      setMeasuredWidths(nextWidths);
    };

    measure();

    const observer = new ResizeObserver(measure);
    observer.observe(pillsContainer);
    for (const element of pillElementsRef.current.values()) {
      observer.observe(element);
    }

    return () => observer.disconnect();
  }, [pillsContainer]);

  const { visible, overflow } = splitConfigPillsByWidth(
    PER_RUN_MENTION_KINDS,
    pillsContainerWidth,
    measuredWidths,
  );

  return {
    pillsContainerRef: setPillsContainer,
    setPillRef,
    visibleKinds: visible,
    overflowKinds: overflow,
  };
}
