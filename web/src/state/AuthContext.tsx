/** Auth state: hydrates from localStorage on load, exposes login/register/
 * logout, and reacts when the fetch client clears a dead session. */

import { createContext, useContext, useEffect, useState } from "react";
import * as authApi from "../api/auth";
import { session } from "../api/session";
import type { UserOut } from "../types";
import { toastError } from "../components/Toaster";

interface AuthContextValue {
  user: UserOut | null;
  /** True once the initial me()/clear check finished (prevents flash). */
  ready: boolean;
  login(email: string, password: string): Promise<void>;
  register(email: string, password: string): Promise<void>;
  logout(): Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }): React.JSX.Element {
  const [user, setUser] = useState<UserOut | null>(null);
  const [ready, setReady] = useState(false);

  // Initial hydration: stored tokens → who am I?
  useEffect(() => {
    let cancelled = false;
    async function boot(): Promise<void> {
      if (!session.has()) {
        setReady(true);
        return;
      }
      try {
        const u = await authApi.me();
        if (!cancelled) setUser(u);
      } catch {
        // me() failed even after one auto-refresh → dead session.
        session.clear();
      } finally {
        if (!cancelled) setReady(true);
      }
    }
    void boot();
    // External clears (client session.clear on dead refresh) → logout UI.
    const unsub = session.subscribe(() => {
      if (!session.has()) setUser(null);
    });
    return () => {
      cancelled = true;
      unsub();
    };
  }, []);

  async function login(email: string, password: string): Promise<void> {
    const pair = await authApi.login(email, password);
    session.set(pair.access_token, pair.refresh_token);
    setUser(await authApi.me());
  }

  async function register(email: string, password: string): Promise<void> {
    await authApi.register(email, password);
    // Registration returns the user, not tokens — log straight in.
    await login(email, password);
  }

  async function logout(): Promise<void> {
    try {
      await authApi.logout(session.refresh);
    } catch (err) {
      // 204-always endpoint; a network blip shouldn't block logging out.
      toastError(err);
    }
    session.clear();
    setUser(null);
  }

  return (
    <AuthContext.Provider value={{ user, ready, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside <AuthProvider>");
  return ctx;
}
