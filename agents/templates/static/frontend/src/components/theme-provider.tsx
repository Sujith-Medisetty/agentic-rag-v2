// Re-export the provider AND the hook from `next-themes` so the agent
// can import either from one local path. Without this, code that
// follows the `@/components/theme-provider` pattern will assume
// `useTheme` lives here too — but it doesn't, it lives in `next-themes`
// — and the build will fail with "no exported member 'useTheme'".
export { ThemeProvider, useTheme } from "next-themes";
