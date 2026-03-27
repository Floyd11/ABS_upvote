"use client";

import { useState, useEffect, useRef } from "react";
import {
  useLoginWithAbstract,
  useAbstractClient,
} from "@abstract-foundation/agw-react";
import { useAccount } from "wagmi";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { LimitType } from "@abstract-foundation/agw-client/sessions";
import { toFunctionSelector, Hex, parseEther } from "viem";
import { abstract } from "viem/chains";

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8001";
const VOTING_CONTRACT = "0x3B50dE27506f0a8C1f4122A1e6F470009a76ce2A";

export default function Home() {
  const { login, logout } = useLoginWithAbstract();
  const { address, isConnected } = useAccount();
  const { data: agwClient } = useAbstractClient();

  const [step, setStep] = useState(1); // 1: Connect, 2: SIWE/JWT, 3: Activate
  const [jwt, setJwt] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [siweError, setSiweError] = useState<string | null>(null);
  const [status, setStatus] = useState<any>(null);
  const [history, setHistory] = useState<any[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  // Guard against double-execution of SIWE
  const siweRunning = useRef(false);

  // Step 2: SIWE Flow (Automatic after connection)
  useEffect(() => {
    if (isConnected && address && agwClient && step === 1 && !siweRunning.current) {
      handleSIWE();
    }
    // intentionally omit loading/siweRunning from deps — we use ref for that
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isConnected, address, agwClient]);

  async function handleSIWE() {
    if (!address || siweRunning.current) return;
    siweRunning.current = true;
    setSiweError(null);
    setLoading(true);
    try {
      // 1. Get Nonce
      const nonceResp = await fetch(`${BACKEND_URL}/auth/nonce?wallet=${address}`);
      if (!nonceResp.ok) throw new Error(`Nonce request failed: ${nonceResp.status}`);
      const { nonce, message } = await nonceResp.json();

      // 2. Sign Message (SIWE) — AGW signs without user popup
      if (!agwClient) throw new Error("Wallet client not ready. Please try again.");
      let signature: string;
      try {
        signature = await agwClient.signMessage({ message });
      } catch (signErr: any) {
        throw new Error(`Signature failed: ${signErr?.message ?? signErr}`);
      }

      // 3. Verify signature and get JWT
      const verifyResp = await fetch(`${BACKEND_URL}/auth/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ wallet_address: address, nonce, signature })
      });

      if (!verifyResp.ok) {
        const errBody = await verifyResp.text();
        throw new Error(`Verification failed (${verifyResp.status}): ${errBody}`);
      }

      const { access_token } = await verifyResp.json();
      setJwt(access_token);
      localStorage.setItem("upvote_jwt", access_token);
      setStep(3);

      // Fetch current status and history
      fetchStatus(access_token);
      fetchHistory(access_token);
    } catch (err: any) {
      console.error("Auth error:", err);
      // Show error in UI — do NOT logout, let user retry
      setSiweError(err?.message ?? "Authentication failed. Check console for details.");
      siweRunning.current = false; // allow retry
    } finally {
      setLoading(false);
    }
  }

  async function fetchStatus(token: string) {
    try {
      const resp = await fetch(`${BACKEND_URL}/status`, {
        headers: { "Authorization": `Bearer ${token}` }
      });
      if (resp.ok) {
        const data = await resp.json();
        setStatus(data);
      }
    } catch (err) {
      console.error("Status check failed:", err);
    }
  }

  async function fetchHistory(token: string) {
    setHistoryLoading(true);
    try {
      const resp = await fetch(`${BACKEND_URL}/history`, {
        headers: { "Authorization": `Bearer ${token}` }
      });
      if (resp.ok) {
        setHistory(await resp.json());
      }
    } catch (err) {
      console.error("History fetch failed:", err);
    } finally {
      setHistoryLoading(false);
    }
  }

  // Step 3: Activate Bot (Session Key Creation)
  async function handleActivate() {
    if (!agwClient || !address || !jwt) return;
    setLoading(true);
    try {
      // 1. Generate local key pair
      const sessionPrivateKey = generatePrivateKey();
      const sessionSigner = privateKeyToAccount(sessionPrivateKey);

      // 2. Create Session on Abstract
      const { session } = await agwClient.createSession({
        chain: abstract,
        account: address as Hex,
        session: {
          signer: sessionSigner.address,
          expiresAt: BigInt(Math.floor(Date.now() / 1000) + 60 * 60 * 24 * 60), // 60 days
          feeLimit: {
            limitType: LimitType.Lifetime,
            limit: parseEther("0.01"), // Max 0.01 ETH in fees per session (~60 days, required by mainnet registry)
            period: BigInt(0),
          },
          callPolicies: [
            {
              target: VOTING_CONTRACT,
              selector: toFunctionSelector("voteForApp(uint256)"),
              valueLimit: {
                limitType: LimitType.Unlimited,
                limit: BigInt(0),
                period: BigInt(0),
              },
              maxValuePerUse: BigInt(0),
              constraints: [],
            },
          ],
          transferPolicies: [],
        },
      });

      // 3. Send raw private key to backend
      const regResp = await fetch(`${BACKEND_URL}/register`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${jwt}`
        },
        body: JSON.stringify({
          wallet_address: address,
          session_key_enc: sessionPrivateKey, // raw hex, encrypted on backend
          session_config: session,            // session object from agwClient.createSession
          session_expires_at: new Date(Date.now() + 60 * 60 * 24 * 60 * 1000).toISOString(),
          voting_contract: VOTING_CONTRACT
        }, (key, value) => typeof value === 'bigint' ? value.toString() : value)
      });

      if (!regResp.ok) throw new Error("Registration failed");

      const regData = await regResp.json();
      setStatus(regData);
      alert("Bot activated successfully!");
      fetchHistory(jwt);

    } catch (err) {
      console.error("Activation error:", err);
      alert("Failed to activate bot. Check console for details.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main>
      <div className="container">
        <div className="header">
          <h1 className="title">Abstract Upvote</h1>
          <p className="subtitle">
            Automate your daily ecosystem contributions with zero security risk.
          </p>
        </div>

        <div className="card">
          {!isConnected ? (
            <button className="btn" onClick={login} disabled={loading}>
              {loading ? <div className="loader" /> : "Connect Wallet"}
            </button>
          ) : step === 3 ? (
            <>
              <div className={`status-badge ${status?.is_active ? 'active' : ''}`}>
                <div className="dot" />
                {status?.is_active ? "Bot is Active" : "Bot is Inactive"}
              </div>

              <button className="btn" onClick={handleActivate} disabled={loading}>
                {loading ? <div className="loader" /> : (status?.is_active ? "Renew Session" : "Activate Bot")}
              </button>

              {status && (
                <div style={{ fontSize: '0.8rem', color: '#555', marginTop: '1rem', width: '100%', textAlign: 'center' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.5rem' }}>
                    <span>Total Votes: <strong>{status.total_votes}</strong></span>
                    <span>Streak: <strong>{status.streak_days} days</strong></span>
                  </div>
                  {status.next_vote_in_hours && (
                    <div style={{ opacity: 0.7 }}>Next vote in ~{status.next_vote_in_hours}h</div>
                  )}
                </div>
              )}
            </>
          ) : siweError ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', alignItems: 'center', width: '100%' }}>
              <p style={{ color: '#ff4f4f', fontSize: '0.8rem', textAlign: 'center', wordBreak: 'break-word' }}>
                ⚠️ {siweError}
              </p>
              <button className="btn" onClick={() => { siweRunning.current = false; setSiweError(null); handleSIWE(); }}>
                Retry Sign-In
              </button>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '0.5rem' }}>
              <div className="loader" />
              <p style={{ fontSize: '0.75rem', opacity: 0.4 }}>Signing in with wallet…</p>
            </div>
          )}
        </div>

        {isConnected && status?.is_active && (
          <div className="card" style={{ padding: '2rem', marginTop: '1rem', gap: '1rem', alignItems: 'flex-start' }}>
            <h3 style={{ fontSize: '1rem', opacity: 0.6 }}>Voting History</h3>
            <div style={{ width: '100%', fontSize: '0.85rem' }}>
              {historyLoading ? (
                <div style={{ textAlign: 'center', padding: '1rem' }}>Loading logs...</div>
              ) : history.length === 0 ? (
                <div style={{ textAlign: 'center', opacity: 0.4, padding: '1rem' }}>No votes yet.</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                  {history.map((log: any, i: number) => (
                    <div key={i} style={{ display: 'flex', justifyContent: 'space-between', borderBottom: '1px solid #111', paddingBottom: '0.5rem' }}>
                      <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <span>App ID #{log.app_id}</span>
                        <span style={{ fontSize: '0.7rem', opacity: 0.5 }}>{new Date(log.voted_at).toLocaleString()}</span>
                      </div>
                      <div style={{ color: log.status === 'ok' ? '#00ff88' : '#ff4444' }}>
                        {log.status === 'ok' ? '✓ Success' : '✗ Failed'}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {isConnected && (
          <button
            onClick={() => { logout(); setStep(1); setJwt(null); setHistory([]); setSiweError(null); siweRunning.current = false; }}
            className="status-badge"
            style={{ border: 'none', cursor: 'pointer', marginTop: '1rem', opacity: 0.5 }}
          >
            Disconnect {address?.slice(0, 6)}...{address?.slice(-4)}
          </button>
        )}
      </div>
    </main>
  );
}
