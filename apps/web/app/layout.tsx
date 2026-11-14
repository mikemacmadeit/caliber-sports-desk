import type { Metadata, Viewport } from "next";
import { Suspense } from "react";
import "@fontsource/barlow-condensed/latin-500.css";
import "@fontsource/barlow-condensed/latin-600.css";
import "@fontsource/barlow-condensed/latin-700.css";
import "@fontsource-variable/outfit/wght.css";
import "@fontsource-variable/jetbrains-mono/wght.css";
import { AppShell } from "@/components/AppShell";
import "./globals.css";
import { PipelineStatus } from "@/components/PipelineStatus";

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
        <Suspense fallback={<main className="px-5 py-8 lg:px-10 lg:py-10">{children}</main>}>
          <AppShell><PipelineStatus />{children}</AppShell>
        </Suspense>
      </body>
    </html>
  );
}
