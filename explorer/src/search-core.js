const DEFAULT_STOP_WORDS = new Set([
  "a",
  "an",
  "and",
  "are",
  "as",
  "at",
  "be",
  "by",
  "for",
  "from",
  "in",
  "into",
  "is",
  "it",
  "of",
  "on",
  "or",
  "the",
  "to",
  "with"
]);

export function tokenize(value, minimumLength = 2, stopWords = DEFAULT_STOP_WORDS) {
  const text = String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
  const tokens = [];
  const seen = new Set();
  for (const match of text.matchAll(/[a-z0-9][a-z0-9._-]*/g)) {
    const token = match[0].replace(/^[._-]+|[._-]+$/g, "");
    if (token.length < minimumLength || stopWords.has(token) || seen.has(token)) continue;
    tokens.push(token);
    seen.add(token);
  }
  return tokens;
}

export function searchShard(value, length = 2) {
  const clean = String(value || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  return clean.slice(0, length) || "_";
}

export function intersectSets(left, right) {
  const output = new Set();
  for (const value of left) {
    if (right.has(value)) output.add(value);
  }
  return output;
}

export function rankOrdinals(groups, limit = 200, maxPostingsPerToken = Number.MAX_SAFE_INTEGER) {
  const scores = new Map();
  const allSets = [];
  const completeSets = [];
  for (const group of groups) {
    const groupSet = new Set();
    let complete = true;
    for (const entry of group) {
      if (Number(entry.df || 0) > maxPostingsPerToken) complete = false;
      for (const posting of entry.rows || []) {
        const ordinal = Number(posting[0]);
        const score = Number(posting[1] || 0);
        const mask = Number(posting[2] || 0);
        if (!Number.isInteger(ordinal) || ordinal < 0) continue;
        groupSet.add(ordinal);
        scores.set(ordinal, (scores.get(ordinal) || 0) + score + (mask & 1 ? 4 : 0));
      }
    }
    allSets.push(groupSet);
    if (complete) completeSets.push(groupSet);
  }
  const sets = completeSets.length ? completeSets : allSets.slice(0, 1);
  if (!sets.length) return [];
  let matches = sets[0];
  for (const set of sets.slice(1)) matches = intersectSets(matches, set);
  if (!matches.size && sets.length > 1) {
    matches = new Set();
    for (const set of sets) {
      for (const value of set) matches.add(value);
    }
  }
  return [...matches]
    .sort((left, right) => (scores.get(right) || 0) - (scores.get(left) || 0) || left - right)
    .slice(0, limit)
    .map((ordinal) => ({ ordinal, score: scores.get(ordinal) || 0 }));
}
