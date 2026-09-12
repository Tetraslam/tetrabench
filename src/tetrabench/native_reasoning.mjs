/**
 * Metadata-only helpers for HOST-OWNED, already initialized native objects.
 * No imports, process spawn, auth lookup, refresh, prompt, or stream operation.
 * The host must bind versions/config/route and review its own initialization.
 */

const PI_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];

function pick(value, keys) {
  return Object.fromEntries(keys.filter((key) => Object.hasOwn(value, key))
    .map((key) => [key, value[key]]));
}

export function capturePiModel(runtimeOrRegistry, sdk, provider, modelId) {
  // pi-coding-agent 0.85.1 ModelRuntime.getModel / ModelRegistry.find.
  // pi-ai 0.85.1 models.ts getSupportedThinkingLevels / clampThinkingLevel.
  const getter = runtimeOrRegistry.getModel ?? runtimeOrRegistry.find;
  if (typeof getter !== "function" || typeof sdk.getSupportedThinkingLevels !== "function"
      || typeof sdk.clampThinkingLevel !== "function") {
    throw new Error("Pi 0.85.1 metadata API unavailable; install the matched SDK");
  }
  const model = getter.call(runtimeOrRegistry, provider, modelId);
  if (!model) throw new Error("Pi model absent from effective native catalog");
  const projection = projectPiModel(model);
  return JSON.stringify({
    model: projection,
    supportedThinkingLevels: sdk.getSupportedThinkingLevels(model),
    normalizations: Object.fromEntries(PI_LEVELS.map((level) =>
      [level, sdk.clampThinkingLevel(model, level)])),
  });
}

export function projectPiModel(model) {
  // Complete public Model metadata, including route policy and model defaults.
  // Never retain credential/header values. Such model-header routes need a
  // separately proven native boundary and are refused by the runtime guard.
  const projection = pick(model, ["id", "name", "provider", "api", "baseUrl",
    "reasoning", "thinkingLevelMap", "maxTokens", "contextWindow", "input",
    "cost", "samplingParams"]);
  let opaque = false;
  const publicMetadata = (value, depth = 0) => {
    if (depth > 32) throw new Error("Pi metadata nesting limit");
    if (Array.isArray(value)) return value.map((item) => publicMetadata(item, depth + 1));
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).flatMap(([key, item]) => {
        if (/headers|api.?key|secret|password|authorization|access.?token|refresh.?token/i.test(key)) {
          opaque = true;
          return [];
        }
        return [[key, publicMetadata(item, depth + 1)]];
      }));
    }
    return value;
  };
  projection.compat = publicMetadata(model.compat ?? {});
  projection.hasOpaqueMetadata = opaque;
  projection.hasModelHeaders = Object.keys(model.headers ?? {}).length !== 0;
  return projection;
}

export async function captureClaudeModels(query, { allowNativeRead = false } = {}) {
  // SDK 0.3.269 / CLI 2.1.269; the 0.3.267 / 2.1.267 contract is retained.
  // Never call query(), reinitialize(), or any prompt/stream method here.
  if (!allowNativeRead) throw new Error("Explicit native metadata read required");
  if (typeof query?.supportedModels !== "function") {
    throw new Error("Claude Agent SDK supportedModels API unavailable");
  }
  const models = await query.supportedModels();
  return JSON.stringify(models.map((model) => pick(model, ["value", "resolvedModel",
    "supportsEffort", "supportedEffortLevels", "supportsAdaptiveThinking"])));
}

export function projectOpenCodeProviders(response) {
  // OpenCode 1.18.30 GET /provider: project the effective model rows only.
  // The complete provider response can contain keys/options. Never serialize it.
  return JSON.stringify({ all: response.all.map((provider) => ({
    id: provider.id,
    models: Object.fromEntries(Object.entries(provider.models).map(([id, model]) => [id,
      pick(model, ["id", "providerID", "api", "capabilities", "variants"])])),
  })) });
}

// CLI entry for the isolated Python collector. Importing this module stays inert.
if (process.argv[2] === "collect-pi" && process.argv[1] &&
    import.meta.url === (await import("node:url")).pathToFileURL(process.argv[1]).href) {
  let stage = "input";
  try {
    const { readFile } = await import("node:fs/promises");
    const { pathToFileURL } = await import("node:url");
    let input = "";
    for await (const chunk of process.stdin) {
      input += chunk;
      if (input.length > 2 * 1024 * 1024) throw new Error("input limit");
      if (input.includes("\n")) break;
    }
    const request = JSON.parse(input);
    const packageRoot = request.pi_package;
    stage = "package-pin";
    const packageInfo = JSON.parse(await readFile(`${packageRoot}/package.json`, "utf8"));
    if (packageInfo.version !== "0.85.1" ||
        packageInfo.name !== "@earendil-works/pi-coding-agent") throw new Error("pin mismatch");
    stage = "import-sdk";
    const sdkPath = import.meta.resolve("@earendil-works/pi-ai",
      pathToFileURL(`${packageRoot}/package.json`).href);
    const sdkInfo = JSON.parse(await readFile(new URL("../package.json", sdkPath), "utf8"));
    if (sdkInfo.name !== "@earendil-works/pi-ai" || sdkInfo.version !== "0.85.1") {
      throw new Error("Pi thinking SDK pin mismatch");
    }
    const sdk = await import(sdkPath);
    const { ModelRuntime } = await import(pathToFileURL(`${packageRoot}/dist/core/model-runtime.js`));
    stage = "create-runtime";
    const runtime = await ModelRuntime.create({
      modelsPath: request.models_path,
      refreshOnCreate: false,
      allowModelNetwork: false,
      ...(request.authenticated ? {} : { credentials: new sdk.InMemoryCredentialStore() }),
    });
    let catalogEntry;
    if (request.authenticated) {
      // Only the explicit auth-owner runtime may reach native authenticated reads.
      await runtime.refresh({ allowNetwork: request.refresh, providers: [request.provider] });
    } else if (request.cache) {
      stage = "native-cache-restore";
      const store = JSON.parse(await readFile(request.cache, "utf8"));
      const provider = runtime.getProvider(request.provider);
      if (provider?.refreshModels && store[request.provider]) {
        await provider.refreshModels({
          allowNetwork: false, stored: store[request.provider],
          signal: AbortSignal.timeout(10000),
          publish: async (publication) => {
            if (publication.persist !== undefined) throw new Error("offline cache mutation");
            publication.update?.();
            return true;
          },
        });
        const entry = store[request.provider];
        const selected = entry.models?.find((model) => model.id === request.model);
        if (selected) {
          const safe = projectPiModel(selected);
          if (!safe.hasModelHeaders && !safe.hasOpaqueMetadata) {
            delete safe.hasModelHeaders;
            delete safe.hasOpaqueMetadata;
            catalogEntry = {...pick(entry, ["lastModified", "checkedAt", "etag"]), models:[safe]};
          }
        }
      }
    }
    if (runtime.getError()) throw new Error("native model config rejected");
    stage = "model-lookup";
    const captured = JSON.parse(capturePiModel(runtime, sdk, request.provider, request.model));
    if (catalogEntry) captured.catalogEntry = catalogEntry;
    console.log(JSON.stringify(captured));
  } catch {
    console.log(JSON.stringify({ unavailable: `Pi metadata-only initialization failed at ${stage}` }));
    process.exitCode = 1;
  }
}
