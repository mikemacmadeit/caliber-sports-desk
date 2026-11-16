import { Suspense } from "react";
import { PageHeader } from "@/components/PageHeader";
import { ResetForm } from "@/components/ResetForm";

export const metadata = { title: "Choose a password — Caliber" };

export default function ResetPage() {
  return (
    <div className="space-y-8">
      <PageHeader kicker="Account" title="Choose a password" lede="This signs you in on this device." />
      <Suspense fallback={<p className="text-sm text-mute">Loading the reset link.</p>}>
        <ResetForm />
      </Suspense>
    </div>
  );
}
