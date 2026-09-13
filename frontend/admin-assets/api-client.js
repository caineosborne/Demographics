/**
 * Small JSON client shared by the admin shell.
 *
 * The optional auth hook is deliberately a function so a deployment can
 * provide a bearer token/session without coupling the shell to an auth SDK.
 */
export class ApiError extends Error {
  constructor(message, { status = 0, body = null, path = "" } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.path = path;
  }
}

const TERMINAL_JOB_STATES = new Set(["completed", "complete", "failed", "stopped", "cancelled", "interrupted"]);

const readJson = async (response) => {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("json")) return null;
  return response.json().catch(() => null);
};

/** Create a typed JSON request client. */
export function createApiClient({ baseUrl = "", fetchImpl = globalThis.fetch, getAuthToken = () => null } = {}) {
  if (typeof fetchImpl !== "function") throw new TypeError("A fetch implementation is required.");
  const request = async (path, { method = "GET", body, signal, headers = {} } = {}) => {
    const token = await getAuthToken();
    const requestHeaders = { Accept: "application/json", ...headers };
    if (body !== undefined) requestHeaders["Content-Type"] = "application/json";
    if (token) requestHeaders.Authorization = `Bearer ${token}`;
    let response;
    try {
      response = await fetchImpl(`${baseUrl}${path}`, {
        method, signal, headers: requestHeaders,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch (error) {
      throw new ApiError(error.message || "The API could not be reached.", { path });
    }
    const payload = await readJson(response);
    if (!response.ok) {
      const detail = payload?.detail || payload?.error || `Request failed (${response.status})`;
      throw new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail), { status: response.status, body: payload, path });
    }
    return payload;
  };

  const pollJob = async (path, { intervalMs = 1000, maxAttempts = 90, onUpdate = () => {}, isTerminal = (job) => TERMINAL_JOB_STATES.has(job?.status) } = {}) => {
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      const job = await request(path);
      onUpdate(job);
      if (isTerminal(job)) return job;
      if (attempt + 1 < maxAttempts && intervalMs > 0) await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
    throw new ApiError(`Job polling timed out after ${maxAttempts} attempts.`, { path });
  };

  return Object.freeze({ request, pollJob });
}

export const terminalJobStates = TERMINAL_JOB_STATES;
