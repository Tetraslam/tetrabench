import assert from "node:assert/strict";
import test from "node:test";
import {
  capturePiModel, captureClaudeModels, projectOpenCodeProviders,
} from "../src/tetrabench/native_reasoning.mjs";

test("Pi only uses synchronous model and pure thinking SDK methods", () => {
  const model = {
    id: "model", provider: "route", api: "some-native-api",
    baseUrl: "https://metadata.example/v1", reasoning: true,
    thinkingLevelMap: { high: "deliberate" }, headers: { authorization: "private" },
    compat: { forceAdaptiveThinking: true, headers: { authorization: "private" } },
  };
  let reads = 0;
  const runtime = new Proxy({}, {
    get(_target, prop) {
      assert.equal(prop, "getModel");
      return (provider, id) => {
        assert.equal(provider, "route");
        assert.equal(id, "model");
        reads++;
        return model;
      };
    },
  });
  const sdk = {
    getSupportedThinkingLevels(input) {
      assert.equal(input, model);
      return ["high"];
    },
    clampThinkingLevel(input, _level) {
      assert.equal(input, model);
      return "high";
    },
  };
  const result = capturePiModel(runtime, sdk, "route", "model");
  assert.equal(reads, 1);
  assert.ok(!result.includes("private"));
  assert.deepEqual(JSON.parse(result).supportedThinkingLevels, ["high"]);
  assert.equal(JSON.parse(result).normalizations.low, "high");
  assert.throws(() => capturePiModel({}, {}, "route", "model"), /unavailable/);
});

test("Claude supportedModels is gated and strips account data", async () => {
  let calls = 0;
  const query = {
    async supportedModels() {
      calls++;
      return [{ value: "alias", resolvedModel: "resolved", supportsEffort: true,
        supportedEffortLevels: ["high"], supportsAdaptiveThinking: true,
        account: { token: "private" } }];
    },
  };
  await assert.rejects(captureClaudeModels(query), /Explicit/);
  assert.equal(calls, 0);
  const result = await captureClaudeModels(query, { allowNativeRead: true });
  assert.equal(calls, 1);
  assert.ok(!result.includes("private"));
  assert.equal(JSON.parse(result)[0].resolvedModel, "resolved");
});

test("OpenCode projection removes provider keys and preserves native variants", () => {
  const projected = JSON.parse(projectOpenCodeProviders({ all: [{
    id: "route", key: "private", options: { apiKey: "private" },
    models: { model: { id: "model", providerID: "route",
      variants: { custom: { thinking: { budgetTokens: 2048 } } },
      headers: { authorization: "private" } } },
  }] }));
  assert.ok(!JSON.stringify(projected).includes("private"));
  assert.equal(projected.all[0].models.model.variants.custom.thinking.budgetTokens, 2048);
});
