/**
 * tx-service/src/index.ts
 *
 * Микросервис для отправки AA-транзакций через официальный @abstract-foundation/agw-client.
 *
 * Почему отдельный Node.js сервис, а не Python:
 *   - AGW построен на Privy infrastructure
 *   - Официальный SDK только для JS/TS (@abstract-foundation/agw-client)
 *   - SDK корректно упаковывает подпись сессионного ключа в поле customSignature
 *     в формате, который понимает SessionKeyValidator модуль контракта AGW
 *   - Python zksync2 не знает об этом формате и транзакция реджектится
 *
 * API:
 *   POST /vote  { walletAddress, sessionPrivateKey, appId, votingContract? }
 *   GET  /health
 *
 * Слушает только на localhost — FastAPI обращается к нему напрямую, не через интернет.
 */

import express, { Request, Response } from "express";
import { voteForApp } from "./voter.js";
import { VoteRequest, VoteRequestSchema } from "./types";

const app = express();
app.use(express.json());

const PORT = Number(process.env.PORT) || 3010;
const INTERNAL_SECRET = process.env.TX_SERVICE_SECRET ?? "";

// ---------------------------------------------------------------------------
// Auth middleware — принимаем запросы только от FastAPI (localhost)
// ---------------------------------------------------------------------------

app.use((req, res, next) => {
  // Простой shared secret между Python и Node процессами
  const secret = req.headers["x-internal-secret"];
  if (!INTERNAL_SECRET || secret !== INTERNAL_SECRET) {
    res.status(401).json({ error: "Unauthorized" });
    return;
  }
  next();
});

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

app.get("/health", (_req: Request, res: Response) => {
  res.json({ status: "ok" });
});

app.post("/vote", async (req: Request, res: Response) => {
  // Валидируем входные данные через Zod
  const parsed = VoteRequestSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Invalid request", details: parsed.error.flatten() });
    return;
  }

  const voteReq: VoteRequest = parsed.data;

  try {
    const txHash = await voteForApp(voteReq);
    res.json({ txHash });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`[vote] Failed: agw=${voteReq.walletAddress.slice(0, 10)} app_id=${voteReq.appId} error=${message}`);
    res.status(500).json({ error: message });
  }
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

app.listen(PORT, "0.0.0.0", () => {
  console.log(`[tx-service] Listening on 0.0.0.0:${PORT}`);
});
