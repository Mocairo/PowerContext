import { describe, expect, it } from 'vitest';
import { chunkText, chunkId, quality } from '../shared/chunking';
import { SerialQueue } from '../server/database';

describe('chunk boundaries and identity', () => {
  it('preserves every Unicode code point and overlaps the intended region', () => {
    const text = 'a😀你好bcde';
    expect(chunkText(text, 4, 1)).toEqual(['a😀你好', '好bcd', 'de']);
    expect(chunkText('  \r\n  ')).toEqual([]);
    expect(chunkText('a'.repeat(380))).toHaveLength(1);
    expect(() => chunkText('x', 4, 4)).toThrow();
  });
  it('deduplicates identical inputs and distinguishes changed content/source/part', async () => {
    const first = await chunkId('a.txt', 0, 'hello');
    expect(first).toBe(await chunkId('a.txt', 0, 'hello'));
    expect(new Set(await Promise.all([chunkId('b.txt', 0, 'hello'), chunkId('a.txt', 1, 'hello'), chunkId('a.txt', 0, 'changed'), Promise.resolve(first)])).size).toBe(4);
  });
});

describe('graded retrieval metrics', () => {
  it('uses all relevant documents as recall denominator and discounted graded gains', () => {
    const result = quality(['b', 'a', 'a'], { a: 3, b: 1, c: 2 }, 2);
    expect(result.recall).toBeCloseTo(2 / 3);
    expect(result.ndcg).toBeCloseTo((1 + 7 / Math.log2(3)) / (7 + 3 / Math.log2(3)));
    expect(quality([], {}, 5)).toEqual({ recall: 0, ndcg: 0 });
    expect(quality(['a', 'b', 'c'], { a: 3, b: 2, c: 1 }, 3).ndcg).toBe(1);
  });
});

describe('SDK connection operation serialization', () => {
  it('never interleaves session-variable operations and survives a rejected operation', async () => {
    const queue = new SerialQueue(), trace: string[] = [];
    const op = (name: string, fail = false) => queue.run(async () => {
      trace.push(`${name}:set`);
      await new Promise(resolve => setTimeout(resolve, 2));
      trace.push(`${name}:query`);
      if (fail) throw new Error(name);
      return name;
    });
    const result = await Promise.allSettled([op('a'), op('b', true), op('c')]);
    expect(trace).toEqual(['a:set', 'a:query', 'b:set', 'b:query', 'c:set', 'c:query']);
    expect(result.map(r => r.status)).toEqual(['fulfilled', 'rejected', 'fulfilled']);
  });
});
