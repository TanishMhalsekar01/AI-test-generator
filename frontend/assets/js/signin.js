// Explains sign-in problems: ?error=... from the OAuth flow, and server
// configuration that would make GitHub sign-in fail (checked via /auth/status).
const MESSAGES = {
  oauth_not_configured: ['GitHub sign-in is not configured',
    'The server is missing GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET. See the README section "GitHub OAuth App".'],
  state_mismatch: ['Sign-in expired', 'The sign-in request expired or was opened in another browser. Try again.'],
  access_denied: ['Access was not granted', 'GitHub authorization was cancelled.'],
  token_exchange_failed: ['GitHub rejected the sign-in', 'The authorization code could not be exchanged. Try again.'],
  github_unreachable: ['GitHub is unreachable', 'The server could not contact GitHub. Try again in a moment.'],
};

function show(title, text) {
  document.getElementById('signin-error-title').textContent = title;
  document.getElementById('signin-error-text').textContent = text;
  document.getElementById('signin-error').classList.remove('hidden');
}

function unreachableBaseUrl(baseUrl) {
  const here = `https://${window.location.host}`; // production always serves HTTPS
  show('Sign-in address is not reachable yet',
    `This server finishes GitHub sign-in at ${baseUrl} (APP_BASE_URL), but that domain has no DNS record yet. `
    + 'Either finish the DNS setup for that domain, or set APP_BASE_URL to '
    + `${here} on the server and use ${here}/auth/github/callback as the GitHub OAuth App's callback URL.`);
}

const code = new URLSearchParams(window.location.search).get('error');
if (code && code !== 'base_url_unreachable') {
  const [title, text] = MESSAGES[code] || ['Sign-in failed', `Error code: ${code}`];
  show(title, text);
}

// Warn before the user goes through GitHub if the configured address cannot work.
fetch('/auth/status', { credentials: 'same-origin' })
  .then((r) => (r.ok ? r.json() : null))
  .then((status) => {
    if (!status) return;
    if (!status.oauth_configured) {
      show(...MESSAGES.oauth_not_configured);
    } else if (!status.on_base_host && !status.base_host_resolves) {
      unreachableBaseUrl(status.app_base_url);
    }
  })
  .catch(() => {
    if (code === 'base_url_unreachable') show('Sign-in address is not reachable yet', 'Check APP_BASE_URL on the server.');
  });
