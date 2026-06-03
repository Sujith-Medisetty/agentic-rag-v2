// localStorage-backed bearer token store. Single user per browser profile.

const KEY = "agentic-rag.token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(KEY, token);
  } catch {
    /* ignore — private mode / storage full */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}

export function hasToken(): boolean {
  return !!getToken();
}
