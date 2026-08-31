"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AuthFlow, PasswordAuthShell } from "../../components/auth/auth-flow";
import { configApi } from "../../lib/config-api/client";

export function RegisterClient() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);
  const [registrationEnabled, setRegistrationEnabled] = useState(true);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      configApi.getMe().then(() => true).catch(() => false),
      configApi.getAuthStatus().then((status) => status.registrationEnabled).catch(() => true),
    ]).then(([signedIn, canRegister]) => {
      if (cancelled) return;
      if (signedIn) {
        router.replace("/data-tasks");
        return;
      }
      setRegistrationEnabled(canRegister);
      setChecking(false);
    });
    return () => {
      cancelled = true;
    };
  }, [router]);

  if (checking) {
    return <PasswordAuthShell title="Loading account..." />;
  }

  if (!registrationEnabled) {
    return (
      <PasswordAuthShell
        title="Registration closed"
        subtitle="Registration is closed. Contact your deployment administrator."
      >
        <button
          type="button"
          className="text-sm font-medium text-foreground underline-offset-2 hover:underline"
          onClick={() => router.push("/login")}
        >
          Back to sign in
        </button>
      </PasswordAuthShell>
    );
  }

  return (
    <AuthFlow
      initialMode="register"
      registrationEnabled={registrationEnabled}
      onAuthenticated={() => router.replace("/data-tasks")}
    />
  );
}
