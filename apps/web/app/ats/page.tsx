import { AtsBoard, type AtsRow } from "@/components/AtsBoard";
import { LeagueToggle } from "@/components/LeagueToggle";
import { PageHeader } from "@/components/PageHeader";
import { apiGet } from "@/lib/api";

type AtsPayload = {
  as_of: string;
  disclaimer: string;
  rows: AtsRow[];
};

export default async function AtsPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string; sort?: string; side?: string; view?: string; result?: string; book?: string }>;
}) {
  const { league = "all", sort = "kickoff", side = "all", view, result, book } = await searchParams;
  let data: AtsPayload;
  try {
    data = await apiGet<AtsPayload>(`/v1/ats?league=${league}&sort=${sort}&side=${side}`, 120000);
  } catch {
    data = {
      as_of: new Date().toISOString(),
      disclaimer: "The card is unavailable right now.",
      rows: [],
    };
  }
  const boardView = view === "open" || view === "final" ? view : "all";
  const boardResult = result === "correct" || result === "miss" || result === "push" ? result : undefined;
  const boardBook = book === "ml" || book === "spread" ? book : undefined;

  return (
    <div className="space-y-8">
      <PageHeader
        kicker="The card"
        title="Against the Spread"
        lede="Moneyline and spread are kept apart. Each game shows who we think wins and the side of the number."
        action={<LeagueToggle value={league} basePath="/ats" today="" extra={{ sort, side, view: boardView === "all" ? undefined : boardView, result: boardResult, book: boardBook }} />}
      />
      <AtsBoard
        rows={data.rows}
        disclaimer={data.disclaimer}
        league={league}
        sort={sort}
        side={side}
        view={boardView}
        result={boardResult}
        book={boardBook}
      />
    </div>
  );
}
