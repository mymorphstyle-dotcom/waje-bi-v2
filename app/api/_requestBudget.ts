import { gatewayError } from "./_conversationStore";

export const CUSTOMER_JSON_BODY_MAX_BYTES = 64 * 1024;
export const CUSTOMER_MESSAGE_MAX_BYTES = 16 * 1024;

export async function readBoundedCustomerJson(
  request: Request,
  invalidCode: string,
): Promise<Record<string, unknown>> {
  const declaredLength = request.headers.get("content-length");
  if (declaredLength !== null) {
    if (!/^\d+$/.test(declaredLength)) throw gatewayError(invalidCode);
    const bytes = Number(declaredLength);
    if (!Number.isSafeInteger(bytes)) throw gatewayError(invalidCode);
    if (bytes > CUSTOMER_JSON_BODY_MAX_BYTES) {
      throw gatewayError("customer_request_body_too_large");
    }
  }
  const raw = await request.text();
  if (Buffer.byteLength(raw, "utf8") > CUSTOMER_JSON_BODY_MAX_BYTES) {
    throw gatewayError("customer_request_body_too_large");
  }
  try {
    const value: unknown = JSON.parse(raw);
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw gatewayError(invalidCode);
    }
    return value as Record<string, unknown>;
  } catch (error) {
    if (error instanceof Error && error.name === "GatewayRuntimeError") throw error;
    throw gatewayError(invalidCode);
  }
}

export function requireCustomerMessageBudget(message: string) {
  if (Buffer.byteLength(message, "utf8") > CUSTOMER_MESSAGE_MAX_BYTES) {
    throw gatewayError("customer_message_too_large");
  }
}
