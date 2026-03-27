import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { AbstractProvider } from "@/components/AbstractProvider";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Abstract Upvote Bot",
  description: "Automated voting for Abstract ecosystem apps",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className={inter.className}>
        <AbstractProvider>
          {children}
        </AbstractProvider>
      </body>
    </html>
  );
}
