const WAJE_PYTHON_RUNTIME_COMMAND = "uv";
const WAJE_PYTHON_RUNTIME_PREFIX = [
  "run",
  "--python",
  "3.12",
  "--with-requirements",
  "requirements.txt",
  "python",
] as const;

export function wajePythonInvocation(pythonArgs: readonly string[]) {
  return {
    command: WAJE_PYTHON_RUNTIME_COMMAND,
    args: [...WAJE_PYTHON_RUNTIME_PREFIX, ...pythonArgs],
  };
}
