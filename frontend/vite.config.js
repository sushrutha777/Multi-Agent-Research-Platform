import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/research": "http://localhost:8000",
      "/result": "http://localhost:8000",
      "/session": "http://localhost:8000",
      "/diff": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
});
