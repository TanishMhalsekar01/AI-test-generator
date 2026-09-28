// Shows the reason when GitHub sign-in redirected back with ?error=...
const MESSAGES = {
  oauth_not_configured: ['GitHub sign-in is not configured',
    'The server is missing GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET. See the README section "GitHub OAuth App".'],
  state_mismatch: ['Sign-in expired', 'The sign-in request expired or was opened in another browser. Try again.'],
  access_denied: ['Access was not granted', 'GitHub authorization was cancelled.'],
  token_exchange_failed: ['GitHub rejected the sign-in', 'The authorization code could not be exchanged. Try again.'],
  github_unreachable: ['GitHub is unreachable', 'The server could not contact GitHub. Try again in a moment.'],
};

const code = new URLSearchParams(window.location.search).get('error');
if (code) {
  const [title, text] = MESSAGES[code] || ['Sign-in failed', `Error code: ${code}`];
  document.getElementById('signin-error-title').textContent = title;
  document.getElementById('signin-error-text').textContent = text;
  document.getElementById('signin-error').classList.remove('hidden');
}
