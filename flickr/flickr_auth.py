"""
flickr_auth.py — one-time OAuth 1.0a setup for flickr-curator

Run this once interactively to authorise the app and save credentials.
After that, the poller loads the saved token silently.

Usage:
    uv run python flickr/flickr_auth.py --config config/config.yaml

The script will:
  1. Use your API key + secret from config to get a request token
  2. Open flickr.com in your browser to approve access
  3. Ask you to paste back the verifier code
  4. Exchange for an access token and save it to config

Requires:
    uv add requests requests-oauthlib pyyaml
"""

import argparse
import sys
import webbrowser
from pathlib import Path

import yaml
from requests_oauthlib import OAuth1Session

# Flickr OAuth endpoints
REQUEST_TOKEN_URL = "https://www.flickr.com/services/oauth/request_token"
AUTHORIZE_URL     = "https://www.flickr.com/services/oauth/authorize"
ACCESS_TOKEN_URL  = "https://www.flickr.com/services/oauth/access_token"


def load_config(path: Path) -> dict:
    if not path.exists():
        print(f"Config file not found: {path}")
        print("Create one first — see config/config.example.yaml")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def save_config(path: Path, config: dict):
    with open(path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    print(f"Config saved to {path}")


def run_auth_flow(api_key: str, api_secret: str) -> tuple[str, str, str, str]:
    """
    Run the full OAuth 1.0a three-legged flow.
    Returns (oauth_token, oauth_token_secret, user_nsid).
    """

    # Step 1: get a request token (oob = out-of-band, no callback URL)
    print("\nStep 1: requesting token from Flickr...")
    oauth = OAuth1Session(api_key, client_secret=api_secret, callback_uri="oob")
    try:
        resp = oauth.fetch_request_token(REQUEST_TOKEN_URL)
    except Exception as e:
        print(f"Failed to get request token: {e}")
        print("Check that your api_key and api_secret are correct.")
        sys.exit(1)

    request_token  = resp["oauth_token"]
    request_secret = resp["oauth_token_secret"]

    # Step 2: send user to Flickr to approve
    # perms=delete gives us read + write + delete (needed to set privacy)
    auth_url = f"{AUTHORIZE_URL}?oauth_token={request_token}&perms=write"
    print(f"\nStep 2: opening Flickr authorisation page...")
    print(f"  URL: {auth_url}\n")
    webbrowser.open(auth_url)

    print("After you click 'OK, I'll authorize it' on Flickr, you'll see")
    print("a 9-digit code. Paste it here:\n")
    verifier = input("Verifier code: ").strip()
    if not verifier:
        print("No verifier entered, aborting.")
        sys.exit(1)

    # Step 3: exchange for access token
    print("\nStep 3: exchanging for access token...")
    oauth = OAuth1Session(
        api_key,
        client_secret=api_secret,
        resource_owner_key=request_token,
        resource_owner_secret=request_secret,
        verifier=verifier,
    )
    try:
        resp = oauth.fetch_access_token(ACCESS_TOKEN_URL)
    except Exception as e:
        print(f"Failed to get access token: {e}")
        print("The verifier code may be wrong or expired. Try again.")
        sys.exit(1)

    access_token        = resp["oauth_token"]
    access_token_secret = resp["oauth_token_secret"]
    user_nsid           = resp.get("user_nsid", "")
    username            = resp.get("username", "")

    print(f"\nAuthorised as: {username} ({user_nsid})")
    return access_token, access_token_secret, user_nsid, username


def verify_token(api_key: str, api_secret: str, token: str, token_secret: str) -> bool:
    """Call flickr.auth.checkToken to confirm the saved token works."""
    oauth = OAuth1Session(
        api_key,
        client_secret=api_secret,
        resource_owner_key=token,
        resource_owner_secret=token_secret,
    )
    resp = oauth.get(
        "https://api.flickr.com/services/rest/",
        params={
            "method":         "flickr.auth.oauth.checkToken",
            "format":         "json",
            "nojsoncallback": 1,
        },
    )
    data = resp.json()
    return data.get("stat") == "ok"


def main():
    parser = argparse.ArgumentParser(description="Flickr OAuth setup for flickr-curator")
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config file (default: config/config.yaml)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify the saved token, don't re-authorise",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)

    flickr_cfg = config.get("flickr", {})
    api_key    = flickr_cfg.get("api_key", "")
    api_secret = flickr_cfg.get("api_secret", "")

    if not api_key or not api_secret:
        print("ERROR: flickr.api_key and flickr.api_secret must be set in config.")
        sys.exit(1)

    # --verify-only mode
    if args.verify_only:
        token        = flickr_cfg.get("oauth_token", "")
        token_secret = flickr_cfg.get("oauth_token_secret", "")
        if not token or not token_secret:
            print("No saved token found. Run without --verify-only to authorise.")
            sys.exit(1)
        if verify_token(api_key, api_secret, token, token_secret):
            print("Token is valid.")
        else:
            print("Token is INVALID. Re-run without --verify-only to re-authorise.")
            sys.exit(1)
        return

    # Check if we already have a valid token
    existing_token  = flickr_cfg.get("oauth_token", "")
    existing_secret = flickr_cfg.get("oauth_token_secret", "")
    if existing_token and existing_secret:
        print("Found existing token. Verifying...")
        if verify_token(api_key, api_secret, existing_token, existing_secret):
            print("Token is already valid — nothing to do.")
            print("Run with --verify-only to check again, or delete the token")
            print("from config.yaml to force re-authorisation.")
            return
        else:
            print("Existing token is invalid or expired. Re-authorising...")

    # Run the flow
    token, token_secret, user_nsid, username = run_auth_flow(api_key, api_secret)

    # Save back to config
    if "flickr" not in config:
        config["flickr"] = {}
    config["flickr"]["oauth_token"]        = token
    config["flickr"]["oauth_token_secret"] = token_secret
    config["flickr"]["user_nsid"]          = user_nsid
    if username:
        config["flickr"]["username"]       = username
    save_config(config_path, config)

    # Verify it works
    print("\nVerifying token...")
    if verify_token(api_key, api_secret, token, token_secret):
        print("All good. You're ready to run the poller.")
    else:
        print("WARNING: token saved but verification failed. Check your app permissions.")


if __name__ == "__main__":
    main()
