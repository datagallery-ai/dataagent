export type TuiUser = {
  id: string;
  email: string;
  displayName?: string;
};

export type TuiWorkspace = {
  id: string;
  name?: string;
};

export type StoredTuiSession = {
  apiBaseUrl: string;
  cookies: Record<string, string>;
  user: TuiUser;
  workspace: TuiWorkspace;
  expiresAt: string;
};

export type AuthStatus = {
  publicBaseUrl: string;
  registrationEnabled: boolean;
};

export type AuthCommandController = {
  logout(): Promise<
    | { kind: "complete" }
    | { kind: "remote-failed"; clearLocalOnly: () => Promise<void> }
    | {
        kind: "local-cleanup-failed";
        storePath: string;
        error: string;
        retry: () => Promise<void>;
      }
  >;
};

export type AppExitReason = "exit" | "logout" | "auth-required";
