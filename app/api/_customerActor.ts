const AUTHENTICATED_USER_HEADER = "x-waje-authenticated-user-id";

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
    return actorId;
  }
  if (process.env.NODE_ENV !== "production") return "local-user";
  throw new CustomerActorError("customer_identity_required", 401);
}

export function assertInternalRouteAvailable() {
  if (process.env.NODE_ENV === "production") {
    throw new CustomerActorError("internal_route_unavailable", 404);
  }
}
