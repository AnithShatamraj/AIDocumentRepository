import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Inside Docker on Windows/macOS, a bind-mounted source tree delivers no
// inotify events to the Linux container, so Vite's watcher never fires and the
// page silently serves the last-transformed version. Polling is the fix, but
// it costs CPU, so it's opt-in via the env var docker-compose sets for the
// frontend service and stays off for a native `npm run dev`.
const usePolling = process.env.CHOKIDAR_USEPOLLING === "true";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    watch: usePolling ? { usePolling: true, interval: 300 } : undefined,
  },
});
