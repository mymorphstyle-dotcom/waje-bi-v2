#!/usr/bin/env node

import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  CanonicalDecimal,
  CanonicalTimestamp,
  canonicalIdentityJson,
  canonicalIdentitySha256,
} from "./measurement-identity-codec.mjs";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const vectors = JSON.parse(
  await readFile(
    join(root, "contracts", "test-vectors", "measurement-identity.v1.json"),
    "utf8",
  ),
);

for (const vector of vectors.golden_vectors) {
  const value = revive(vector.value);
  const canonical = canonicalIdentityJson(value);
  const digest = canonicalIdentitySha256(value);
  if (canonical !== vector.expected_canonical_json || digest !== vector.expected_sha256) {
    throw new Error(`identity golden vector failed: ${vector.name}`);
  }
}

for (const vector of vectors.mutation_vectors) {
  const left = canonicalIdentitySha256(revive(vector.left));
  const right = canonicalIdentitySha256(revive(vector.right));
  const actual = left === right ? "same" : "different";
  if (actual !== vector.expected_identity_relation) {
    throw new Error(`identity mutation vector failed: ${vector.name}`);
  }
}

function revive(value) {
  if (Array.isArray(value)) {
    return value.map(revive);
  }
  if (value && typeof value === "object") {
    if (Object.keys(value).length === 1 && "$decimal" in value) {
      return new CanonicalDecimal(value.$decimal);
    }
    if (Object.keys(value).length === 1 && "$timestamp" in value) {
      return new CanonicalTimestamp(value.$timestamp);
    }
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, revive(item)]),
    );
  }
  return value;
}
