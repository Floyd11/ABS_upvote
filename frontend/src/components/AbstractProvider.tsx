"use client";

import { AbstractWalletProvider } from "@abstract-foundation/agw-react";
import { abstract } from "viem/chains";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

export function AbstractProvider({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient());

  return (
    <QueryClientProvider client={queryClient}>
      <AbstractWalletProvider chain={abstract}>
        {children}
      </AbstractWalletProvider>
    </QueryClientProvider>
  );
}
