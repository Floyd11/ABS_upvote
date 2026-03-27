"use client";

import { useState, useEffect } from "react";
import { 
  useLoginWithAbstract, 
  useAbstractClient, 
} from "@abstract-foundation/agw-react";
import { useAccount } from "wagmi";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { LimitType } from "@abstract-foundation/agw-client/sessions";
import { toFunctionSelector, Hex } from "viem";
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
  const [status, setStatus] = useState<any>(null);

  // Step 2: SIWE Flow (Automatic after connection)
  useEffect(() => {
    if (isConnected && address && step === 1) {
      handleSIWE();
    }
  }, [isConnected, address]);

  async function handleSIWE() {
    if (!address) return;
    setLoading(true);
    try {
      // 1. Get Nonce
      const nonceResp = await fetch(`${BACKEND_URL}/auth/nonce?wallet=${address}`);
      const { nonce, message } = await nonceResp.json();

      // 2. Sign Message (SIWE)
      if (!agwClient) throw new Error("Client not ready");
      const signature = await agwClient.signMessage({ message });

      // 3. Verify and get JWT
      const verifyResp = await fetch(`${BACKEND_URL}/auth/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          wallet_address: address,
          nonce,
          signature
        })
      });
      
      if (!verifyResp.ok) throw new Error("Verification failed");
      
      const { access_token } = await verifyResp.json();
      setJwt(access_token);
      localStorage.setItem("upvote_jwt", access_token);
      setStep(3);
      
      // Check current project status
      fetchStatus(access_token);
    } catch (err) {
      console.error("Auth error:", err);
      // Reset if failed
      logout();
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
            limitType: LimitType.Unlimited,
            limit: BigInt(0),
            period: BigInt(0),
          },
          callPolicies: [
            {
              target: VOTING_CONTRACT,
              selector: toFunctionSelector("voteForApp(uint256)"),
              valueLimit: {
                limitType: LimitType.Unlimited,
                limit: BigInt(0), // user requested valueLimit: 0n
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
        })
      });

      if (!regResp.ok) throw new Error("Registration failed");
      
      const regData = await regResp.json();
      setStatus(regData);
      alert("Bot activated successfully!");

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
                <div style={{ fontSize: '0.8rem', color: '#555', marginTop: '1rem' }}>
                  Total Votes: {status.total_votes} | Current Epoch: {status.current_epoch}
                </div>
              )}
            </>
          ) : (
            <div className="loader" />
          )}
        </div>
        
        {isConnected && (
          <button 
            onClick={() => { logout(); setStep(1); setJwt(null); }} 
            className="status-badge" 
            style={{ border: 'none', cursor: 'pointer', marginTop: '1rem' }}
          >
            Disconnect {address?.slice(0, 6)}...{address?.slice(-4)}
          </button>
        )}
      </div>
    </main>
  );
}
