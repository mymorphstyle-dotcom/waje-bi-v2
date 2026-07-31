import { createHash } from "node:crypto";

export class CanonicalDecimal {
  constructor(value) {
    if (typeof value !== "string" || !/^-?(0|[1-9]\d*)(\.\d+)?$/.test(value)) {
      throw new TypeError("decimal must be a base-10 string");
    }
    this.value = value;
  }
}

export class CanonicalTimestamp {
  constructor(value) {
    if (typeof value !== "string") {
      throw new TypeError("timestamp must be a string");
    }
    this.value = canonicalTimestamp(value);
  }
}

export function canonicalIdentityJson(value) {
  return JSON.stringify(normalize(value));
}

export function canonicalIdentitySha256(value) {
  return createHash("sha256").update(canonicalIdentityJson(value), "utf8").digest("hex");
}

function normalize(value) {
  if (value instanceof CanonicalDecimal) {
    return canonicalDecimal(value.value);
  }
  if (value instanceof CanonicalTimestamp) {
    return value.value;
  }
  if (value === null || typeof value === "boolean") {
    return value;
  }
  if (typeof value === "string") {
    return value.normalize("NFC");
  }
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) {
      throw new TypeError("binary float and unsafe integer identity inputs are forbidden");
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map(normalize);
  }
  if (typeof value === "object") {
    const normalized = {};
    for (const [rawKey, rawValue] of Object.entries(value)) {
      const key = rawKey.normalize("NFC");
      if (Object.hasOwn(normalized, key)) {
        throw new TypeError("identity keys collide after NFC normalization");
      }
      normalized[key] = normalize(rawValue);
    }
    return Object.fromEntries(
      Object.entries(normalized).sort(([left], [right]) =>
        compareCodePointSequences(left, right),
      ),
    );
  }
  throw new TypeError(`unsupported identity value: ${typeof value}`);
}

function canonicalTimestamp(value) {
  const match = value.match(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/,
  );
  if (!match) {
    throw new TypeError("timestamp must be ISO-8601 with an explicit offset");
  }
  const [, yearText, monthText, dayText, hourText, minuteText, secondText, fraction = "", zone] =
    match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  if (hour > 23 || minute > 59 || second > 59) {
    throw new TypeError("timestamp contains an invalid clock time");
  }
  const localEpoch = Date.UTC(year, month - 1, day, hour, minute, second);
  const local = new Date(localEpoch);
  if (
    local.getUTCFullYear() !== year ||
    local.getUTCMonth() !== month - 1 ||
    local.getUTCDate() !== day
  ) {
    throw new TypeError("timestamp contains an invalid calendar date");
  }
  let offsetMinutes = 0;
  if (zone !== "Z") {
    const sign = zone[0] === "+" ? 1 : -1;
    const offsetHour = Number(zone.slice(1, 3));
    const offsetMinute = Number(zone.slice(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) {
      throw new TypeError("timestamp contains an invalid UTC offset");
    }
    offsetMinutes = sign * (offsetHour * 60 + offsetMinute);
  }
  const utc = new Date(localEpoch - offsetMinutes * 60_000);
  const microseconds = fraction.padEnd(6, "0");
  return `${utc.toISOString().slice(0, 19)}.${microseconds}Z`;
}

function compareCodePointSequences(left, right) {
  const leftPoints = Array.from(left, (value) => value.codePointAt(0));
  const rightPoints = Array.from(right, (value) => value.codePointAt(0));
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < length; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) {
      return leftPoints[index] - rightPoints[index];
    }
  }
  return leftPoints.length - rightPoints.length;
}

function canonicalDecimal(value) {
  const negative = value.startsWith("-");
  const unsigned = negative ? value.slice(1) : value;
  const [whole, fraction = ""] = unsigned.split(".");
  const trimmedFraction = fraction.replace(/0+$/, "");
  const trimmedWhole = whole.replace(/^0+(?=\d)/, "");
  const isZero = /^0*$/.test(trimmedWhole) && trimmedFraction.length === 0;
  if (isZero) {
    return "0";
  }
  const magnitude =
    trimmedFraction.length > 0 ? `${trimmedWhole}.${trimmedFraction}` : trimmedWhole;
  return negative ? `-${magnitude}` : magnitude;
}
