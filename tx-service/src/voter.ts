/**
 * tx-service/src/voter.ts
 *
 * Отправка voteForApp(uint256) через AGW Session Key.
 *
 * Ключевые моменты:
 *
 * 1. createSessionClient() из @abstract-foundation/agw-client создаёт
 *    клиент, который:
 *    - знает что from = walletAddress (AGW смарт-аккаунт)
 *    - подписывает транзакцию сессионным ключом
 *    - упаковывает подпись в customSignature в формате, который ожидает
 *      SessionKeyValidator модуль контракта AGW:
 *      abi.encode(address validatorModule, bytes signature)
 *
 * 2. msg.sender в контракте голосования = walletAddress пользователя (AGW)
 *    → пользователь получает XP и очки
 *
 * 3. Газ платит AGW кошелёк пользователя (ETH на Abstract mainnet)
 */

import {
  createSessionClient,
  toSmartAccount,
} from "@abstract-foundation/agw-client";
import { privateKeyToAccount } from "viem/accounts";
import { createPublicClient, http, encodeFunctionData, Hex } from "viem";
import { abstractMainnet } from "viem/chains";
import type { VoteRequest } from "./types";

// ---------------------------------------------------------------------------
// Chain & RPC
// ---------------------------------------------------------------------------

const RPC_URL = process.env.ABSTRACT_RPC_URL ?? "https://api.mainnet.abs.xyz";

const publicClient = createPublicClient({
  chain: abstractMainnet,
  transport: http(RPC_URL),
});

// ---------------------------------------------------------------------------
// ABI (минимальный)
// ---------------------------------------------------------------------------

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
  "0x3B50dE27506f0a8C1f4122A1e6F470009a76ce2A" as const;

// ---------------------------------------------------------------------------
// Core function
// ---------------------------------------------------------------------------

export async function voteForApp(req: VoteRequest): Promise<string> {
  const {
    walletAddress,
    sessionPrivateKey,
    appId,
    votingContract = DEFAULT_VOTING_CONTRACT,
  } = req;

  // Аккаунт сессионного ключа (подписывает транзакцию)
  const sessionSigner = privateKeyToAccount(sessionPrivateKey as Hex);

  // AGW смарт-аккаунт пользователя (from / msg.sender)
  const agwAccount = toSmartAccount({
    address: walletAddress as Hex,
    client: publicClient,
  });

  // Создаём Session Key клиент
  // Под капотом он:
  //   1. Строит транзакцию типа 113 (ZKsync EIP-712)
  //   2. Подписывает её sessionSigner
  //   3. Упаковывает customSignature = abi.encode(sessionKeyValidator, signature)
  //   4. Отправляет через eth_sendRawTransaction
  const sessionClient = createSessionClient({
    account: agwAccount,
    signer: sessionSigner,
    chain: abstractMainnet,
    transport: http(RPC_URL),
  });

  console.log(
    `[voter] Voting: agw=${walletAddress.slice(0, 10)} session=${sessionSigner.address.slice(0, 10)} app_id=${appId}`
  );

  // Отправляем транзакцию — SDK сам упакует всё правильно
  const txHash = await sessionClient.sendTransaction({
    to: votingContract as Hex,
    data: encodeFunctionData({
      abi: VOTING_ABI,
      functionName: "voteForApp",
      args: [BigInt(appId)],
    }),
  });

  console.log(`[voter] TX sent: ${txHash.slice(0, 22)}`);

  // Ждём подтверждения
  const receipt = await publicClient.waitForTransactionReceipt({
    hash: txHash,
    timeout: 360_000, // 6 минут — с запасом на нагрузку сети
  });

  if (receipt.status !== "success") {
    throw new Error(`TX reverted: ${txHash}`);
  }

  console.log(
    `[voter] ✓ Confirmed: agw=${walletAddress.slice(0, 10)} app_id=${appId} tx=${txHash.slice(0, 22)}`
  );

  return txHash;
}
