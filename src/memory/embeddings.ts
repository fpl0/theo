/**
 * High-performance embedding service for Theo's knowledge graph.
 *
 * Uses HuggingFace Transformers.js with ONNX runtime. On macOS Apple Silicon,
 * CoreML execution provider routes computation through the Neural Engine and GPU
 * for hardware-accelerated inference. The pipeline is loaded lazily on first call
 * and reused as a singleton — call warmup() at boot to avoid cold-start latency.
 *
 * Model: Xenova/all-mpnet-base-v2 (768 dimensions, L2-normalized).
 * Precision: fp32. The Xenova fp16 variant of this model has a LayerNorm fusion
 * that onnxruntime's graph optimizer cannot resolve (insertedPrecisionFreeCast
 * references a non-existent node), so init fails on every execution provider.
 * CoreML handles fp32 natively and is ~4× faster than CPU fp32, so the quality
 * and speed win is kept without the fp16 download. Revisit if upstream fixes the
 * fp16 graph or we pin a different model.
 * Schema: vector(768) columns in PostgreSQL via pgvector.
 */

import type { DataType, DeviceType, FeatureExtractionPipeline } from "@huggingface/transformers";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Embedding dimensions produced by Xenova/all-mpnet-base-v2. Must match vector(768) columns. */
export const EMBEDDING_DIM = 768;

/** Model identifier on HuggingFace Hub. */
const MODEL_ID = "Xenova/all-mpnet-base-v2";

/** Maximum texts per batch to prevent OOM. The model pads to longest sequence in batch. */
const MAX_BATCH_SIZE = 32;

/** Pipeline call options: mean pooling + L2 normalization for cosine similarity. */
const PIPELINE_OPTS = { pooling: "mean" as const, normalize: true };

// ---------------------------------------------------------------------------
// EmbeddingService interface
// ---------------------------------------------------------------------------

/** Converts text to dense vector embeddings for semantic similarity search. */
export interface EmbeddingService {
	/** Embed a single text. Returns L2-normalized Float32Array of EMBEDDING_DIM. */
	embed(text: string): Promise<Float32Array>;

	/** Embed multiple texts. Returns one Float32Array per input text. */
	embedBatch(texts: readonly string[]): Promise<readonly Float32Array[]>;

	/**
	 * Eagerly load the model and warm the inference pipeline.
	 * Call at startup to shift the cold-start cost out of the first user request.
	 * Safe to call multiple times — subsequent calls are no-ops.
	 */
	warmup(): Promise<void>;
}

// ---------------------------------------------------------------------------
// Validation helpers
// ---------------------------------------------------------------------------

function validateText(text: string): void {
	if (text.trim().length === 0) {
		throw new Error("Cannot embed empty or whitespace-only text");
	}
}

/**
 * Extract a single 768-dim Float32Array from a pipeline Tensor output.
 * The output shape for a single text with mean pooling is [1, 768].
 */
function extractSingleVector(output: {
	readonly data: unknown;
	readonly dims: readonly number[];
}): Float32Array {
	if (!(output.data instanceof Float32Array)) {
		throw new Error(`Expected Float32Array from pipeline, got ${typeof output.data}`);
	}
	if (output.data.length !== EMBEDDING_DIM) {
		throw new Error(
			`Expected ${String(EMBEDDING_DIM)} dimensions, got ${String(output.data.length)}`,
		);
	}
	return output.data;
}

/**
 * Extract N vectors from a batched pipeline Tensor output.
 * The output shape for N texts with mean pooling is [N, 768].
 * The data is a contiguous Float32Array of length N * 768.
 */
function extractBatchVectors(
	output: { readonly data: unknown; readonly dims: readonly number[] },
	count: number,
): Float32Array[] {
	if (!(output.data instanceof Float32Array)) {
		throw new Error(`Expected Float32Array from pipeline, got ${typeof output.data}`);
	}
	const expected = count * EMBEDDING_DIM;
	if (output.data.length !== expected) {
		throw new Error(
			`Expected ${String(expected)} values (${String(count)} x ${String(EMBEDDING_DIM)}), got ${String(output.data.length)}`,
		);
	}
	const vectors: Float32Array[] = [];
	for (let i = 0; i < count; i++) {
		vectors.push(output.data.slice(i * EMBEDDING_DIM, (i + 1) * EMBEDDING_DIM));
	}
	return vectors;
}

// ---------------------------------------------------------------------------
// pgvector serialization
// ---------------------------------------------------------------------------

/**
 * Serialize Float32Array to pgvector string literal: `[0.1,0.2,...,0.768]`.
 * Used in tagged template queries: `sql\`... ${toVectorLiteral(v)}::vector ...\``.
 */
export function toVectorLiteral(v: Float32Array): string {
	if (v.length !== EMBEDDING_DIM) {
		throw new Error(`Expected ${String(EMBEDDING_DIM)} dimensions, got ${String(v.length)}`);
	}
	let s = "[";
	for (let i = 0; i < v.length; i++) {
		if (i > 0) s += ",";
		s += String(v[i]);
	}
	return `${s}]`;
}

/**
 * Parse pgvector string literal back to Float32Array.
 * pgvector returns vectors as `[0.1,0.2,...,0.768]` which is valid JSON.
 */
export function fromVectorLiteral(s: string): Float32Array {
	const parsed: unknown = JSON.parse(s);
	if (!Array.isArray(parsed)) {
		throw new Error(`Expected array from pgvector literal, got ${typeof parsed}`);
	}
	if (parsed.length !== EMBEDDING_DIM) {
		throw new Error(`Expected ${String(EMBEDDING_DIM)} dimensions, got ${String(parsed.length)}`);
	}
	for (const el of parsed) {
		if (typeof el !== "number") {
			throw new Error(`Expected numeric elements in pgvector literal, got ${typeof el}`);
		}
	}
	return new Float32Array(parsed);
}

// ---------------------------------------------------------------------------
// HuggingFace implementation
// ---------------------------------------------------------------------------

export interface EmbeddingServiceOptions {
	/** ONNX execution provider device. Defaults to "coreml" on macOS, "cpu" elsewhere. */
	readonly device?: DeviceType;
	/**
	 * Model precision. Defaults to "fp32".
	 *
	 * The Xenova/all-mpnet-base-v2 fp16 ONNX graph has a LayerNorm fusion that
	 * onnxruntime's optimizer cannot resolve (insertedPrecisionFreeCast references
	 * a node removed during fusion). fp16 therefore fails on both CoreML and CPU.
	 * fp32 on CoreML is fast (~1.5s init) and keeps identical embedding output.
	 */
	readonly dtype?: DataType;
}

/**
 * Embedding service backed by HuggingFace Transformers.js + ONNX runtime.
 *
 * On Apple Silicon, CoreML routes inference through the Neural Engine (16-core ANE
 * on M1), GPU, or CPU based on what is optimal per operation.
 *
 * The pipeline is loaded lazily on first embed/warmup call. Construction is cheap.
 * Model is downloaded on first run (~218MB fp16) and cached at ~/.cache/huggingface/.
 */
export class HuggingFaceEmbeddingService implements EmbeddingService {
	private pipelinePromise: Promise<FeatureExtractionPipeline> | null = null;
	private readonly device: DeviceType;
	private readonly dtype: DataType;

	constructor(options?: EmbeddingServiceOptions) {
		this.device = options?.device ?? (process.platform === "darwin" ? "coreml" : "cpu");
		this.dtype = options?.dtype ?? "fp32";
	}

	async warmup(): Promise<void> {
		const pipe = await this.getPipeline();
		// Run a throwaway inference to trigger CoreML model compilation (first-run cost).
		// Subsequent calls skip compilation because CoreML caches the compiled model.
		await pipe._call("warmup", PIPELINE_OPTS);
	}

	async embed(text: string): Promise<Float32Array> {
		validateText(text);
		const pipe = await this.getPipeline();
		// _call is the typed internal method on FeatureExtractionPipeline. The pipeline
		// is callable as a function at runtime (via Proxy), but TypeScript's type system
		// doesn't expose the call signature — only _call is typed with the correct
		// parameter and return types. Pinned to @huggingface/transformers@4.0.1.
		const output = await pipe._call(text, PIPELINE_OPTS);
		return extractSingleVector(output);
	}

	async embedBatch(texts: readonly string[]): Promise<readonly Float32Array[]> {
		if (texts.length === 0) return [];
		for (const text of texts) validateText(text);

		const pipe = await this.getPipeline();
		const results: Float32Array[] = [];

		for (let i = 0; i < texts.length; i += MAX_BATCH_SIZE) {
			const chunk = texts.slice(i, i + MAX_BATCH_SIZE);
			const output = await pipe._call([...chunk], PIPELINE_OPTS);
			results.push(...extractBatchVectors(output, chunk.length));
		}

		return results;
	}

	/**
	 * Lazy singleton pipeline. The promise is shared so concurrent callers
	 * wait for the same initialization — no duplicate model loads.
	 * On failure, the cached promise is cleared so the next call retries.
	 */
	private getPipeline(): Promise<FeatureExtractionPipeline> {
		if (this.pipelinePromise === null) {
			this.pipelinePromise = this.initPipeline().catch((error: unknown) => {
				this.pipelinePromise = null;
				throw error;
			});
		}
		return this.pipelinePromise;
	}

	private async initPipeline(): Promise<FeatureExtractionPipeline> {
		const { pipeline } = await import("@huggingface/transformers");
		// Intentionally no log here: the pipeline loads lazily on the first
		// embed call, which typically happens AFTER Ink has mounted. Writing
		// to stdout from this path bleeds through the TUI.
		return await pipeline("feature-extraction", MODEL_ID, {
			device: this.device,
			dtype: this.dtype,
		});
	}
}
