import { access } from "node:fs/promises";
import { fileURLToPath } from "node:url";

export async function resolve(specifier, context, nextResolve) {
  try {
    return await nextResolve(specifier, context);
  } catch (error) {
    if (
      error?.code !== "ERR_MODULE_NOT_FOUND"
      || !context.parentURL?.startsWith("file:")
      || (!specifier.startsWith("./") && !specifier.startsWith("../"))
      || /\.[^/]+$/.test(specifier)
    ) {
      throw error;
    }

    const candidate = new URL(`${specifier}.ts`, context.parentURL);
    try {
      await access(fileURLToPath(candidate));
    } catch {
      throw error;
    }
    return nextResolve(candidate.href, context);
  }
}
