import { useState, type FormEvent } from "react";
import { api } from "../lib/api";
import { connectBroker, disconnectBroker, toast } from "../lib/app";
import { emptyForm, type Account, type AccountForm, type AccountsResp } from "../lib/accounts";
import { usePoll } from "../lib/store";
import { Modal } from "../components/ui";

function AccountDialog({ edit, onClose, onDone }: { edit: Account | null; onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState<AccountForm>({ ...emptyForm(), label: edit?.label ?? "My Angel One account" });
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = (patch: Partial<AccountForm>) => setForm({ ...form, ...patch });
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true); setError("");
    try {
      await api(edit ? `/accounts/${edit.id}` : "/accounts", {
        method: edit ? "PUT" : "POST", body: form,
      });
      toast(edit ? "Angel One account updated" : "Angel One account saved");
      onDone();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }
  const type = show ? "text" : "password";
  return <Modal title={edit ? "Edit Angel One account" : "Add Angel One account"} onClose={() => !busy && onClose()}
    footer={<><button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
      <button className="btn primary" form="angel-account-form" disabled={busy}>{busy ? "Saving…" : "Save account"}</button></>}>
    <form id="angel-account-form" className="stack" onSubmit={submit} autoComplete="off">
      <label className="field"><span>Account name</span><input className="input" maxLength={40} value={form.label} onChange={e=>set({label:e.target.value})}/></label>
      <label className="field"><span>Angel One API Key</span><input className="input" type={type} autoComplete="off" value={form.api_key} onChange={e=>set({api_key:e.target.value})} placeholder={edit ? "Leave blank to keep saved key" : "Enter API Key"}/></label>
      <label className="field"><span>Client Code</span><input className="input" autoComplete="off" value={form.client_code} onChange={e=>set({client_code:e.target.value})} placeholder={edit ? "Leave blank to keep saved client code" : "Angel One Client Code"}/></label>
      <label className="field"><span>Trading PIN</span><input className="input" type={type} inputMode="numeric" autoComplete="off" maxLength={8} value={form.pin} onChange={e=>set({pin:e.target.value.replace(/\D/g,"")})} placeholder={edit ? "Leave blank to keep saved PIN" : "Enter PIN"}/></label>
      <label className="field"><span>TOTP setup secret (optional)</span><input className="input" type={type} autoComplete="off" value={form.totp_secret} onChange={e=>set({totp_secret:e.target.value})} placeholder={edit ? "Leave blank to keep saved secret" : "Authenticator setup key"}/></label>
      <label className="check"><input type="checkbox" checked={show} onChange={e=>setShow(e.target.checked)}/><span>Show secret fields while typing</span></label>
      <p className="hint">Credentials are encrypted on the server using your dashboard password. Never paste your API Key, PIN or TOTP secret into chat or screenshots.</p>
      {error && <div className="err" role="alert">{error}</div>}
    </form>
  </Modal>;
}

function ConnectDialog({ account, mode, onClose, onDone }: { account: Account; mode: string; onClose: () => void; onDone: () => void }) {
  const [totp,setTotp]=useState(""); const [confirm,setConfirm]=useState(false);
  const [busy,setBusy]=useState(false); const [error,setError]=useState("");
  async function submit(e:FormEvent) {
    e.preventDefault(); if(busy)return; setBusy(true);setError("");
    try { await connectBroker(totp.trim(),account.id,confirm); toast("Connected to Angel One");onDone(); }
    catch(err){setError((err as Error).message);} finally{setBusy(false);}
  }
  return <Modal title="Connect Angel One" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" form="angel-connect-form" disabled={busy}>{busy?"Connecting…":"Connect"}</button></>}>
    <form id="angel-connect-form" className="stack" onSubmit={submit}>
      <p>Connect <b>{account.label}</b> ({account.client_code}).</p>
      {!account.has_totp_secret && <label className="field"><span>Current 6-digit TOTP</span><input className="input num" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={totp} onChange={e=>setTotp(e.target.value.replace(/\D/g,""))}/></label>}
      {account.has_totp_secret && <p className="hint">The saved TOTP setup secret will generate the login code on the server.</p>}
      {mode==="live" && <label className="check"><input type="checkbox" checked={confirm} onChange={e=>setConfirm(e.target.checked)}/><span><b>LIVE mode:</b> I understand that orders submitted by this terminal may be real orders on this Angel One account.</span></label>}
      {error && <div className="err" role="alert">{error}</div>}
    </form>
  </Modal>;
}

export function AccountsPanel() {
  const {data,error,reload}=usePoll<AccountsResp>("/accounts",5000);
  const [dialog,setDialog]=useState<"add"|"edit"|"connect"|"disconnect"|null>(null);
  const [busy,setBusy]=useState(false);
  const account=data?.accounts?.[0] ?? null;
  const close=()=>setDialog(null);
  const done=()=>{setDialog(null);reload();};
  async function disconnect() {
    setBusy(true);
    try { await disconnectBroker(account?.id); toast("Disconnected from Angel One"); done(); }
    catch(err){toast((err as Error).message,"err");} finally{setBusy(false);}
  }
  return <section className="panel" style={{marginBottom:12}}>
    <div className="panel-h">Angel One account</div>
    <div className="panel-b stack">
      {error && !data && <div className="notice bad">{error}</div>}
      {data && !data.readable && <div className="notice bad">{data.problem}</div>}
      {data && data.mode==="sample" && <div className="notice">Sample mode uses simulated data. Switch to Paper mode to connect Angel One for live market data with simulated orders.</div>}
      {data && data.mode==="live" && <div className="notice warn">Live order placement is locked while the Angel One migration is being verified. Switch to Paper mode to test the account connection safely.</div>}
      {account ? <>
        <div className="acct"><div className="acct-main"><div className="acct-title"><b>{account.label}</b>
          {account.connected && <span className="chip up">Connected</span>}
          {account.source==="env" && <span className="chip flat">Environment</span>}</div>
          <div className="acct-sub">Client Code {account.client_code} · API Key {account.api_key} · {account.has_totp_secret?"TOTP secret saved":"TOTP required at connect"}</div>
        </div><div className="row wrap">
          {account.connected ? <button className="btn sm" disabled={busy} onClick={()=>setDialog("disconnect")}>Disconnect</button>
            : <button className="btn primary sm" disabled={!data?.can_connect} onClick={()=>setDialog("connect")}>Connect</button>}
          {account.editable && <button className="btn sm" disabled={busy||account.connected} onClick={()=>setDialog("edit")}>Edit</button>}
        </div></div>
      </> : <p className="hint">No Angel One account configured. Add your API Key, Client Code and PIN to get started.</p>}
      {!account && <button className="btn" disabled={!data?.readable} onClick={()=>setDialog("add")}>+ Add Angel One account</button>}
      <p className="hint">Only one Angel One account is supported. The PIN and API credentials stay encrypted on the server and are never returned to the browser.</p>
      {data?.mode==="live" && <div className="notice warn">LIVE mode can submit real orders. Verify the account, quantity and risk limits before enabling it.</div>}
    </div>
    {dialog==="add" && <AccountDialog edit={null} onClose={close} onDone={done}/>}
    {dialog==="edit" && account && <AccountDialog edit={account} onClose={close} onDone={done}/>}
    {dialog==="connect" && account && <ConnectDialog account={account} mode={data?.mode??"paper"} onClose={close} onDone={done}/>}
    {dialog==="disconnect" && <Modal title="Disconnect Angel One?" onClose={close} footer={<><button className="btn" onClick={close}>Cancel</button><button className="btn danger solid" disabled={busy} onClick={disconnect}>Disconnect</button></>}>
      <p>The terminal will stop receiving market data and broker account updates. Open positions and orders remain at Angel One. Stop all running strategies before disconnecting.</p>
    </Modal>}
  </section>;
}
