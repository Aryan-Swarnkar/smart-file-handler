/** Login / register screen. Errors surface verbatim from our envelope
 * (429 shows a live countdown via the Toaster). */

import { useState } from "react";
import { useAuth } from "../state/AuthContext";
import { ApiError, isApiError } from "../api/client";
import { toastError } from "./Toaster";

export function AuthScreens(): React.JSX.Element {
  const { login, register } = useAuth();
  const [tab, setTab] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function submit(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (tab === "login") await login(email.trim(), password);
      else await register(email.trim(), password);
    } catch (err) {
      if (isApiError(err)) setError(err);
      else toastError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrap">
      <form className="auth-card" onSubmit={submit}>
        <h1 className="auth-logo">
          smart-file-<span>handler</span>
        </h1>
        <div className="tabs">
          <button
            type="button"
            className={tab === "login" ? "tab active" : "tab"}
            onClick={() => {
              setTab("login");
              setError(null);
            }}
          >
            Log in
          </button>
          <button
            type="button"
            className={tab === "register" ? "tab active" : "tab"}
            onClick={() => {
              setTab("register");
              setError(null);
            }}
          >
            Register
          </button>
        </div>

        <label>
          Email
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            autoComplete="username"
          />
        </label>
        <label>
          Password
          <input
            type="password"
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="min 8 chars"
            autoComplete={tab === "login" ? "current-password" : "new-password"}
          />
        </label>

        {error && (
          <div className="auth-error">
            <strong>{error.code}</strong> — {error.message}
          </div>
        )}

        <button className="btn primary wide" disabled={busy}>
          {busy ? "…" : tab === "login" ? "Log in" : "Create account"}
        </button>
        <p className="auth-hint">
          Local testing UI for the smart-file-handler API. Sessions survive
          reloads (auto-refresh on 401).
        </p>
      </form>
    </div>
  );
}
