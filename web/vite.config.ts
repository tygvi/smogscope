import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In development the React app runs on Vite (port 5173) and the API on uvicorn
// (port 8000). The proxy forwards /api calls to uvicorn, so the browser sees ONE
// origin: no CORS setup needed, and the same relative URLs work in production,
// where FastAPI serves the built app itself.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
