// Intentional: agwClient.account.address mutation required for session key context
import {
  createAbstractClient,
} from "@abstract-foundation/agw-client";
import { privateKeyToAccount } from "viem/accounts";
import { http, encodeFunctionData, Hex, createPublicClient } from "viem";
import { abstract } from "viem/chains";
import type { VoteRequest } from "./types";

const RPC_URL = process.env.RPC_URL ?? "https://api.mainnet.abs.xyz";

// Reviver: строки вида "123n" → BigInt(123)
function reviveBigInt(_key: string, value: unknown): unknown {
  if (typeof value === "string" && /^\d+n$/.test(value)) {
    return BigInt(value.slice(0, -1));
  }
  return value;
}

const VOTING_ABI = [
  {
    name: "voteForApp",
    type: "function",
    stateMutability: "nonpayable",
    inputs: [{ name: "appId", type: "uint256" }],
    outputs: [],
  },
] as const;

const DEFAULT_VOTING_CONTRACT =
  "0x3B50dE27506f0a8C1f4122A1e6F470009a76ce2A";

function extractErrorDetails(err: unknown): string {
  if (!(err instanceof Error)) return String(err);
  const parts: string[] = [err.message.slice(0, 200)];
  const e = err as Error & {
    cause?: { reason?: string; data?: string; message?: string };
    details?: string;
    shortMessage?: string;
  };
  if (e.shortMessage) parts.push("short: " + e.shortMessage);
  if (e.details) parts.push("details: " + e.details.slice(0, 200));
  if (e.cause?.reason) parts.push("reason: " + e.cause.reason);
  if (e.cause?.data) parts.push("data: " + e.cause.data);
  if (e.cause?.message) parts.push("cause: " + e.cause.message.slice(0, 200));
  return parts.join(" | ");
}

export async function voteForApp(req: VoteRequest): Promise<string> {
  const { walletAddress, sessionPrivateKey, appId, votingContract, sessionConfig } = req;

  // Восстанавливаем bigint из строк с суффиксом 'n'
  const session = JSON.parse(JSON.stringify(sessionConfig), reviveBigInt);

  // Аккаунт сессионного ключа (подписывает, но НЕ является from)
  const sessionSigner = privateKeyToAccount(sessionPrivateKey as Hex);

  console.log(
    `[voter] Voting: agw=${walletAddress.slice(0, 10)} session=${sessionSigner.address.slice(0, 10)} app_id=${appId}`
  );

  // 1. Создаём AbstractClient для AGW кошелька пользователя
  //    Это смарт-аккаунт клиент — он строит AA-транзакции через AGW контракт
  const agwClient = await createAbstractClient({
    signer: sessionSigner,        // временно используем signer для инициализации
    chain: abstract,
    transport: http(RPC_URL),
  });

  // Переопределяем адрес аккаунта на AGW адрес пользователя. 
  // Мы должны мутировать объект аккаунта, так как toSessionClient ожидает SmartAccount.
  // @ts-ignore
  agwClient.account.address = walletAddress as Hex;

  // 2. Создаём Session клиент через toSessionClient
  const sessionClient = agwClient.toSessionClient(sessionSigner, session);

  console.log(`[vote] Using account ${sessionClient.account.address} to writeContract`);

  let hash: Hex;
  try {
    hash = await sessionClient.writeContract({
      abi: VOTING_ABI,
      account: sessionClient.account,
      chain: abstract,
      address: (votingContract ?? DEFAULT_VOTING_CONTRACT) as Hex,
      functionName: "voteForApp",
      args: [BigInt(appId)],
    });
  } catch (err: unknown) {
    const details = extractErrorDetails(err);
    console.error(`[voter] writeContract failed: agw=${walletAddress.slice(0, 10)} app_id=${appId} | ${details}`);
    throw new Error(details);
  }


  console.log(`[voter] TX sent: ${hash.slice(0, 22)}`);

  // 4. Ждём подтверждения через публичный клиент
  const publicClient = createPublicClient({
    chain: abstract,
    transport: http(RPC_URL),
  });

  const receipt = await publicClient.waitForTransactionReceipt({
    hash: hash,
    timeout: 360_000,  // 6 минут
  });

  if (receipt.status !== "success") {
    // Симулируем вызов чтобы получить причину revert
    try {
      await publicClient.call({
        account: walletAddress as Hex,
        to: (votingContract ?? DEFAULT_VOTING_CONTRACT) as Hex,
        data: encodeFunctionData({
          abi: VOTING_ABI,
          functionName: "voteForApp",
          args: [BigInt(appId)],
        }),
      });
    } catch (simErr: unknown) {
      const reason = extractErrorDetails(simErr);
      console.error(`[voter] TX reverted simulation: ${reason}`);
      throw new Error(`TX reverted: ${hash} | ${reason}`);
    }
    throw new Error(`TX reverted: ${hash} | no simulation details`);
  }

  console.log(
    `[voter] ✓ Confirmed: agw=${walletAddress.slice(0, 10)} app_id=${appId} tx=${hash.slice(0, 22)}`
  );
  console.log(`[debug] agwClient.account.address:`, agwClient.account.address);
  console.log(`[debug] sessionClient.account.address:`, sessionClient.account.address);

  return hash;
}
