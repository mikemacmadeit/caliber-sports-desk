"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

export function AccountMenu({ className = "" }: { className?: string }) {
  const pathname = usePathname();
  const [href, setHref] = useState("/account");
  const [label, setLabel] = useState("Account");

  useEffect(() => {
    let cancel = false;
    fetch("/api/auth/me", { cache: "no-store" })
      .then((res) => { if (!res.ok) throw new Error("Account unavailable"); return res.json(); })
      .then((data) => {
        if (cancel) return;
        if (data.user?.admin) {
          setHref("/account");
          setLabel("Admin");
        } else if (data.user?.subscribed) {
          setHref("/account");
          setLabel("Member");
        } else if (data.user) {
          setHref("/account");
          setLabel("Subscribe");
        } else {
          setHref("/login");
          setLabel("Log in");
        }
      })
      .catch(() => {
        if (!cancel) {
          setHref("/account");
          setLabel("Account unavailable");
        }
      });
    return () => {
      cancel = true;
    };
  }, [pathname]);

  return (
    <Link href={href} className={`inline-flex min-h-11 items-center font-mono text-[10px] uppercase tracking-[0.22em] text-gold ${className}`}>
      {label}
    </Link>
  );
}
