"use client";

import { useEffect } from "react";
import { reportClientError } from "../lib/client-errors";

export default function ErrorPage({ error, reset }: { error: Error; reset: () => void }) {
  useEffect(() => reportClientError(error, { stage: "render" }), [error]);
  return <div role="alert"><p>The page could not be displayed.</p><button onClick={reset}>Try again</button></div>;
}
