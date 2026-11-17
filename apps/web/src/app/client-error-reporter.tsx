"use client";

import { useEffect } from "react";
import { reportClientError } from "../lib/client-errors";

export function ClientErrorReporter() {
  useEffect(() => {
    const onError = (event: ErrorEvent) => reportClientError(event.error ?? event.message, { stage: "render" });
    const onRejection = (event: PromiseRejectionEvent) => reportClientError(event.reason, { stage: "promise" });
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);
  return null;
}
