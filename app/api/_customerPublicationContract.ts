export type CustomerMaterialFactBinding = {
  fact_kind: "number" | "date" | "date_range" | "scope" | "label";
  name: string;
  range_end: string | null;
  unit: string | null;
  value: string;
};

export type CustomerPublicationBlock = {
  claim_refs: string[];
  limitation_refs: string[];
  material_fact_bindings: CustomerMaterialFactBinding[];
  recommendation_refs: string[];
  role: string;
  statement_role: string;
  text: string;
};

export type CustomerPublication = {
  blocks: CustomerPublicationBlock[];
  claim_refs: string[];
  field_visibility_policy_ref: string;
  limitation_refs: string[];
  recommendation_refs: string[];
  visualization_refs: string[];
  warnings: string[];
};

const CUSTOMER_PUBLICATION_KEYS = [
  "blocks",
  "claim_refs",
  "field_visibility_policy_ref",
  "limitation_refs",
  "recommendation_refs",
  "visualization_refs",
  "warnings",
] as const;

const CUSTOMER_BLOCK_KEYS = [
  "claim_refs",
  "limitation_refs",
  "material_fact_bindings",
  "recommendation_refs",
  "role",
  "statement_role",
  "text",
] as const;

const CUSTOMER_FACT_KEYS = [
  "fact_kind",
  "name",
  "range_end",
  "unit",
  "value",
] as const;

const CUSTOMER_FACT_KINDS = new Set([
  "number",
  "date",
  "date_range",
  "scope",
  "label",
]);

export function parseCustomerPublication(value: unknown): CustomerPublication {
  if (!isExactObject(value, CUSTOMER_PUBLICATION_KEYS)) {
    throw new Error("customer_publication_invalid");
  }
  const blocks = value.blocks;
  if (!Array.isArray(blocks)) {
    throw new Error("customer_publication_invalid");
  }
  const publication = {
    blocks: blocks.map(parseCustomerBlock),
    claim_refs: parseStringArray(value.claim_refs),
    field_visibility_policy_ref: parseRequiredString(
      value.field_visibility_policy_ref,
    ),
    limitation_refs: parseStringArray(value.limitation_refs),
    recommendation_refs: parseStringArray(value.recommendation_refs),
    visualization_refs: parseStringArray(value.visualization_refs),
    warnings: parseStringArray(value.warnings),
  } satisfies CustomerPublication;
  return publication;
}

function parseCustomerBlock(value: unknown): CustomerPublicationBlock {
  if (!isExactObject(value, CUSTOMER_BLOCK_KEYS)) {
    throw new Error("customer_publication_invalid");
  }
  if (!Array.isArray(value.material_fact_bindings)) {
    throw new Error("customer_publication_invalid");
  }
  return {
    claim_refs: parseStringArray(value.claim_refs),
    limitation_refs: parseStringArray(value.limitation_refs),
    material_fact_bindings: value.material_fact_bindings.map(
      parseCustomerMaterialFact,
    ),
    recommendation_refs: parseStringArray(value.recommendation_refs),
    role: parseRequiredString(value.role),
    statement_role: parseRequiredString(value.statement_role),
    text: parseRequiredString(value.text),
  };
}

function parseCustomerMaterialFact(value: unknown): CustomerMaterialFactBinding {
  if (!isExactObject(value, CUSTOMER_FACT_KEYS)) {
    throw new Error("customer_publication_invalid");
  }
  const factKind = parseRequiredString(value.fact_kind);
  const rangeEnd = parseNullableString(value.range_end);
  const unit = parseNullableString(value.unit);
  if (
    !CUSTOMER_FACT_KINDS.has(factKind)
    || (factKind !== "date_range" && rangeEnd !== null)
    || (factKind === "date_range" && rangeEnd === null)
    || (factKind !== "number" && unit !== null)
  ) {
    throw new Error("customer_publication_invalid");
  }
  return {
    fact_kind: factKind as CustomerMaterialFactBinding["fact_kind"],
    name: parseRequiredString(value.name),
    range_end: rangeEnd,
    unit,
    value: parseRequiredString(value.value),
  };
}

function parseStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    throw new Error("customer_publication_invalid");
  }
  return value.map(parseRequiredString);
}

function parseRequiredString(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error("customer_publication_invalid");
  }
  return value;
}

function parseNullableString(value: unknown): string | null {
  if (value === null) return null;
  return parseRequiredString(value);
}

function isExactObject<const Keys extends readonly string[]>(
  value: unknown,
  keys: Keys,
): value is Record<Keys[number], unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}
