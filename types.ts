import { z } from "zod";

// ---------------------------------------------------------------------------
// Request schema
// ---------------------------------------------------------------------------

export const VoteRequestSchema = z.object({
  // AGW адрес пользователя — будет msg.sender в контракте голосования
  walletAddress: z
    .string()
    .regex(/^0x[0-9a-fA-F]{40}$/, "Invalid Ethereum address"),

  // Сырой приватный ключ сессии (0x + 64 hex)
  // Python расшифровывает его из БД и передаёт сюда по localhost
  sessionPrivateKey: z
    .string()
    .regex(/^0x[0-9a-fA-F]{64}$/, "Invalid private key format"),

  // appId для voteForApp(uint256)
  appId: z
    .number()
    .int()
    .positive(),

  // Опционально — можно переопределить контракт
  votingContract: z
    .string()
    .regex(/^0x[0-9a-fA-F]{40}$/)
    .optional(),
});

export type VoteRequest = z.infer<typeof VoteRequestSchema>;

// ---------------------------------------------------------------------------
// Response
// ---------------------------------------------------------------------------

export interface VoteResponse {
  txHash: string;
}

export interface ErrorResponse {
  error: string;
}
