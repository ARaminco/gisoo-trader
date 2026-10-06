import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'
import path from 'node:path'
import { execSync } from 'node:child_process'

// Shown on the More page so the user can tell which build the phone runs.
// The git hash is absent inside the Docker build context, so the time is
// the reliable part; a VITE_BUILD_ID build-arg can supply the hash there.
const BUILD_TIME = new Date().toISOString()
const BUILD_ID = (() => {
  if (process.env.VITE_BUILD_ID) return process.env.VITE_BUILD_ID
  try {
    return execSync('git rev-parse --short HEAD', { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
  } catch {
    return ''
  }
})()

export default defineConfig({
  define: {
    __BUILD_TIME__: JSON.stringify(BUILD_TIME),
    __BUILD_ID__: JSON.stringify(BUILD_ID),
  },
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      // src/main.tsx registers the worker itself (update checks on resume,
      // a visible reload offer), so the plugin must not inject a second one.
      injectRegister: null,
      // Our own worker (src/sw.ts): the generated one cannot receive pushes.
      // It keeps the same precache + runtime rules and adds the push handlers.
      strategies: 'injectManifest',
      srcDir: 'src',
      filename: 'sw.ts',
      injectManifest: {
        globPatterns: ['**/*.{js,css,html,svg,png,woff2}'],
      },
      includeAssets: [
        'favicon.svg',
        'favicon.png',
        'apple-touch-icon.png',
        'icon-192.png',
        'icon-512.png',
        'icon-maskable-512.png',
        'badge-96.png',
        'brand/mark.svg',
      ],
      manifest: {
        id: '/',
        name: 'Gisoo Trader',
        short_name: 'گیسو تریدر',
        description: 'گیسو تریدر: پنل کنترل ربات معامله‌گر متن‌باز',
        lang: 'fa',
        dir: 'rtl',
        theme_color: '#060912',
        background_color: '#060912',
        display: 'standalone',
        orientation: 'any',
        start_url: '/',
        scope: '/',
        categories: ['finance'],
        icons: [
          { src: 'icon-192.png?v=2', sizes: '192x192', type: 'image/png', purpose: 'any' },
          { src: 'icon-512.png?v=2', sizes: '512x512', type: 'image/png', purpose: 'any' },
          { src: 'icon-maskable-512.png?v=2', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
        shortcuts: [
          { name: 'معاملات', url: '/trades', icons: [{ src: 'icon-192.png?v=2', sizes: '192x192' }] },
          { name: 'سیگنال‌ها', url: '/signals', icons: [{ src: 'icon-192.png?v=2', sizes: '192x192' }] },
        ],
      },
      devOptions: { enabled: false },
    }),
  ],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          charts: ['recharts'],
        },
      },
    },
  },
})
