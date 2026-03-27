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

interface BotStatus {
  is_active: boolean;
  total_votes: number;
  streak_days: number;
  next_vote_in_hours: number | null;
  week_queue: number[];
  week_app_index: number;
  current_epoch: number;
  today_app_id: number | null;
  last_voted_at: string | null;
}

interface VoteLog {
  app_id: number;
  epoch: number;
  tx_hash: string | null;
  voted_at: string;
  status: "ok" | "fail";
  error_msg: string | null;
}

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
  const [status, setStatus] = useState<BotStatus | null>(null);
  const [history, setHistory] = useState<VoteLog[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [activateError, setActivateError] = useState<string | null>(null);

  // Guard against double-execution of SIWE
  const siweRunning = useRef(false);

  // 1. Восстановление JWT из localStorage при перезагрузке страницы
  useEffect(() => {
    if (!isConnected) return;
    const saved = localStorage.getItem("upvote_jwt");
    if (saved) {
      setJwt(saved);
      setStep(3);
      fetchStatus(saved);
      fetchHistory(saved);
    }
  }, [isConnected]);

  // 2. Сброс состояния при дисконнекте
  useEffect(() => {
    if (!isConnected) {
      setStep(1);
      setJwt(null);
      setSiweError(null);
      siweRunning.current = false;
    }
  }, [isConnected]);

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

      // 2. Create Session Configuration
      const sessionConfig = {
        signer: sessionSigner.address,
        expiresAt: BigInt(Math.floor(Date.now() / 1000) + 60 * 60 * 24 * 60), // 60 days
        feeLimit: {
          limitType: LimitType.Lifetime,
          limit: parseEther("0.01"), // Max 0.01 ETH in fees per session (~60 days, required by mainnet registry)
          period: BigInt(0),
        },
        callPolicies: [
          {
            target: VOTING_CONTRACT as `0x${string}`,
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
      };

      // 2.5 Create Session onchain
      await agwClient.createSession({
        chain: abstract,
        account: address as Hex,
        session: sessionConfig,
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
          session_config: sessionConfig,
          session_expires_at: new Date(Date.now() + 60 * 60 * 24 * 60 * 1000).toISOString(),
          voting_contract: VOTING_CONTRACT
        }, (key, value) => typeof value === 'bigint' ? value.toString() + 'n' : value)
      });

      if (!regResp.ok) {
        const errorData = await regResp.json().catch(() => ({}));
        const detail = errorData.detail 
          ? (typeof errorData.detail === 'string' ? errorData.detail : JSON.stringify(errorData.detail))
          : "Registration failed";
        throw new Error(detail);
      }

      const regData = await regResp.json();
      setStatus(regData);
      setActivateError(null);
      await fetchStatus(jwt);
      await fetchHistory(jwt);

    } catch (err: any) {
      console.error("Activation error:", err);
      setActivateError(err?.message ?? "Activation failed. Check console for details.");
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
              {activateError && (
                <p style={{
                  color: '#ff4f4f', fontSize: '0.8rem', textAlign: 'center',
                  wordBreak: 'break-word', marginTop: '0.5rem'
                }}>
                  ⚠️ {activateError}
                </p>
              )}

              {status && (
                <div style={{ fontSize: '0.8rem', color: '#555', marginTop: '1rem', width: '100%', textAlign: 'center' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.5rem' }}>
                    <span>Total Votes: <strong>{status.total_votes}</strong></span>
                    <span>Streak: <strong>{status.streak_days} days</strong></span>
                  </div>
                  {status.next_vote_in_hours !== null && (
                    <div style={{ opacity: 0.7 }}>Next vote in ~{status.next_vote_in_hours}h</div>
                  )}
                </div>
              )}
            </>
          ) : step === 1 ? (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '1rem', width: '100%' }}>
              <p style={{ fontSize: '0.8rem', color: '#888', textAlign: 'center' }}>
                Connected: {address?.slice(0, 6)}...{address?.slice(-4)}
              </p>
              {siweError && (
                <p style={{ color: '#ff4f4f', fontSize: '0.8rem', textAlign: 'center', wordBreak: 'break-word' }}>
                  ⚠️ {siweError}
                </p>
              )}
              <button
                className="btn"
                onClick={() => { siweRunning.current = false; setSiweError(null); handleSIWE(); }}
                disabled={loading}
              >
                {loading ? <div className="loader" /> : "Sign In with Wallet"}
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
                  {history.map((log: VoteLog, i: number) => (
                    <div key={i} style={{ display: 'flex', justifyContent: 'space-between', borderBottom: '1px solid #111', paddingBottom: '0.5rem' }}>
                      <div style={{ display: 'flex', flexDirection: 'column' }}>
                        <span>App ID #{log.app_id}</span>
                        <span style={{ fontSize: '0.7rem', opacity: 0.5 }}>{new Date(log.voted_at).toLocaleString()}</span>
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
                        <div style={{ color: log.status === 'ok' ? '#00ff88' : '#ff4444' }}>
                          {log.status === 'ok' ? '✓ Success' : '✗ Failed'}
                        </div>
                        {log.status === 'ok' && log.tx_hash && (
                          <a
                            href={`https://abscan.org/tx/${log.tx_hash}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            style={{ fontSize: '0.65rem', opacity: 0.5, color: '#888' }}
                          >
                            {log.tx_hash.slice(0, 8)}...
                          </a>
                        )}
                        {log.status === 'fail' && log.error_msg && (
                          <div style={{ fontSize: '0.65rem', color: '#ff4444', opacity: 0.7, maxWidth: '180px', textAlign: 'right', wordBreak: 'break-word' }}>
                            {log.error_msg.slice(0, 80)}
                          </div>
                        )}
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
            onClick={() => {
              logout();
              localStorage.removeItem("upvote_jwt");
              setStep(1);
              setJwt(null);
              setStatus(null);
              setHistory([]);
              setSiweError(null);
              setActivateError(null);
              siweRunning.current = false;
            }}
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
