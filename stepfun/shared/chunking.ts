// Short overlapping windows avoid silently truncating long imported files.
export function chunkText(text: string, size = 380, overlap = 60): string[] {
  if (!Number.isInteger(size) || size < 1 || overlap < 0 || overlap >= size) throw new Error('Invalid chunk window');
  const chars = Array.from(text.replace(/\r\n?/g, '\n').normalize('NFC').trim());
  if (!chars.length) return [];
  const chunks: string[] = [];
  for (let start = 0; start < chars.length; start += size - overlap) {
    chunks.push(chars.slice(start, start + size).join(''));
    if (start + size >= chars.length) break;
  }
  return chunks;
}

export async function chunkId(source: string, part: number, text: string) {
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify([source, part, text])));
  return Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, '0')).join('');
}

export function quality(retrieved: string[], relevant: Record<string, number>, k: number) {
  const ranking = [...new Set(retrieved)].slice(0, k);
  const relevantIds = Object.keys(relevant).filter(id => relevant[id] > 0);
  const recall = relevantIds.length ? ranking.filter(id => relevant[id] > 0).length / relevantIds.length : 0;
  const dcg = ranking.reduce((sum, id, i) => sum + (2 ** (relevant[id] ?? 0) - 1) / Math.log2(i + 2), 0);
  const ideal = Object.values(relevant).filter(x => x > 0).sort((a, b) => b - a).slice(0, k).reduce((sum, grade, i) => sum + (2 ** grade - 1) / Math.log2(i + 2), 0);
  return { recall, ndcg: ideal ? dcg / ideal : 0 };
}
