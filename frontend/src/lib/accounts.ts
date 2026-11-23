/** Single Angel One account metadata. Secret values are never returned to the browser. */
export interface Account {
  id: string;
  label: string;
  source: "saved" | "env";
  client_code: string;
  api_key: string;
  has_pin: boolean;
  has_totp_secret: boolean;
  default: boolean;
  connected: boolean;
  editable: boolean;
}
export interface AccountsResp {
  ok: boolean;
  mode: "sample" | "paper" | "live";
  can_connect: boolean;
  connected: boolean;
  active: string | null;
  default: string;
  readable: boolean;
  problem: string;
  accounts: Account[];
}
export interface AccountForm {
  label: string;
  api_key: string;
  client_code: string;
  pin: string;
  totp_secret: string;
}
export const emptyForm = (): AccountForm => ({
  label: "My Angel One account", api_key: "", client_code: "", pin: "", totp_secret: "",
});
