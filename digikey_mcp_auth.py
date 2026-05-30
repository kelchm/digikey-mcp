"""One-time OAuth bootstrap CLI for the DigiKey MCP MyLists tools.

DigiKey requires 3-legged (authorization_code) OAuth for MyLists. This CLI does the
dance once on the operator's laptop, prints the resulting refresh_token, and exits.
The operator then either:

  * sets DIGIKEY_REFRESH_TOKEN_SEED on the deployment (recommended for remote
    deployments — the server consumes the seed on first use and persists rotated
    tokens to its writable cache thereafter), or
  * passes --write-cache to persist the tokens directly to the local cache file
    (recommended for local dev — the server then has nothing extra to configure).

DigiKey requires the redirect_uri to be HTTPS — `http://127.0.0.1` is rejected.
We don't try to run a callback server here: instead we tell the operator to paste
the URL their browser landed on after login, and we parse `?code=...` out of it.
The landing page itself can be unreachable (no server listening at the URI) —
the authorization code is in the URL bar regardless.

The redirect_uri passed to /authorize must match exactly the value registered to
the operator's DigiKey app. Default is `https://localhost`, which is what DigiKey
suggests for apps without existing infrastructure.
"""
import argparse
import json
import os
import stat
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import requests
from dotenv import load_dotenv


def _default_token_cache_path() -> Path:
    xdg = os.getenv("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "digikey-mcp" / "tokens.json"


def _exchange_code(
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    token_url: str,
) -> dict:
    resp = requests.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        # Generous but bounded — a stuck endpoint shouldn't leave the CLI hanging
        # past the auth code's 60-second TTL (when the code would expire anyway).
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(
            f"Token exchange failed ({resp.status_code}): {resp.text}\n\n"
            "Common causes:\n"
            "  - The authorization code expired (DigiKey codes live ~60 seconds — "
            "be quick between login and paste).\n"
            "  - redirect_uri doesn't match the value registered to your DigiKey app.\n"
            "  - CLIENT_ID / CLIENT_SECRET wrong or for the wrong DigiKey environment "
            "(production vs sandbox)."
        )
    return resp.json()


def _extract_code(raw_input: str) -> str:
    """Pull the `code` query param out of whatever the user pasted.

    Accepts: a full URL, a URL fragment, or a bare code. The latter exists because
    some browsers strip the protocol when you copy a 'this site can't be reached'
    URL.
    """
    s = raw_input.strip()
    if "code=" not in s:
        # Treat as bare code.
        return s
    parsed = urllib.parse.urlparse(s if "://" in s else "https://x?" + s.split("?", 1)[-1])
    code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        raise SystemExit(
            f"Could not parse a `code` query param from your input:\n  {s!r}\n"
            "Paste the full URL your browser landed on (the 'this site can't be "
            "reached' page is fine — the URL bar still has the code)."
        )
    return code


def _write_cache(tokens: dict, path: Path, expires_in_default: int = 1799) -> None:
    """Write a fresh token cache compatible with the server's _read_token_cache.

    Uses os.open with mode 0600 so the file is created with restrictive perms
    from the start — write_text() + chmod has a window where the umask
    determines the mode, which on most systems makes the refresh token briefly
    readable by other local users.
    """
    import time

    state = {
        "refresh_token": tokens.get("refresh_token"),
        "access_token": tokens.get("access_token"),
        "expires_at": int(time.time()) + int(tokens.get("expires_in", expires_in_default)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_TRUNC because re-running `login --write-cache` should replace an existing
    # cache without prompting; we accept the trade-off vs O_EXCL since the auth
    # CLI is a deliberate operator action, not a concurrent process.
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(state, indent=2))


def cmd_login(args: argparse.Namespace) -> int:
    load_dotenv()
    client_id = args.client_id or os.getenv("CLIENT_ID")
    client_secret = args.client_secret or os.getenv("CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "Missing CLIENT_ID / CLIENT_SECRET. Set them in .env or pass "
            "--client-id / --client-secret.",
            file=sys.stderr,
        )
        return 1

    use_sandbox = args.sandbox or os.getenv("USE_SANDBOX", "").lower() in ("true", "1", "yes")
    if use_sandbox:
        authorize_url = "https://sandbox-api.digikey.com/v1/oauth2/authorize"
        token_url = "https://sandbox-api.digikey.com/v1/oauth2/token"
    else:
        authorize_url = "https://api.digikey.com/v1/oauth2/authorize"
        token_url = "https://api.digikey.com/v1/oauth2/token"

    redirect_uri = args.redirect_uri or os.getenv("DIGIKEY_REDIRECT_URI", "https://localhost")
    qs = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    })
    full_url = f"{authorize_url}?{qs}"

    env_label = "SANDBOX" if use_sandbox else "PRODUCTION"
    print(f"DigiKey environment: {env_label}")
    print(f"Redirect URI:        {redirect_uri}")
    print(
        f"\nOpen this URL in your browser and log in:\n\n  {full_url}\n\n"
        "After you authorize, your browser will redirect to a URL that may show "
        "'this site can't be reached' — that's fine. Copy the URL from the address "
        "bar (it contains ?code=...) and paste it below.\n"
    )
    if not args.no_open:
        try:
            webbrowser.open(full_url)
        except Exception:
            pass

    try:
        raw = input("Paste the redirect URL (or just the code): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        return 1

    code = _extract_code(raw)
    tokens = _exchange_code(code, redirect_uri, client_id, client_secret, token_url)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print(
            f"Token exchange succeeded but the response had no refresh_token. "
            f"Full response: {tokens}",
            file=sys.stderr,
        )
        return 1

    print("\n=== SUCCESS ===")
    if args.write_cache:
        cache_path = Path(args.cache_path or _default_token_cache_path())
        _write_cache(tokens, cache_path)
        print(f"Tokens written to: {cache_path}")
        print(
            "\nStart the MCP server normally — it will read this file. If you move "
            "the deployment elsewhere, run this CLI again with no --write-cache to "
            "print a refresh_token you can pass via DIGIKEY_REFRESH_TOKEN_SEED."
        )
    else:
        print(
            "\nrefresh_token (copy this once — it rotates on every server-side use):"
        )
        print(f"\n  {refresh_token}\n")
        print(
            "Inject it into your deployment as:\n"
            "  DIGIKEY_REFRESH_TOKEN_SEED=<above>\n\n"
            "Also ensure DIGIKEY_TOKEN_CACHE points at a writable file path on the "
            "deployment so the rotated tokens can be persisted after first use."
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Print the contents of the local token cache (without the access_token, to
    avoid casually leaking a short-lived bearer)."""
    path = Path(args.cache_path or _default_token_cache_path())
    if not path.exists():
        print(f"No token cache at {path}.", file=sys.stderr)
        return 1
    state = json.loads(path.read_text())
    refresh = state.get("refresh_token") or ""
    expires_at = state.get("expires_at")
    print(f"Cache path:         {path}")
    print(f"refresh_token tail: ...{refresh[-12:] if refresh else '(missing)'}")
    print(f"expires_at:         {expires_at}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="digikey-mcp-auth",
        description="One-time OAuth bootstrap for the DigiKey MCP MyLists tools.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="Run the 3-legged OAuth flow.")
    p_login.add_argument("--client-id", help="Override CLIENT_ID from env/.env.")
    p_login.add_argument("--client-secret", help="Override CLIENT_SECRET from env/.env.")
    p_login.add_argument("--sandbox", action="store_true", help="Use DigiKey sandbox endpoints.")
    p_login.add_argument(
        "--redirect-uri", default=None,
        help="Must match the URI registered on your DigiKey app. Default: https://localhost.",
    )
    p_login.add_argument(
        "--write-cache", action="store_true",
        help="Persist tokens to the local cache file (default: print refresh_token to stdout).",
    )
    p_login.add_argument(
        "--cache-path", default=None,
        help="Override the cache file path (default: $XDG_CONFIG_HOME/digikey-mcp/tokens.json).",
    )
    p_login.add_argument(
        "--no-open", action="store_true",
        help="Don't auto-open the authorize URL in a browser; just print it.",
    )
    p_login.set_defaults(func=cmd_login)

    p_show = sub.add_parser("show", help="Show local token cache state (without leaking the access_token).")
    p_show.add_argument("--cache-path", default=None, help="Override the cache file path.")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
