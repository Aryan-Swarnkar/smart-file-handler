import { useState } from "react";
import { AuthProvider, useAuth } from "./state/AuthContext";
import { AuthScreens } from "./components/AuthScreens";
import { StatusStrip } from "./components/StatusStrip";
import { Dropzone } from "./components/Dropzone";
import { UploadQueueView, useUploadQueue } from "./components/UploadQueue";
import { FileGrid } from "./components/FileGrid";
import { Toaster } from "./components/Toaster";

export default function App(): React.JSX.Element {
  return (
    <AuthProvider>
      <Shell />
      <Toaster />
    </AuthProvider>
  );
}

function Shell(): React.JSX.Element | null {
  const { user, ready, logout } = useAuth();
  // Bumping this refetches page 1 of the grid (uploads landed).
  const [refreshSignal, setRefreshSignal] = useState(0);
  const queue = useUploadQueue(() => setRefreshSignal((n) => n + 1));

  if (!ready) return <div className="boot">connecting…</div>;
  if (!user) return <AuthScreens />;

  return (
    <div className="app">
      <header className="topbar">
        <span className="logo">
          smart-file-<span>handler</span>
        </span>
        <StatusStrip />
        <span className="user-email" title={user.id}>
          {user.email}
        </span>
        <button className="btn ghost" onClick={() => void logout()}>
          Log out
        </button>
      </header>

      <main>
        <Dropzone onFiles={queue.addFiles} />
        <UploadQueueView queue={queue} />
        <FileGrid refreshSignal={refreshSignal} />
      </main>
    </div>
  );
}
