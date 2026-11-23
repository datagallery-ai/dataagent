"use client";

import ErrorPage from "./error";

export default function GlobalError({ error, reset }: { error: Error; reset: () => void }) {
  return <html lang="en"><body><ErrorPage error={error} reset={reset} /></body></html>;
}
