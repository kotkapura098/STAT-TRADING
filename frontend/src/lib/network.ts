export interface NetworkStatus {
  ok: boolean;
  app_mode: "sample" | "paper" | "live";
  mode: "angel-one";
  detected: { at: number | null; v4: string; v6: string; err: { v4: string; v6: string } };
  outbound_ipv4: string;
  own: { v4: string; v6: string };
  registered: { v4: string; v6: string; at: number } | null;
  expected: string[];
  source: "" | "environment" | "config" | "unset";
  account_label: string;
  broker_sees: string | null;
  verdict: "ok" | "invalid" | "mismatch" | "unset" | "unchecked";
  message: string;
  bad: boolean;
}
