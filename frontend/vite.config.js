import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  build: {
    // Cloudscape is intentionally bundled for this static demo UI. Keep the
    // build output quiet unless the bundle grows materially beyond that baseline.
    chunkSizeWarningLimit: 1000,
  },
});
