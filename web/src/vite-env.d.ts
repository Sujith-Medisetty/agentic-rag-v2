/// <reference types="vite/client" />

// Pulls in Vite's ambient types so `import.meta.env.DEV`, `import.meta.env.PROD`,
// and any user-defined VITE_* env vars are known to TypeScript. Without this
// reference, `npm run build` fails with TS2339 on the registerSW guard.
