// Fetch wrapper: same-origin cookie session; any 401 sends the browser to sign-in.

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function detailOf(body) {
  if (!body) return '';
  if (typeof body.detail === 'string') return body.detail;
  if (Array.isArray(body.detail)) return body.detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
  return '';
}

export async function api(path, options = {}) {
  let resp;
  try {
    resp = await fetch(path, {
      credentials: 'same-origin',
      ...options,
      headers: { Accept: 'application/json', ...(options.headers || {}) },
    });
  } catch (err) {
    throw new ApiError('Could not reach the server. Check your connection and try again.', 0);
  }
  if (resp.status === 401) {
    window.location.assign('/');
    throw new ApiError('Your session has ended. Sign in again.', 401);
  }
  if (resp.status === 204) return null;
  let body = null;
  try {
    body = await resp.json();
  } catch (_) {
    body = null;
  }
  if (!resp.ok) throw new ApiError(detailOf(body) || `Request failed with HTTP ${resp.status}.`, resp.status);
  return body;
}

export const postJSON = (path, data) =>
  api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });

export const postForm = (path, formData) => api(path, { method: 'POST', body: formData });
