// 组合产品配置、语音演示和报告页面并持有跨页面会话。
import { useEffect, useState } from "react";
import { api } from "./api/client";
import { AppShell } from "./components/AppShell";
import { ProductEditor } from "./features/products/ProductEditor";
import { ReportsPage } from "./features/reports/ReportsPage";
import { StudioPage } from "./features/studio/StudioPage";
import { useVoiceSession } from "./features/studio/useVoiceSession";
import type { Product, Health, Session } from "./types/api";

/** 组合功能页面，并使语音会话在切换页面时继续运行。 */
export default function App() {
  const [page, setPage] = useState("studio");
  const [products, setProducts] = useState<Product[]>([]);
  const [selected, setSelected] = useState("");
  const [health, setHealth] = useState<Health>();
  const [error, setError] = useState("");
  const [sessions, setSessions] = useState<Session[]>([]);
  const product = products.find((p) => p.id === selected);
  const voice = useVoiceSession({ selected, product, health, setError });
  /**
   * 并行刷新产品与模型配置状态，仅在尚未选产品时设置默认项。
   */
  async function refresh() {
    const [p, h] = await Promise.all([
      api<Product[]>("/api/products"),
      api<Health>("/api/health"),
    ]);
    setProducts(p);
    setHealth(h);
    setSelected((id) => id || p[0]?.id || "");
  }

  useEffect(() => {
    void refresh().catch((e) => setError(String(e.message)));
  }, []);
  useEffect(() => {
    if (page === "reports")
      void api<Session[]>("/api/sessions")
        .then(setSessions)
        .catch((e) => setError(e.message));
  }, [page, voice.state]);
  return (
    <AppShell page={page} setPage={setPage} error={error} setError={setError}>
      {page === "studio" && (
        <StudioPage
          voice={voice}
          products={products}
          selected={selected}
          setSelected={setSelected}
          health={health}
          product={product}
          onConfigure={() => setPage("products")}
          onReport={() => setPage("reports")}
        />
      )}
      {page === "products" && (
        <ProductEditor
          products={products}
          selected={selected}
          setSelected={setSelected}
          onSaved={async (id) => {
            await refresh();
            setSelected(id);
          }}
          onError={setError}
        />
      )}
      {page === "reports" && (
        <ReportsPage
          products={products}
          sessions={sessions}
          report={voice.report}
          loadReport={voice.loadReport}
        />
      )}
    </AppShell>
  );
}
