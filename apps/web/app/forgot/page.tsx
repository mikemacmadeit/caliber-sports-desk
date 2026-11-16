import { ForgotForm } from "@/components/ForgotForm";
import { PageHeader } from "@/components/PageHeader";

export const metadata = { title: "Reset password — Caliber" };

export default function ForgotPage() {
  return (
    <div className="space-y-8">
      <PageHeader kicker="Account" title="Reset password" lede="We’ll email a link that expires in one hour." />
      <ForgotForm />
    </div>
  );
}
