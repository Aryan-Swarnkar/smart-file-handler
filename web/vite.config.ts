import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server on 5173 — the origin already whitelisted by the API's
// CORS_ORIGINS and the SeaweedFS bucket CORS (presigned PUT is cross-origin).
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
});
