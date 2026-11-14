/** Token storage — localStorage survives page reloads (user-locked decision:
 * reload keeps the session; access tokens auto-refresh on 401).
 *
 * Kept as a tiny observable store OUTSIDE React so the fetch client can read
 * tokens without import cycles, and AuthContext can subscribe to external
 * clears (e.g. the client wipes tokens when a refresh proves dead). */

const ACCESS_KEY = "sfh.access";
const REFRESH_KEY = "sfh.refresh";

type Listener = () => void;
const listeners = new Set<Listener>();

function notify(): void {
  for (const fn of listeners) fn();
}

export const session = {
  get access(): string | null {
    return localStorage.getItem(ACCESS_KEY);
  },
  get refresh(): string | null {
    return localStorage.getItem(REFRESH_KEY);
  },
  has(): boolean {
    return !!(localStorage.getItem(ACCESS_KEY) || localStorage.getItem(REFRESH_KEY));
  },
  set(access: string, refresh: string): void {
    localStorage.setItem(ACCESS_KEY, access);
    localStorage.setItem(REFRESH_KEY, refresh);
    notify();
  },
  clear(): void {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
    notify();
  },
  subscribe(fn: Listener): () => void {
    listeners.add(fn);
    return () => {
      listeners.delete(fn);
    };
  },
};
