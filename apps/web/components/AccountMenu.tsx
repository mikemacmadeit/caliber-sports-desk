"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

export function AccountMenu({ className = "" }: { className?: string }) {
  const pathname = usePathname();
  const [href, setHref] = useState("/login");
  const [label, setLabel] = useState("Log in");

  useEffect(() => {
    let cancel = false;
    fetch("/api/auth/me", { cache: "no-store" })
      .then((res) => res.json())
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
          setHref("/login");
          setLabel("Log in");
        }
      });
    return () => {
      cancel = true;
    };
  }, [pathname]);

  return (
    <Link href={href} className={`font-mono text-[10px] uppercase tracking-[0.22em] text-gold ${className}`}>
      {label}
    </Link>
  );
}
