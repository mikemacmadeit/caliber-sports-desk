import type { Metadata } from "next";
import { Suspense } from "react";
import { Barlow_Condensed, JetBrains_Mono, Outfit } from "next/font/google";
import { AppShell } from "@/components/AppShell";
import "./globals.css";

const display = Barlow_Condensed({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-display",
});
const sans = Outfit({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-sans",
});
const mono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "Caliber — NFL & FBS probability desk",
  description:
    "Private research desk. Timestamped probabilities, no-vig baselines, and a call that is allowed to be no bet. Not betting advice.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${display.variable} ${sans.variable} ${mono.variable} font-sans antialiased`}>
        <Suspense fallback={<main className="px-5 py-8 lg:px-10 lg:py-10">{children}</main>}>
          <AppShell>{children}</AppShell>
        </Suspense>
      </body>
    </html>
  );
}
