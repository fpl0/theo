/**
 * Unit tests for the embedding service.
 *
 * These tests use a mock embedding service — they do NOT load the real
 * ONNX model. Real model integration is tested separately when available.
 */

import { describe, expect, test } from "bun:test";
import { EMBEDDING_DIM, fromVectorLiteral, toVectorLiteral } from "../../src/memory/embeddings.ts";
import { createMockEmbeddings } from "../helpers.ts";

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("mock embedding service", () => {
	const svc = createMockEmbeddings();

	test("embed returns Float32Array of correct dimension", async () => {
		const v = await svc.embed("hello world");
		expect(v).toBeInstanceOf(Float32Array);
		expect(v.length).toBe(EMBEDDING_DIM);
	});

	test("embed is deterministic", async () => {
		const v1 = await svc.embed("same input");
		const v2 = await svc.embed("same input");
		expect(v1).toEqual(v2);
	});

	test("embed produces L2-normalized vectors", async () => {
		const v = await svc.embed("normalize me");
		let norm = 0;
		for (let i = 0; i < v.length; i++) {
			norm += (v[i] ?? 0) * (v[i] ?? 0);
		}
		expect(Math.abs(Math.sqrt(norm) - 1.0)).toBeLessThan(1e-5);
	});

	test("embed rejects empty string", async () => {
		await expect(svc.embed("")).rejects.toThrow("empty");
	});

	test("embed rejects whitespace-only string", async () => {
		await expect(svc.embed("   ")).rejects.toThrow("empty");
	});

	test("embedBatch returns correct count", async () => {
		const results = await svc.embedBatch(["one", "two", "three"]);
		expect(results.length).toBe(3);
		for (const v of results) {
			expect(v.length).toBe(EMBEDDING_DIM);
		}
	});

	test("embedBatch returns empty array for empty input", async () => {
		const results = await svc.embedBatch([]);
		expect(results.length).toBe(0);
	});

	test("different texts produce different embeddings", async () => {
		const v1 = await svc.embed("cats are great");
		const v2 = await svc.embed("dogs are great");
		let differs = false;
		for (let i = 0; i < EMBEDDING_DIM; i++) {
			if (v1[i] !== v2[i]) {
				differs = true;
				break;
			}
		}
		expect(differs).toBe(true);
	});
});

describe("vector serialization", () => {
	/** Generate a deterministic 768-dim vector for serialization tests. */
	function makeVector(seed: number): Float32Array {
		const v = new Float32Array(EMBEDDING_DIM);
		for (let i = 0; i < EMBEDDING_DIM; i++) {
			v[i] = Math.sin(seed + i * 0.1);
		}
		return v;
	}

	test("toVectorLiteral produces pgvector format", () => {
		const v = makeVector(42);
		const literal = toVectorLiteral(v);
		expect(literal).toStartWith("[");
		expect(literal).toEndWith("]");
		expect(literal).toContain(",");
	});

	test("toVectorLiteral rejects wrong dimension", () => {
		const v = new Float32Array([0.1, 0.2, 0.3]);
		expect(() => toVectorLiteral(v)).toThrow("768");
	});

	test("fromVectorLiteral rejects wrong dimension", () => {
		expect(() => fromVectorLiteral("[0.1,0.2,0.3]")).toThrow("768");
	});

	test("round-trip preserves values", () => {
		const original = makeVector(7);
		const literal = toVectorLiteral(original);
		const restored = fromVectorLiteral(literal);
		expect(restored.length).toBe(EMBEDDING_DIM);
		for (let i = 0; i < EMBEDDING_DIM; i++) {
			expect(restored[i]).toBeCloseTo(original[i] ?? 0, 5);
		}
	});

	test("round-trip with varied values", () => {
		const original = new Float32Array(EMBEDDING_DIM);
		for (let i = 0; i < EMBEDDING_DIM; i++) {
			original[i] = (i % 2 === 0 ? 1 : -1) * ((i + 1) / EMBEDDING_DIM);
		}
		const literal = toVectorLiteral(original);
		const restored = fromVectorLiteral(literal);
		expect(restored.length).toBe(EMBEDDING_DIM);
		for (let i = 0; i < EMBEDDING_DIM; i++) {
			expect(restored[i]).toBeCloseTo(original[i] ?? 0, 5);
		}
	});

	test("fromVectorLiteral rejects non-array", () => {
		expect(() => fromVectorLiteral("{}")).toThrow("Expected array");
	});
});
