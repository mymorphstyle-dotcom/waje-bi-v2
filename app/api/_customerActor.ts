import { createHmac, timingSafeEqual } from "node:crypto";

const AUTHENTICATED_USER_HEADER = "x-waje-authenticated-user-id";
const AUTHENTICATED_USER_ISSUED_AT_HEADER =
  "x-waje-authenticated-user-issued-at";
const AUTHENTICATION_SIGNATURE_HEADER = "x-waje-authentication-signature";
const AUTHENTICATION_SCHEME = "waje-auth-v1";
const DEFAULT_MAX_AGE_SECONDS = 60;

class CustomerActorError extends Error {
  readonly code: string;
  readonly httpStatus: number;

  constructor(code: string, httpStatus: number) {
    super(code);
    this.name = "CustomerActorError";
    this.code = code;
    this.httpStatus = httpStatus;
  }
}

export function resolveCustomerActor(request: Request): string {
  const headerValue = request.headers.get(AUTHENTICATED_USER_HEADER);
  if (headerValue !== null) {
    const actorId = headerValue.trim();
    if (!actorId || actorId.length > 256 || /[\u0000-\u001f\u007f]/.test(actorId)) {
      throw new CustomerActorError("customer_identity_invalid", 400);
    }
    if (process.env.NODE_ENV === "production") {
      verifyTrustedActorHeader(request, actorId);
    }
    return actorId;
  }
  if (process.env.NODE_ENV !== "production") return "local-user";
  throw new CustomerActorError("customer_identity_required", 401);
}

function verifyTrustedActorHeader(request: Request, actorId: string) {
  const secret = process.env.WAJE_AUTH_HEADER_SECRET ?? "";
  if (Buffer.byteLength(secret, "utf8") < 32) {
    throw new CustomerActorError(
      "customer_identity_configuration_invalid",
      500,
    );
  }
  const issuedAt = request.headers.get(AUTHENTICATED_USER_ISSUED_AT_HEADER) ?? "";
  const suppliedSignature = (
    request.headers.get(AUTHENTICATION_SIGNATURE_HEADER) ?? ""
  ).toLowerCase();
  if (!/^\d{10}$/.test(issuedAt) || !/^[0-9a-f]{64}$/.test(suppliedSignature)) {
    throw new CustomerActorError("customer_identity_untrusted", 401);
  }
  const issuedAtSeconds = Number(issuedAt);
  const maxAgeSeconds = authenticatedHeaderMaxAgeSeconds();
  const nowSeconds = Math.floor(Date.now() / 1000);
  if (
    !Number.isSafeInteger(issuedAtSeconds)
    || Math.abs(nowSeconds - issuedAtSeconds) > maxAgeSeconds
  ) {
    throw new CustomerActorError("customer_identity_expired", 401);
  }
  const url = new URL(request.url);
  const canonical = [
    AUTHENTICATION_SCHEME,
    request.method.toUpperCase(),
    `${url.pathname}${url.search}`,
    issuedAt,
    actorId,
  ].join("\n");
  const expectedSignature = createHmac("sha256", secret)
    .update(canonical)
    .digest();
  const supplied = Buffer.from(suppliedSignature, "hex");
  if (
    supplied.length !== expectedSignature.length
    || !timingSafeEqual(supplied, expectedSignature)
  ) {
    throw new CustomerActorError("customer_identity_untrusted", 401);
  }
}

function authenticatedHeaderMaxAgeSeconds() {
  const configured = process.env.WAJE_AUTH_HEADER_MAX_AGE_SECONDS;
  if (configured === undefined) return DEFAULT_MAX_AGE_SECONDS;
  if (!/^\d+$/.test(configured)) {
    throw new CustomerActorError(
      "customer_identity_configuration_invalid",
      500,
    );
  }
  const value = Number(configured);
  if (!Number.isSafeInteger(value) || value < 1 || value > 300) {
    throw new CustomerActorError(
      "customer_identity_configuration_invalid",
      500,
    );
  }
  return value;
}

export function assertInternalRouteAvailable() {
  if (process.env.NODE_ENV === "production") {
    throw new CustomerActorError("internal_route_unavailable", 404);
  }
}
