// LLM Providers admin page (root only).
//
// Pick the active provider + model and manage per-provider API keys. Every
// change takes effect on the next LLM call — no backend restart. The page
// reads + writes the same app_settings KV store that the legacy env vars
// were derived from, so a saved choice survives a backend restart.

import { useEffect, useMemo, useState } from "react";
import { Link, Navigate } from "react-router-dom";
import { ApiError, authApi, pricingApi, providersApi } from "@/lib/api";
import type {
  ActiveProvider, ModelPrice, ModelPriceUpdate, ProviderInfo, TestResult,
} from "@/lib/api";
import { KeyIcon } from "@/components/icons";

// Per-provider UI draft state — keyed by provider id.
type DraftMap = Record<
  string,
  { api_key: string; base_url: string; show_key: boolean }
>;

export default function Providers() {
  const [me, setMe] = useState<"loading" | "denied" | "ok">("loading");
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [active, setActive] = useState<ActiveProvider>({
    provider: "",
    model: "",
  });
  const [err, setErr] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  // Form state for the "swap card"
  const [swapProvider, setSwapProvider] = useState("");
  const [swapModel, setSwapModel] = useState("");
  const [savingSwap, setSavingSwap] = useState(false);

  // Form state for the per-provider cards (key + base_url draft)
  const [drafts, setDrafts] = useState<DraftMap>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, TestResult>>({});

  // Token pricing catalog + per-row drafts. Grouped by provider for display.
  const [prices, setPrices] = useState<ModelPrice[]>([]);
  const [draftPrices, setDraftPrices] = useState<Record<string, ModelPriceUpdate>>({});
  const [savingPrice, setSavingPrice] = useState<string | null>(null);

  // Resolve current user (root only).
  useEffect(() => {
    authApi.me()
      .then((u) => setMe(u.role === "root" ? "ok" : "denied"))
      .catch(() => setMe("denied"));
  }, []);

  // Load the catalog once we're confirmed root.
  useEffect(() => {
    if (me !== "ok") return;
    void reload();
  }, [me]);

  const reload = async () => {
    try {
      const [data, priceData] = await Promise.all([
        providersApi.list(),
        pricingApi.list().catch(() => ({ models: [] as ModelPrice[] })),
      ]);
      setProviders(data.providers);
      setActive(data.active);
      setPrices(priceData.models);
      // Seed the swap-card selects from the active config.
      setSwapProvider((cur) => cur || data.active.provider);
      setSwapModel((cur) => cur || data.active.model);
      // Seed per-provider drafts so the input fields have something to show.
      setDrafts((cur) => {
        const next = { ...cur };
        for (const p of data.providers) {
          if (!next[p.id]) {
            next[p.id] = { api_key: "", base_url: p.default_base_url ?? "", show_key: false };
          } else if (!next[p.id].base_url && p.default_base_url) {
            next[p.id].base_url = p.default_base_url;
          }
        }
        return next;
      });
      // Seed per-model price drafts from the catalog (so the inputs show
      // the current effective price, override or builtin).
      setDraftPrices((cur) => {
        const next = { ...cur };
        for (const m of priceData.models) {
          if (!next[m.model]) {
            next[m.model] = {
              input: m.input,
              output: m.output,
              cache_write: m.cache_write,
              cache_read: m.cache_read,
            };
          }
        }
        return next;
      });
      setErr(null);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "failed to load providers");
    }
  };

  const setPriceDraft = (model: string, patch: Partial<ModelPriceUpdate>) => {
    setDraftPrices((cur) => ({ ...cur, [model]: { ...cur[model], ...patch } }));
  };

  const onSavePrice = async (m: ModelPrice) => {
    const d = draftPrices[m.model];
    if (!d) return;
    setSavingPrice(m.model);
    setErr(null);
    setInfo(null);
    try {
      await pricingApi.set(m.model, {
        input: Number(d.input),
        output: Number(d.output),
        cache_write: Number(d.cache_write ?? 0),
        cache_read: Number(d.cache_read ?? 0),
      });
      setInfo(`Saved price for ${m.model}.`);
      await reload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : `failed to save price for ${m.model}`);
    } finally {
      setSavingPrice(null);
    }
  };

  const onResetPrice = async (m: ModelPrice) => {
    if (m.source !== "override") return;
    if (!confirm(`Reset ${m.model} to the built-in price?`)) return;
    setSavingPrice(m.model);
    setErr(null);
    setInfo(null);
    try {
      await pricingApi.delete(m.model);
      setInfo(`Reset ${m.model} to built-in price.`);
      await reload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : `failed to reset price for ${m.model}`);
    } finally {
      setSavingPrice(null);
    }
  };

  // Group models by provider_id so the pricing section reads top-down
  // (Anthropic → OpenAI → Google → …) like the catalog above.
  const groupedPrices = useMemo(() => {
    const groups = new Map<string, ModelPrice[]>();
    for (const p of prices) {
      const key = p.provider_id ?? "_other";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(p);
    }
    // Preserve catalog order: use the providers list as the canonical order,
    // then any leftover ("_other") at the end.
    const ordered: [string, ModelPrice[]][] = [];
    for (const p of providers) {
      const g = groups.get(p.id);
      if (g) ordered.push([p.id, g]);
    }
    if (groups.has("_other")) ordered.push(["_other", groups.get("_other")!]);
    return ordered;
  }, [prices, providers]);

  if (me === "loading") {
    return (
      <div className="mx-auto max-w-3xl px-4 py-12 text-sm text-muted">
        Loading…
      </div>
    );
  }
  if (me === "denied") return <Navigate to="/" replace />;

  const activeProvider = providers.find((p) => p.id === active.provider);

  const setDraft = (id: string, patch: Partial<DraftMap[string]>) => {
    setDrafts((cur) => ({ ...cur, [id]: { ...cur[id], ...patch } }));
  };

  const onSwap = async () => {
    if (!swapProvider || !swapModel.trim()) return;
    setSavingSwap(true);
    setErr(null);
    setInfo(null);
    try {
      const next = await providersApi.setActive(swapProvider, swapModel.trim());
      setActive(next);
      setInfo(`Active provider set to ${swapProvider} / ${swapModel.trim()}.`);
      // Reload so is_active badges and key indicators refresh everywhere.
      await reload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "failed to swap provider");
    } finally {
      setSavingSwap(false);
    }
  };

  const onSaveKey = async (p: ProviderInfo) => {
    const draft = drafts[p.id];
    if (!draft || !draft.api_key.trim()) {
      setErr("API key is required to save.");
      return;
    }
    setSavingKey(p.id);
    setErr(null);
    setInfo(null);
    try {
      await providersApi.setKey(p.id, draft.api_key.trim(), draft.base_url || null);
      setInfo(`Saved key for ${p.name}.`);
      setDraft(p.id, { api_key: "" }); // clear the input after save
      await reload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : `failed to save key for ${p.name}`);
    } finally {
      setSavingKey(null);
    }
  };

  const onClearKey = async (p: ProviderInfo) => {
    if (!confirm(`Clear the saved API key for ${p.name}? The provider will fall back to env vars (if set).`)) return;
    setSavingKey(p.id);
    setErr(null);
    setInfo(null);
    try {
      await providersApi.deleteKey(p.id);
      setInfo(`Cleared key for ${p.name}.`);
      await reload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : `failed to clear key for ${p.name}`);
    } finally {
      setSavingKey(null);
    }
  };

  const onTest = async (p: ProviderInfo, model?: string) => {
    setTesting(p.id);
    setTestResults((cur) => {
      const next = { ...cur };
      delete next[p.id];
      return next;
    });
    try {
      const m = (model ?? active.model) || p.default_model;
      const result = await providersApi.test(p.id, m);
      setTestResults((cur) => ({ ...cur, [p.id]: result }));
    } catch (e) {
      setTestResults((cur) => ({
        ...cur,
        [p.id]: { ok: false, error: e instanceof ApiError ? e.message : "test failed" },
      }));
    } finally {
      setTesting(null);
    }
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6 px-4 pb-12 pt-6 sm:px-6 sm:pt-8">
      <div className="flex items-end justify-between gap-4">
        <div>
          <Link
            to="/"
            className="text-xs text-muted transition-colors hover:text-accent"
          >
            ← Back to workspace
          </Link>
          <h1 className="mt-2 font-serif text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">
            LLM Providers
          </h1>
          <p className="mt-1.5 text-sm text-muted">
            Pick the active provider and model, and store API keys. Changes take
            effect on the next LLM call — no backend restart needed. Already
            in-flight streams keep using their current chat client.
          </p>
        </div>
      </div>

      {err && (
        <div className="rounded border border-danger/30 bg-danger/10 px-3 py-2 text-tx-xs text-danger">
          {err}
        </div>
      )}
      {info && (
        <div className="rounded border border-success/30 bg-success/10 px-3 py-2 text-tx-xs text-success">
          {info}
        </div>
      )}

      {/* ───── Active provider card ───────────────────────────────────── */}
      <section className="rounded-2xl border border-border bg-surface p-4 sm:p-5">
        <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-subtle">
          Active provider
        </h2>
        <p className="mt-1 text-tx-xs text-muted">
          Currently routing LLM calls to:{" "}
          <span className="font-mono text-text">
            {activeProvider ? activeProvider.name : active.provider}
          </span>{" "}
          <span className="text-muted">/</span>{" "}
          <span className="font-mono text-text">{active.model}</span>
        </p>

        <div className="mt-4 grid gap-3 sm:grid-cols-[1fr_1.5fr_auto]">
          <label className="block">
            <span className="mb-1 block text-tx-xs font-medium text-muted">
              Provider
            </span>
            <select
              className="field min-h-touch w-full text-sm"
              value={swapProvider}
              onChange={(e) => {
                const id = e.target.value;
                setSwapProvider(id);
                const def = providers.find((p) => p.id === id);
                if (def) setSwapModel(def.default_model);
              }}
            >
              {providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="mb-1 block text-tx-xs font-medium text-muted">
              Model
            </span>
            <input
              type="text"
              className="field min-h-touch w-full text-sm"
              value={swapModel}
              onChange={(e) => setSwapModel(e.target.value)}
              list="provider-default-models"
              placeholder={
                providers.find((p) => p.id === swapProvider)?.default_model ?? ""
              }
            />
            <datalist id="provider-default-models">
              {(providers.find((p) => p.id === swapProvider)?.default_models ?? []).map(
                (m) => (
                  <option key={m} value={m} />
                ),
              )}
            </datalist>
          </label>

          <div className="flex items-end">
            <button
              type="button"
              onClick={onSwap}
              disabled={
                savingSwap ||
                !swapProvider ||
                !swapModel.trim() ||
                (swapProvider === active.provider && swapModel.trim() === active.model)
              }
              className="min-h-touch w-full rounded-md border border-accent bg-accent/15 px-4 py-2 text-sm font-medium text-accent transition-colors hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto"
            >
              {savingSwap ? "Saving…" : "Set as active"}
            </button>
          </div>
        </div>
      </section>

      {/* ───── Per-provider cards ─────────────────────────────────────── */}
      <section className="space-y-3">
        <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-subtle">
          API keys
        </h2>
        <p className="text-tx-xs text-muted">
          Stored in the server's <span className="font-mono">app_settings</span>{" "}
          table. Values are kept verbatim (not encrypted at rest) — matching the
          security posture of <span className="font-mono">.env</span> keys.
        </p>

        {providers.map((p) => {
          const draft = drafts[p.id] ?? {
            api_key: "",
            base_url: p.default_base_url ?? "",
            show_key: false,
          };
          const isActive = p.id === active.provider;
          const testResult = testResults[p.id];
          const saving = savingKey === p.id;
          const isTesting = testing === p.id;
          return (
            <div
              key={p.id}
              className={`rounded-2xl border bg-surface p-4 sm:p-5 ${
                isActive ? "border-accent/50" : "border-border"
              }`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <h3 className="text-sm font-semibold text-text">{p.name}</h3>
                    {isActive && (
                      <span className="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 text-tx-xs font-medium text-accent">
                        Active
                      </span>
                    )}
                    <KeyStatePill hasKey={p.has_key} />
                  </div>
                  <p className="mt-1 text-tx-xs text-muted">
                    Default base URL:{" "}
                    <span className="font-mono text-text">
                      {p.default_base_url ?? "(provider default)"}
                    </span>
                  </p>
                </div>
              </div>

              <div className="mt-3 grid gap-3">
                <label className="block">
                  <span className="mb-1 block text-tx-xs font-medium text-muted">
                    API key
                  </span>
                  <div className="relative">
                    <input
                      type={draft.show_key ? "text" : "password"}
                      autoComplete="off"
                      spellCheck={false}
                      className="field min-h-touch w-full pr-20 font-mono text-sm"
                      value={draft.api_key}
                      onChange={(e) => setDraft(p.id, { api_key: e.target.value })}
                      placeholder={
                        p.has_key
                          ? "••••••••• (a key is already configured — type to replace)"
                          : "Paste your API key"
                      }
                    />
                    <button
                      type="button"
                      onClick={() => setDraft(p.id, { show_key: !draft.show_key })}
                      className="absolute inset-y-0 right-2 my-1 rounded border border-border bg-bg px-2 text-tx-xs text-muted hover:text-text"
                    >
                      {draft.show_key ? "Hide" : "Show"}
                    </button>
                  </div>
                </label>

                <label className="block">
                  <span className="mb-1 block text-tx-xs font-medium text-muted">
                    Base URL{" "}
                    <span className="font-normal text-muted">
                      (optional — leave blank to use the provider default)
                    </span>
                  </span>
                  <input
                    type="text"
                    autoComplete="off"
                    spellCheck={false}
                    className="field min-h-touch w-full font-mono text-sm"
                    value={draft.base_url}
                    onChange={(e) => setDraft(p.id, { base_url: e.target.value })}
                    placeholder={p.default_base_url ?? ""}
                  />
                </label>
              </div>

              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => onSaveKey(p)}
                  disabled={saving || !draft.api_key.trim()}
                  className="min-h-touch rounded-md border border-accent bg-accent/15 px-3 py-1.5 text-tx-xs font-medium text-accent transition-colors hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {saving ? "Saving…" : "Save key"}
                </button>
                {p.has_key && (
                  <button
                    type="button"
                    onClick={() => onClearKey(p)}
                    disabled={saving}
                    className="min-h-touch rounded-md border border-border bg-bg px-3 py-1.5 text-tx-xs text-muted transition-colors hover:border-danger/40 hover:text-danger disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Clear
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => onTest(p)}
                  disabled={isTesting || !p.has_key}
                  title={
                    p.has_key
                      ? `Send a 1-token ping to ${p.name}`
                      : `Save an API key first to test ${p.name}`
                  }
                  className="min-h-touch rounded-md border border-border bg-bg px-3 py-1.5 text-tx-xs text-muted transition-colors hover:border-accent/40 hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {isTesting ? "Testing…" : "Test connection"}
                </button>
                {testResult && (
                  <span
                    className={`text-tx-xs ${
                      testResult.ok ? "text-success" : "text-danger"
                    }`}
                  >
                    {testResult.ok
                      ? `✓ ${testResult.echo?.slice(0, 60) || "OK"}`
                      : `✗ ${testResult.error}`}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </section>

      {/* ───── Token pricing ───────────────────────────────────────────── */}
      <section className="space-y-3">
        <h2 className="text-sm font-semibold uppercase tracking-[0.16em] text-subtle">
          Token pricing
        </h2>
        <p className="text-tx-xs text-muted">
          Per-million-token USD prices used for cost accounting. Built-in
          defaults come from <span className="font-mono">memory/token_counter.py</span>;
          an admin override wins without a backend restart. Set your own
          numbers if you have negotiated rates or are running a private
          deployment.
        </p>

        {groupedPrices.length === 0 && (
          <div className="rounded-2xl border border-border bg-surface p-4 text-tx-xs text-muted">
            No pricing entries yet.
          </div>
        )}

        <div className="overflow-hidden rounded-2xl border border-border bg-surface">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-elevated/40 text-left text-tx-xs uppercase tracking-wider text-muted">
                <th className="px-3 py-2 font-medium">Model</th>
                <th className="px-2 py-2 text-right font-medium">Input</th>
                <th className="px-2 py-2 text-right font-medium">Output</th>
                <th className="px-2 py-2 text-right font-medium">Cache write</th>
                <th className="px-2 py-2 text-right font-medium">Cache read</th>
                <th className="px-3 py-2 font-medium">Source</th>
                <th className="px-3 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {groupedPrices.map(([providerId, rows]) => {
                const providerName =
                  providers.find((p) => p.id === providerId)?.name ?? "(other)";
                return (
                  <ProviderPriceGroup
                    key={providerId}
                    providerName={providerName}
                    rows={rows}
                    draftPrices={draftPrices}
                    savingPrice={savingPrice}
                    setPriceDraft={setPriceDraft}
                    onSavePrice={onSavePrice}
                    onResetPrice={onResetPrice}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function ProviderPriceGroup({
  providerName,
  rows,
  draftPrices,
  savingPrice,
  setPriceDraft,
  onSavePrice,
  onResetPrice,
}: {
  providerName: string;
  rows: ModelPrice[];
  draftPrices: Record<string, ModelPriceUpdate>;
  savingPrice: string | null;
  setPriceDraft: (model: string, patch: Partial<ModelPriceUpdate>) => void;
  onSavePrice: (m: ModelPrice) => void;
  onResetPrice: (m: ModelPrice) => void;
}) {
  return (
    <>
      <tr className="bg-elevated/20">
        <td
          colSpan={7}
          className="px-3 py-1.5 text-tx-xs font-semibold uppercase tracking-wider text-muted"
        >
          {providerName}
        </td>
      </tr>
      {rows.map((m) => {
        const d = draftPrices[m.model] ?? {
          input: m.input,
          output: m.output,
          cache_write: m.cache_write,
          cache_read: m.cache_read,
        };
        const saving = savingPrice === m.model;
        const dirty =
          Number(d.input) !== m.input ||
          Number(d.output) !== m.output ||
          Number(d.cache_write ?? 0) !== m.cache_write ||
          Number(d.cache_read ?? 0) !== m.cache_read;
        return (
          <tr key={m.model} className="border-t border-border/60">
            <td className="px-3 py-2 font-mono text-tx-xs text-text">
              {m.model}
            </td>
            <td className="px-2 py-2">
              <PriceInput
                value={d.input}
                onChange={(v) => setPriceDraft(m.model, { input: v })}
              />
            </td>
            <td className="px-2 py-2">
              <PriceInput
                value={d.output}
                onChange={(v) => setPriceDraft(m.model, { output: v })}
              />
            </td>
            <td className="px-2 py-2">
              <PriceInput
                value={d.cache_write ?? 0}
                onChange={(v) => setPriceDraft(m.model, { cache_write: v })}
              />
            </td>
            <td className="px-2 py-2">
              <PriceInput
                value={d.cache_read ?? 0}
                onChange={(v) => setPriceDraft(m.model, { cache_read: v })}
              />
            </td>
            <td className="px-3 py-2">
              {m.source === "override" ? (
                <span className="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 text-tx-xs font-medium text-accent">
                  Override
                </span>
              ) : (
                <span className="rounded-full border border-border bg-bg px-2 py-0.5 text-tx-xs font-medium text-muted">
                  Built-in
                </span>
              )}
            </td>
            <td className="px-3 py-2 text-right">
              <div className="flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => onSavePrice(m)}
                  disabled={saving || !dirty}
                  className="min-h-touch rounded-md border border-accent bg-accent/15 px-2.5 py-1 text-tx-xs font-medium text-accent transition-colors hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {saving ? "…" : "Save"}
                </button>
                {m.source === "override" && (
                  <button
                    type="button"
                    onClick={() => onResetPrice(m)}
                    disabled={saving}
                    className="min-h-touch rounded-md border border-border bg-bg px-2.5 py-1 text-tx-xs text-muted transition-colors hover:border-danger/40 hover:text-danger disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Reset
                  </button>
                )}
              </div>
            </td>
          </tr>
        );
      })}
    </>
  );
}

function PriceInput({
  value,
  onChange,
}: {
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <input
      type="number"
      step="0.01"
      min={0}
      inputMode="decimal"
      className="field min-h-touch w-20 text-right font-mono text-tx-xs"
      value={Number.isFinite(value) ? value : 0}
      onChange={(e) => {
        const v = parseFloat(e.target.value);
        onChange(Number.isFinite(v) ? v : 0);
      }}
    />
  );
}

function KeyStatePill({ hasKey }: { hasKey: boolean }) {
  if (hasKey) {
    return (
      <span className="rounded-full border border-success/30 bg-success/10 px-2 py-0.5 text-tx-xs font-medium text-success">
        Key set
      </span>
    );
  }
  return (
    <span className="rounded-full border border-warning/30 bg-warning/10 px-2 py-0.5 text-tx-xs font-medium text-warning">
      No key
    </span>
  );
}