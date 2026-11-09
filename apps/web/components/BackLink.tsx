"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { pageLabel, previousPage } from "@/lib/navHistory";

export function BackLink() {
  const router = useRouter();
  const pathname = usePathname();
  const search = useSearchParams().toString();
  const href = search ? `${pathname}?${search}` : pathname;
  const [previous, setPrevious] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setPrevious(previousPage(href));
    setReady(true);
  }, [href]);

  const label = previous ? pageLabel(previous) : ready ? "Board" : "Back";

  return (
    <button
      type="button"
      onClick={() => {
        if (previous) {
          router.back();
          return;
        }
        router.push("/");
      }}
      className="font-mono text-[10px] uppercase tracking-[0.28em] text-mute hover:text-gold"
    >
      ← {label}
    </button>
  );
}
