import { AuthForm } from "@/components/AuthForm";
import { PageHeader } from "@/components/PageHeader";

export const metadata = { title: "Log in — Caliber" };

export default function LoginPage() {
  return (
    <div className="space-y-8">
      <PageHeader kicker="Account" title="Log in" lede="The ledger is free. The call before kickoff is $19.99 a month." />
      <AuthForm mode="login" />
    </div>
  );
}
