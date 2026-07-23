import { accessSync, constants } from "node:fs";
import { isAbsolute, join } from "node:path";

export function wajePythonInvocation(pythonArgs: readonly string[]) {
  const configured = process.env.WAJE_PYTHON_EXECUTABLE;
  const command = configured || join(process.cwd(), ".venv", "bin", "python");
  if (!isAbsolute(command) || command.includes("\0")) {
    throw new Error("waje_python_executable_invalid");
  }
  try {
    accessSync(command, constants.X_OK);
  } catch {
    throw new Error("waje_python_executable_unavailable");
  }
  return {
    command,
    args: [...pythonArgs],
  };
}
