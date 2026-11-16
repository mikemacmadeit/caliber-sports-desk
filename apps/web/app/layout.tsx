import type { Metadata, Viewport } from "next";
import { Suspense } from "react";
import "@fontsource/barlow-condensed/latin-500.css";
import "@fontsource/barlow-condensed/latin-600.css";
import "@fontsource/barlow-condensed/latin-700.css";
import "@fontsource-variable/outfit/wght.css";
import "@fontsource-variable/jetbrains-mono/wght.css";
import { ClientTelemetry } from "@/components/ClientTelemetry";
import { AppShell } from "@/components/AppShell";
import "./globals.css";

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export const metadata: Metadata = {
  title: "Caliber — NFL & FBS probability desk",
  description:
    "Private research desk. Timestamped probabilities, no-vig baselines, and a call that is allowed to be no bet. Not betting advice.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="font-sans antialiased">
        <a href="#main-content" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:bg-gold focus:p-3 focus:text-ink-950">Skip to content</a>
        <Suspense fallback={<main className="px-5 py-8 lg:px-10 lg:py-10">{children}</main>}>
          <ClientTelemetry />
          <AppShell>{children}</AppShell>
        </Suspense>
      </body>
    </html>
  );
}
