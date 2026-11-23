import { useState } from "react";
import { api } from "../lib/api";
import { toast } from "../lib/app";
import { usePoll } from "../lib/store";
import type { NetworkStatus } from "../lib/network";

export function StaticIpPanel() {
  const { data, error, loading, reload } = usePoll<NetworkStatus>("/api/network", 15000);
  const [busy, setBusy] = useState(false);

  const detect = async () => {
    setBusy(true);
    try {
      await api("/api/network/detect", { method: "POST" });
      reload();
      toast("Outbound IP check refreshed", "ok");
    } catch (e) {
      toast(String(e), "err");
    } finally {
      setBusy(false);
    }
  };

  const expected = data?.expected ?? [];
  const detected = data?.outbound_ipv4 || data?.detected?.v4 || "";

  return (
    <section className="panel" style={{ marginBottom: 12 }}>
      <div className="panel-h">Angel One static outbound IP</div>
      <div className="panel-b">
        <p>Angel One requires API order placement, modification and cancellation to originate from a registered static public IPv4 address. Paper trading and market-data login do not require this order-IP check.</p>
        <div className={data?.verdict === "ok" ? "notice good" : "notice bad"}>
          <b>{loading ? "Checking server outbound IP…" : data?.verdict?.toUpperCase() ?? "STATUS UNKNOWN"}</b>
          <p>{data?.message ?? error ?? "Waiting for network status."}</p>
        </div>
        <div className="kv-grid">
          <div><span>Detected server outbound IPv4</span><b>{detected || "Not detected"}</b></div>
          <div><span>Configured Angel One allowlist</span><b>{expected.length ? expected.join(", ") : "Not configured"}</b></div>
          <div><span>Configuration source</span><b>{data?.source || "Not set"}</b></div>
        </div>
        <button className="btn sm" onClick={detect} disabled={busy}>{busy ? "Checking…" : "Recheck outbound IP"}</button>
        <p className="hint">Set <code>ANGEL_STATIC_IP</code> on the server to the registered primary IPv4 and optional secondary backup IPv4, separated by a comma. You can use <code>static_ip</code> in config.yaml as a fallback. The terminal checks that its detected outbound IP is in this list; it cannot verify Angel One’s server-side registration.</p>
        <p className="hint">Render Dedicated IP sets provide three IPv4 addresses, while Angel One documents a primary and optional secondary IP. Do not assume all three fit the broker allowlist. Use a single fixed egress IP or a proxy with a primary/secondary pair (for example, Render's QuotaGuard Static IP), and ensure every possible order egress IP is registered. Do not enter your laptop’s IP or the service’s inbound IP.</p>
        <p className="hint">Live orders remain locked until broker verification is complete. A missing or mismatched IP blocks all Angel One place, modify and cancel calls.</p>
      </div>
    </section>
  );
}
