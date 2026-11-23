"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AuthFlow, PasswordAuthShell } from "../../components/auth/auth-flow";
import { configApi } from "../../lib/config-api/client";

export function LoginClient() {
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

  return (
    <AuthFlow
      initialMode="login"
      registrationEnabled={registrationEnabled}
      onAuthenticated={() => router.replace("/data-tasks")}
    />
  );
}
