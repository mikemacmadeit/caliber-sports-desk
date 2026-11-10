export type Club = {
  abbr: string;
  city: string;
  nick: string;
  primary: string;
  secondary: string;
  logo: string;
};

const NFL = "https://a.espncdn.com/i/teamlogos/nfl/500";
const NCAA = "https://a.espncdn.com/i/teamlogos/ncaa/500";

const NFL_ABBR = new Set([
  "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX",
  "KC", "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
]);

/** Club colors stay on the mark. They are not part of the site palette. */
const CLUBS: Record<string, Club> = {
  KC: { abbr: "KC", city: "Kansas City", nick: "Chiefs", primary: "#E31837", secondary: "#FFB81C", logo: `${NFL}/kc.png` },
  BUF: { abbr: "BUF", city: "Buffalo", nick: "Bills", primary: "#00338D", secondary: "#C60C30", logo: `${NFL}/buf.png` },
  DET: { abbr: "DET", city: "Detroit", nick: "Lions", primary: "#0076B6", secondary: "#B0B7BC", logo: `${NFL}/det.png` },
  GB: { abbr: "GB", city: "Green Bay", nick: "Packers", primary: "#203731", secondary: "#FFB612", logo: `${NFL}/gb.png` },
  PHI: { abbr: "PHI", city: "Philadelphia", nick: "Eagles", primary: "#004C54", secondary: "#A5ACAF", logo: `${NFL}/phi.png` },
  DAL: { abbr: "DAL", city: "Dallas", nick: "Cowboys", primary: "#003594", secondary: "#869397", logo: `${NFL}/dal.png` },
  SF: { abbr: "SF", city: "San Francisco", nick: "49ers", primary: "#AA0000", secondary: "#B3995D", logo: `${NFL}/sf.png` },
  SEA: { abbr: "SEA", city: "Seattle", nick: "Seahawks", primary: "#002244", secondary: "#69BE28", logo: `${NFL}/sea.png` },
  OSU: { abbr: "OSU", city: "Ohio State", nick: "Buckeyes", primary: "#BB0000", secondary: "#666666", logo: `${NCAA}/194.png` },
  MICH: { abbr: "MICH", city: "Michigan", nick: "Wolverines", primary: "#00274C", secondary: "#FFCB05", logo: `${NCAA}/130.png` },
  ALA: { abbr: "ALA", city: "Alabama", nick: "Crimson Tide", primary: "#9E1B32", secondary: "#828A8F", logo: `${NCAA}/333.png` },
  LSU: { abbr: "LSU", city: "LSU", nick: "Tigers", primary: "#461D7C", secondary: "#FDD023", logo: `${NCAA}/99.png` },
  ORE: { abbr: "ORE", city: "Oregon", nick: "Ducks", primary: "#154733", secondary: "#FEE123", logo: `${NCAA}/2483.png` },
  WASH: { abbr: "WASH", city: "Washington", nick: "Huskies", primary: "#4B2E83", secondary: "#E8D3A2", logo: `${NCAA}/264.png` },
  UGA: { abbr: "UGA", city: "Georgia", nick: "Bulldogs", primary: "#BA0C2F", secondary: "#000000", logo: `${NCAA}/61.png` },
  TEX: { abbr: "TEX", city: "Texas", nick: "Longhorns", primary: "#BF5700", secondary: "#333F48", logo: `${NCAA}/251.png` },
};

export function club(abbr: string | null | undefined): Club {
  const key = (abbr || "").toUpperCase();
  if (CLUBS[key]) return CLUBS[key];
  if (NFL_ABBR.has(key)) {
    return {
      abbr: key,
      city: key,
      nick: key,
      primary: "#2C3848",
      secondary: "#F4F6FB",
      logo: `${NFL}/${key.toLowerCase()}.png`,
    };
  }
  return {
    abbr: key || "—",
    city: key,
    nick: key,
    primary: "#2C3848",
    secondary: "#F4F6FB",
    logo: "",
  };
}

export type TeamMarkInfo = {
  location?: string | null;
  nick?: string | null;
  primary?: string | null;
  secondary?: string | null;
  logo?: string | null;
};

/** Known clubs keep their palette. Everyone else uses the schedule mark. */
export function presentTeam(team: {
  abbreviation: string;
  name?: string;
  mark?: TeamMarkInfo | null;
}): Club {
  const known = CLUBS[(team.abbreviation || "").toUpperCase()];
  if (known) return known;
  const base = club(team.abbreviation);
  const mark = team.mark;
  return {
    abbr: base.abbr,
    city: mark?.location || team.name || base.city,
    nick: mark?.nick || base.nick,
    primary: mark?.primary || base.primary,
    secondary: mark?.secondary || base.secondary,
    logo: mark?.logo || base.logo,
  };
}

const NICK_TO_ABBR: Array<[string, string]> = [
  ["49ers", "SF"],
  ["buccaneers", "TB"],
  ["commanders", "WAS"],
  ["cardinals", "ARI"],
  ["seahawks", "SEA"],
  ["steelers", "PIT"],
  ["packers", "GB"],
  ["patriots", "NE"],
  ["chargers", "LAC"],
  ["dolphins", "MIA"],
  ["panthers", "CAR"],
  ["raiders", "LV"],
  ["broncos", "DEN"],
  ["vikings", "MIN"],
  ["falcons", "ATL"],
  ["ravens", "BAL"],
  ["bengals", "CIN"],
  ["browns", "CLE"],
  ["cowboys", "DAL"],
  ["lions", "DET"],
  ["texans", "HOU"],
  ["colts", "IND"],
  ["jaguars", "JAX"],
  ["chiefs", "KC"],
  ["rams", "LAR"],
  ["giants", "NYG"],
  ["jets", "NYJ"],
  ["eagles", "PHI"],
  ["saints", "NO"],
  ["bills", "BUF"],
  ["bears", "CHI"],
  ["titans", "TEN"],
  ["washington", "WAS"],
];

/** A short name for a ticket. Known clubs use the mascot. Everyone else keeps the school name. */
export function ticketName(name: string | null | undefined): string {
  const text = (name || "").trim();
  if (!text) return "";
  const lower = text.toLowerCase();
  for (const [nick] of NICK_TO_ABBR) {
    if (lower.endsWith(nick)) return nick.replace(/(^|\s)\w/g, (letter) => letter.toUpperCase());
  }
  return text;
}

/** A schedule name is enough for a club color when the card has no abbreviation. */
export function clubFromName(name: string | null | undefined): Club {
  const text = (name || "").toLowerCase();
  for (const [nick, abbr] of NICK_TO_ABBR) {
    if (text.endsWith(nick)) return club(abbr);
  }
  return tinted(name || "Club");
}

function tinted(name: string): Club {
  let hash = 0;
  for (const ch of name) hash = (hash * 33 + ch.charCodeAt(0)) >>> 0;
  const hue = hash % 360;
  const letters = name
    .split(/\s+/)
    .map((word) => word[0] || "")
    .join("")
    .slice(0, 3)
    .toUpperCase();
  return {
    abbr: letters || "—",
    city: name,
    nick: name,
    primary: `hsl(${hue} 58% 42%)`,
    secondary: `hsl(${(hue + 36) % 360} 72% 62%)`,
    logo: "",
  };
}
