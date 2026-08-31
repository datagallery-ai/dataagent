"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useRouter } from "next/navigation";
import {
  clearConfigApiIdentity,
  configApi,
  type ConfigApiIdentity,
} from "../../lib/config-api/client";
import { PasswordAuthShell } from "../../components/auth/auth-flow";
import { LanguageToggle } from "../../i18n/LanguageToggle";
import { useT } from "../../i18n/locale-context";

type DataTaskIdentityContextValue = {
  authMode: "password";
  authHeaders: Record<string, string>;
  changePassword: (input: { currentPassword: string; newPassword: string }) => Promise<void>;
  currentUser: ConfigApiIdentity;
  error: string | null;
  loading: boolean;
  scopeKey: string;
  signOut: () => void;
  signOutAll: () => void;
};

const DataTaskIdentityContext = createContext<DataTaskIdentityContextValue | null>(null);

function identityInitials(identity: ConfigApiIdentity): string {
  const source = identity.displayName || identity.userId;
  const parts = source.split(/\s+/u).filter(Boolean);
  if (parts.length >= 2) {
    return `${parts[0]![0] ?? ""}${parts[1]![0] ?? ""}`.toUpperCase();
  }
  return source.slice(0, 2).toUpperCase();
}

function dtoToIdentity(user: { id: string; displayName?: string; email?: string }): ConfigApiIdentity {
  return {
    userId: user.id,
    displayName: user.displayName || user.id,
    ...(user.email ? { email: user.email } : {}),
  };
}

export function DataTaskIdentityProvider({ children }: { children: ReactNode }) {
  return <PasswordIdentityProvider>{children}</PasswordIdentityProvider>;
}

function PasswordIdentityProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [currentUser, setCurrentUser] = useState<ConfigApiIdentity | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadMe = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await configApi.getMe();
      setCurrentUser(dtoToIdentity(response.user));
      clearConfigApiIdentity();
    } catch (err) {
      setCurrentUser(null);
      if (err instanceof Error && !err.message.includes("Authentication required")) {
        setError(err.message);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadMe();
  }, [loadMe]);

  const signOut = useCallback(() => {
    void configApi.logout().finally(() => {
      setCurrentUser(null);
    });
  }, []);

  const signOutAll = useCallback(() => {
    void configApi.logoutAll().finally(() => {
      setCurrentUser(null);
    });
  }, []);

  const changePassword = useCallback(async (input: { currentPassword: string; newPassword: string }) => {
    await configApi.changePassword(input);
  }, []);

  const value = useMemo<DataTaskIdentityContextValue | null>(() => {
    if (!currentUser) return null;
    return {
      authMode: "password",
      authHeaders: csrfAuthHeaders(),
      changePassword,
      currentUser,
      error,
      loading,
      scopeKey: currentUser.userId,
      signOut,
      signOutAll,
    };
  }, [changePassword, currentUser, error, loading, signOut, signOutAll]);

  useEffect(() => {
    if (!loading && (!currentUser || !value)) {
      router.replace("/login");
    }
  }, [loading, currentUser, value, router]);

  if (loading) {
    return <PasswordAuthShell title="Loading account..." />;
  }
  if (!currentUser || !value) {
    return <PasswordAuthShell title="Redirecting to sign in..." />;
  }
  return (
    <DataTaskIdentityContext.Provider value={value}>
      {children}
    </DataTaskIdentityContext.Provider>
  );
}

function csrfAuthHeaders(): Record<string, string> {
  if (typeof document === "undefined") return {};
  const token = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("df_csrf="))
    ?.slice("df_csrf=".length);
  return token ? { "X-CSRF-Token": token } : {};
}

export function useDataTaskIdentity(): DataTaskIdentityContextValue {
  const context = useContext(DataTaskIdentityContext);
  if (!context) {
    throw new Error("useDataTaskIdentity must be used within DataTaskIdentityProvider");
  }
  return context;
}

export function DataTaskUserBar({
  compact = false,
  onOpenSettings,
  quickStartGuide,
}: {
  compact?: boolean;
  onOpenSettings?: () => void;
  quickStartGuide?: ReactNode;
}) {
  const {
    currentUser,
    error,
    signOut,
  } = useDataTaskIdentity();
  const t = useT();
  const [open, setOpen] = useState(false);

  if (compact) {
    return (
      <div className="mt-auto flex flex-col items-center gap-2 border-t border-border px-2 pt-2">
        {quickStartGuide}
        <button
          type="button"
          onClick={() => onOpenSettings?.()}
          className="flex h-9 w-9 items-center justify-center rounded-lg border border-border bg-surface text-xs font-semibold text-foreground transition-colors hover:bg-surface-subtle"
          title={currentUser.displayName || currentUser.userId}
          aria-label={t("userBar.currentUser")}
        >
          {identityInitials(currentUser)}
        </button>
        <LanguageToggle compact />
      </div>
    );
  }

  return (
    <div className="relative mt-auto border-t border-border bg-surface-subtle px-2 py-2">
      {open ? (
        <div className="account-menu-popover-in absolute bottom-full left-2 right-2 z-50 origin-bottom rounded-xl border border-border bg-surface p-1.5 shadow-[var(--shadow-popover)]">
          <div className="border-b border-border px-2 py-2">
            <div className="flex items-center gap-2 px-2 py-1.5">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-border bg-surface-subtle text-xs font-semibold text-foreground">
                {identityInitials(currentUser)}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-sm font-semibold text-foreground">
                  {currentUser.displayName || currentUser.userId}
                </span>
                <span className="block truncate text-xs text-muted-light">
                  {currentUser.email || currentUser.userId}
                </span>
              </span>
            </div>
          </div>
          <div className="pt-1.5">
            <AccountMenuItem
              label={t("userBar.settings")}
              onClick={() => {
                onOpenSettings?.();
                setOpen(false);
              }}
            />
            <AccountMenuItem
              label={t("userBar.signOut")}
              onClick={() => {
                signOut();
                setOpen(false);
              }}
            />
          </div>
          {error ? <p className="mt-2 text-xs text-rose-700">{error}</p> : null}
        </div>
      ) : null}
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-surface/60"
        >
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-surface-subtle text-xs font-semibold text-foreground">
            {identityInitials(currentUser)}
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-xs font-semibold text-foreground">
              {currentUser.displayName || currentUser.userId}
            </span>
            <span className="block truncate text-[11px] text-muted-light">{currentUser.email || currentUser.userId}</span>
          </span>
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-light transition-colors hover:bg-surface-subtle hover:text-foreground">
            <AccountMoreIcon />
          </span>
        </button>
        {quickStartGuide}
        <LanguageToggle />
      </div>
    </div>
  );
}

function AccountMoreIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4" fill="currentColor" aria-hidden>
      <circle cx="4.5" cy="10" r="1.2" />
      <circle cx="10" cy="10" r="1.2" />
      <circle cx="15.5" cy="10" r="1.2" />
    </svg>
  );
}

function AccountMenuItem({
  detail,
  disabled = false,
  label,
  onClick,
  shortcut,
  trailing,
}: {
  detail?: string;
  disabled?: boolean;
  label: string;
  onClick?: () => void;
  shortcut?: string;
  trailing?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="flex min-h-9 w-full items-center gap-2 rounded-md px-2 text-left text-xs font-medium text-foreground transition-colors enabled:hover:bg-surface-subtle disabled:cursor-not-allowed disabled:text-muted-light"
    >
      <span className="min-w-0 flex-1">
        <span className="block truncate">{label}</span>
        {detail ? <span className="block truncate text-[11px] font-normal text-muted-light">{detail}</span> : null}
      </span>
      {shortcut ? <span className="shrink-0 text-muted-light">{shortcut}</span> : null}
      {trailing ? <span className="shrink-0 text-muted-light">{trailing}</span> : null}
    </button>
  );
}
