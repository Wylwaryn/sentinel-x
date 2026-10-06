import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// En production, Caddy sert le build (dist/) et relaie /api et /ws vers l'API dashboard.
// En dev (npm run dev), Vite relaie /api et /ws vers la pile Docker locale (Caddy :443).
const pile = process.env.SENTINEL_URL || "https://localhost";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: pile, secure: false },
      "/ws": { target: pile, secure: false, ws: true },
    },
  },
});
