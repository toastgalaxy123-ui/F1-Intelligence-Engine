"use client";

import { useEffect, useState } from "react";
import { LogOut, ShieldCheck } from "lucide-react";

export function PrivateSession() {
  const [label, setLabel] = useState("");
  const [offline, setOffline] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  useEffect(() => {
    const abort = new AbortController();
    let refreshId: ReturnType<typeof setTimeout> | undefined;
    async function refresh() {
      clearTimeout(refreshId);
      try {
        const res = await fetch("/auth/session", { signal: abort.signal, cache: "no-store" });
        if (res.status === 401) { window.location.replace("/auth/sign-in"); return; }
        if (!res.ok) throw new Error("Session unavailable");
        const account = await res.json();
        setLabel(account.label);
        setOffline(false);
        refreshId = setTimeout(refresh, Math.max(250, Math.min(15000, account.expires_in_s * 1000 + 250)));
      } catch {
        if (!abort.signal.aborted) {
          setOffline(true);
          refreshId = setTimeout(refresh, 5000);
        }
      }
    }
    void refresh();
    const restored = () => { void refresh(); };
    window.addEventListener("pageshow", restored);
    return () => { abort.abort(); clearTimeout(refreshId); window.removeEventListener("pageshow", restored); };
  }, []);

  async function signOut() {
    setSigningOut(true);
    try {
      const response = await fetch("/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (response.ok || response.status === 401) { window.location.replace("/auth/signed-out"); return; }
      setOffline(true);
    } catch {
      setOffline(true);
    } finally {
      setSigningOut(false);
    }
  }

  return <div className="private-session">
    <span><ShieldCheck size={14} /> {label || "Private access"}</span>
    {offline && <p role="status">Unable to confirm access. Retry when the service returns.</p>}
    <div><a href="/auth/login">Renew session</a>
      <button className="text-button" onClick={() => void signOut()} disabled={signingOut}><LogOut size={13} /> Sign out</button></div>
  </div>;
}
