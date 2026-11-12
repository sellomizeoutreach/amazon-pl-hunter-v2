import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev proxy: the FastAPI backend runs on :8000. In production the built
// files are served by FastAPI itself, so the app uses relative /api paths.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
