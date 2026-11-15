import { AuthForm } from "@/components/AuthForm";
import { PageHeader } from "@/components/PageHeader";

export const metadata = { title: "Subscribe — Caliber" };

export default function SignupPage() {
  return (
    <div className="space-y-8">
      <PageHeader
        kicker="$19.99 a month"
        title="Subscribe"
        lede="Create an account, then start the monthly desk. Cancel any time. Finals stay on the public ledger."
      />
      <AuthForm mode="signup" />
    </div>
  );
}
